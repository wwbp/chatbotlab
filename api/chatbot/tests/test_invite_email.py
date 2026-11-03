"""
Tests for staff invites and self-service password reset (chatbot/staff_auth.py,
UserAdmin in chatbot/admin.py, /api/auth/ URLs).

A superuser adds a person with one form (email, name, role); the person gets an
email with a single-use set-password link and never sees a temporary password.
The same link machinery serves "Forgotten your password?" on the admin login,
and it must also work for invited users who never set a password (Django's
stock form skips them).

Links are always built from SITE_URL, never from the request: behind
CloudFront -> ALB -> nginx the request looks like plain http to the EB hostname.
They live under /api/auth/, which the access log ignores, so a token never
lands in the database.

Coverage:
  - URL names reverse under /api/auth/; the admin login shows the forgot link
  - add form: email + at least one role required, duplicate email rejected,
    creates a staff user with an unusable password, username = lowercased email
  - adding sends exactly one invite (Reply-To inviter, username, roles, a link
    built from SITE_URL even for an http request with a foreign Host); editing
    sends nothing
  - following the link sets the password, then the admin login works; the link
    is single-use and expires
  - "Send set-password email": invalidates earlier links, superuser only,
    recorded in the admin history
  - reset form: unusable-password staff get mail; inactive, non-staff and
    unknown emails get none, and the same done page
  - access log: password_reset_requested and password_set rows, no token
  - a send failure or missing SITE_URL keeps the user and shows a warning
"""

import re
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission, User
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client
from django.urls import reverse

from chatbot.models import AccessLog

SITE_URL = "https://bot.wwbp.org"
PASSWORD = "s3cret-Passw0rd!"
NEW_PASSWORD = "Fresh-Correct-Horse-42"
ADD_URL = "/api/admin/auth/user/add/"
CHANGELIST = "/api/admin/auth/user/"
RESET_URL = "/api/auth/password_reset/"
LINK_RE = re.compile(r"https://\S+/api/auth/reset/\S+/")


@pytest.fixture(autouse=True)
def site_url(settings):
    settings.SITE_URL = SITE_URL


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(
        username=f"admin_{uuid.uuid4().hex[:6]}",
        email="pi@upenn.edu",
        password=PASSWORD,
        first_name="Lyle",
        last_name="Ungar",
    )


@pytest.fixture
def admin_client(superuser):
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def roles(db):
    return {
        g.name: g
        for g in Group.objects.filter(name__in=("Config editor", "Data viewer"))
    }


def add_user(client, roles_, capture=None, **overrides):
    data = {
        "email": "New.Person@Upenn.edu",
        "first_name": "New",
        "last_name": "Person",
        "groups": [g.pk for g in roles_],
        "_save": "Save",
    }
    data.update(overrides)
    with capture(execute=True) if capture else nullcontext():
        return client.post(ADD_URL, data, follow=True)


def link_in(message):
    return LINK_RE.search(message.body).group(0)


def staff_user(**kwargs):
    defaults = {
        "username": f"s_{uuid.uuid4().hex[:6]}",
        "email": "staff@upenn.edu",
        "is_staff": True,
    }
    defaults.update(kwargs)
    user = User.objects.create_user(**defaults)
    user.set_unusable_password()
    user.save()
    return user


def set_password_via(link, password=NEW_PASSWORD):
    client = Client()
    path = link.removeprefix(SITE_URL)
    page = client.get(path, follow=True)
    response = client.post(
        page.redirect_chain[-1][0] if page.redirect_chain else path,
        {"new_password1": password, "new_password2": password},
    )
    return response


# ── URLs and login page ───────────────────────────────────────────────────────


def test_reset_urls_reverse_under_api_auth():
    assert reverse("admin_password_reset") == "/api/auth/password_reset/"
    assert reverse("password_reset_done") == "/api/auth/password_reset/done/"
    assert (
        reverse("password_reset_confirm", args=["MQ", "tok"])
        == "/api/auth/reset/MQ/tok/"
    )
    assert reverse("password_reset_complete") == "/api/auth/reset/done/"


@pytest.mark.django_db
def test_admin_login_shows_forgot_password_link():
    response = Client().get("/api/admin/login/")
    # Django 5.2 wording; the link appears once admin_password_reset resolves.
    assert "Forgotten your login credentials?" in response.content.decode()
    assert RESET_URL in response.content.decode()


@pytest.mark.django_db
def test_complete_page_login_link_is_the_admin_login():
    response = Client().get("/api/auth/reset/done/")
    assert 'href="/api/admin/login/"' in response.content.decode()


# ── Add form ─────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_add_form_requires_email_and_a_role(
    admin_client, roles, django_capture_on_commit_callbacks
):
    response = add_user(admin_client, [], django_capture_on_commit_callbacks, email="")
    form = response.context["adminform"].form
    assert "email" in form.errors
    assert "groups" in form.errors
    assert not User.objects.filter(is_superuser=False).exists()


@pytest.mark.django_db
def test_add_form_rejects_duplicate_email_case_insensitively(
    admin_client, roles, django_capture_on_commit_callbacks
):
    staff_user(email="new.person@upenn.edu")
    response = add_user(
        admin_client, roles.values(), django_capture_on_commit_callbacks
    )
    assert "email" in response.context["adminform"].form.errors
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_add_creates_staff_user_with_unusable_password_and_roles(
    admin_client, roles, django_capture_on_commit_callbacks
):
    add_user(admin_client, roles.values(), django_capture_on_commit_callbacks)
    user = User.objects.get(username="new.person@upenn.edu")
    assert user.is_staff
    assert not user.is_superuser
    assert not user.has_usable_password()
    assert user.first_name == "New"
    assert set(user.groups.values_list("name", flat=True)) == set(roles)


@pytest.mark.django_db
def test_changelist_shows_last_login_and_active(admin_client):
    response = admin_client.get(CHANGELIST)
    html = response.content.decode()
    assert "column-last_login" in html
    assert "column-is_active" in html


# ── Invite on add ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_add_sends_exactly_one_invite_built_from_site_url(
    superuser, roles, settings, django_capture_on_commit_callbacks
):
    # As the request arrives in production: plain http, EB hostname.
    settings.ALLOWED_HOSTS = ["env.elasticbeanstalk.com"]
    client = Client(HTTP_HOST="env.elasticbeanstalk.com")
    client.force_login(superuser)
    add_user(client, [roles["Data viewer"]], django_capture_on_commit_callbacks)

    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["New.Person@upenn.edu"]  # User.clean normalises domain
    assert message.subject == "Your ChatbotLab account (Penn WWBP lab)"
    assert message.reply_to == ["pi@upenn.edu"]
    assert "Hello New," in message.body
    assert "Lyle Ungar has created an account" in message.body
    assert "Your username: new.person@upenn.edu" in message.body
    assert "Your access: Data viewer" in message.body
    assert "expires in 3 days" in message.body
    assert f"{SITE_URL}/api/admin/" in message.body
    assert link_in(message).startswith(f"{SITE_URL}/api/auth/reset/")
    assert message.message().get_content_type() == "text/plain"


@pytest.mark.django_db
def test_editing_a_user_sends_nothing(admin_client, django_capture_on_commit_callbacks):
    user = staff_user()
    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(
            f"/api/admin/auth/user/{user.pk}/change/",
            {
                "username": user.username,
                "email": user.email,
                "first_name": "Changed",
                "last_name": "",
                "is_active": "on",
                "is_staff": "on",
                "date_joined_0": "2026-11-01",
                "date_joined_1": "00:00:00",
                "_save": "Save",
            },
        )
    user.refresh_from_db()
    assert user.first_name == "Changed"
    assert len(mail.outbox) == 0


# In production the admin's transaction commits inside the request, so the
# on_commit warning reaches the page. A test transaction never commits, so run
# the callback at once instead.
@pytest.fixture
def commit_now():
    with patch("chatbot.admin.transaction.on_commit", lambda func: func()):
        yield


@pytest.mark.django_db
def test_send_failure_keeps_user_and_warns(admin_client, roles, commit_now):
    with patch(
        "django.core.mail.backends.locmem.EmailBackend.send_messages",
        side_effect=OSError("SES is down"),
    ):
        response = add_user(admin_client, roles.values())
    assert User.objects.filter(username="new.person@upenn.edu").exists()
    text = response.content.decode()
    assert "User saved, but the invite email could not be sent" in text
    assert "Send set-password email" in text


@pytest.mark.django_db
def test_missing_site_url_warns_without_crashing(
    admin_client, roles, settings, commit_now
):
    settings.SITE_URL = ""
    response = add_user(admin_client, roles.values())
    assert response.status_code == 200
    assert User.objects.filter(username="new.person@upenn.edu").exists()
    assert "User saved, but the invite email could not be sent" in (
        response.content.decode()
    )
    assert len(mail.outbox) == 0


# ── Following the link ────────────────────────────────────────────────────────


@pytest.fixture
def invited(admin_client, roles, django_capture_on_commit_callbacks):
    add_user(admin_client, roles.values(), django_capture_on_commit_callbacks)
    return User.objects.get(username="new.person@upenn.edu"), link_in(mail.outbox[0])


@pytest.mark.django_db
def test_link_sets_password_and_user_can_log_in(invited):
    user, link = invited
    response = set_password_via(link)
    assert response.status_code == 302
    assert response.url == "/api/auth/reset/done/"
    user.refresh_from_db()
    assert user.check_password(NEW_PASSWORD)

    login = Client().post(
        "/api/admin/login/?next=/api/admin/",
        {"username": user.username, "password": NEW_PASSWORD},
    )
    assert login.status_code == 302
    assert login.url == "/api/admin/"


@pytest.mark.django_db
def test_link_is_single_use(invited):
    _, link = invited
    set_password_via(link)
    page = Client().get(link.removeprefix(SITE_URL))
    assert page.status_code == 200
    assert not page.context["validlink"]


@pytest.mark.django_db
def test_link_expires(invited):
    _, link = invited
    # No freezegun here: move the token generator's clock past the default
    # PASSWORD_RESET_TIMEOUT (3 days).
    later = datetime.now() + timedelta(days=3, minutes=1)
    with patch.object(default_token_generator, "_now", return_value=later):
        page = Client().get(link.removeprefix(SITE_URL))
    assert not page.context["validlink"]


# ── Resend action ─────────────────────────────────────────────────────────────


def resend(client, user):
    return client.post(
        CHANGELIST,
        {
            "action": "send_set_password_email",
            "_selected_action": [user.pk],
        },
        follow=True,
    )


@pytest.mark.django_db
def test_resend_invalidates_previous_link_and_is_logged(admin_client, invited):
    user, first_link = invited
    resend(admin_client, user)
    assert len(mail.outbox) == 2
    second_link = link_in(mail.outbox[1])

    stale = Client().get(first_link.removeprefix(SITE_URL))
    assert not stale.context["validlink"]
    assert set_password_via(second_link).status_code == 302
    assert LogEntry.objects.filter(
        object_id=str(user.pk), change_message="Sent set-password email"
    ).exists()


@pytest.mark.django_db
def test_resend_is_superuser_only():
    user = staff_user()
    manager = staff_user(username="manager", email="m@upenn.edu")
    manager.set_password(PASSWORD)
    manager.user_permissions.set(
        Permission.objects.filter(codename__in=("view_user", "change_user"))
    )
    manager.save()
    client = Client()
    client.force_login(manager)
    resend(client, user)
    assert len(mail.outbox) == 0


# ── Self-service reset ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_reset_sends_for_staff_with_unusable_password(settings):
    settings.ALLOWED_HOSTS = ["env.elasticbeanstalk.com"]
    staff_user(email="Invited@upenn.edu", username="invited@upenn.edu")
    response = Client(HTTP_HOST="env.elasticbeanstalk.com").post(
        RESET_URL, {"email": "invited@UPENN.edu"}
    )
    assert response.status_code == 302
    assert response.url == "/api/auth/password_reset/done/"
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.subject == "Reset your ChatbotLab password"
    assert "username invited@upenn.edu" in message.body
    assert "expires in 3 days" in message.body
    assert message.reply_to == []
    assert link_in(message).startswith(f"{SITE_URL}/api/auth/reset/")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kwargs", [{"is_active": False}, {"is_staff": False}], ids=["inactive", "non-staff"]
)
def test_reset_skips_inactive_and_non_staff(kwargs):
    staff_user(email="x@upenn.edu", **kwargs)
    response = Client().post(RESET_URL, {"email": "x@upenn.edu"})
    assert response.url == "/api/auth/password_reset/done/"
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_unknown_email_gets_same_done_page_and_no_mail():
    response = Client().post(RESET_URL, {"email": "nobody@upenn.edu"})
    assert response.url == "/api/auth/password_reset/done/"
    assert len(mail.outbox) == 0


@pytest.fixture
def fresh_cache(settings):
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_reset_is_rate_limited_per_email_silently(fresh_cache):
    from chatbot.staff_auth import RESETS_PER_EMAIL_PER_HOUR

    staff_user(email="x@upenn.edu")
    for _ in range(RESETS_PER_EMAIL_PER_HOUR + 2):
        response = Client().post(RESET_URL, {"email": "X@upenn.edu"})
        # Same page either way, so the limit reveals nothing.
        assert response.url == "/api/auth/password_reset/done/"
    assert len(mail.outbox) == RESETS_PER_EMAIL_PER_HOUR
    # Still recorded, so an attack shows up in the access log.
    assert (
        AccessLog.objects.filter(
            action=AccessLog.Action.PASSWORD_RESET_REQUESTED
        ).count()
        == RESETS_PER_EMAIL_PER_HOUR + 2
    )


@pytest.mark.django_db
def test_reset_is_rate_limited_per_ip(fresh_cache):
    from chatbot.staff_auth import RESETS_PER_IP_PER_HOUR

    for i in range(RESETS_PER_IP_PER_HOUR + 1):
        staff_user(email=f"u{i}@upenn.edu", username=f"u{i}@upenn.edu")
    lab = Client(REMOTE_ADDR="203.0.113.7")
    for i in range(RESETS_PER_IP_PER_HOUR + 1):
        lab.post(RESET_URL, {"email": f"u{i}@upenn.edu"})
    assert len(mail.outbox) == RESETS_PER_IP_PER_HOUR
    Client(REMOTE_ADDR="198.51.100.9").post(RESET_URL, {"email": "u0@upenn.edu"})
    assert len(mail.outbox) == RESETS_PER_IP_PER_HOUR + 1


# ── Access log ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_access_log_records_request_and_password_set_without_token(admin_client):
    user = staff_user(email="r@upenn.edu")
    Client().post(RESET_URL, {"email": "R@upenn.edu"})
    link = link_in(mail.outbox[0])
    token = link.rstrip("/").rsplit("/", 1)[1]
    uid = link.rstrip("/").rsplit("/", 2)[1]

    # A logged-in user opening the link must not put the token in the log.
    admin_client.get(link.removeprefix(SITE_URL), follow=True)
    set_password_via(link)

    requested = AccessLog.objects.get(action="password_reset_requested")
    assert requested.user is None
    assert requested.username == "R@upenn.edu"
    password_set = AccessLog.objects.get(action="password_set")
    assert password_set.user == user
    for row in AccessLog.objects.all():
        assert token not in row.path + row.query
        assert uid not in row.path + row.query
