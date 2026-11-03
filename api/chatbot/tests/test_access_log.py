"""
Tests for the admin access log (chatbot.access_log, AccessLog, AccessLogAdmin).

Django's admin LogEntry only records add/change/delete. With named accounts we
also need to know who *viewed* or *exported* participant data, so every admin
request, every staff-only API request, and every login/logout/failed login
writes one AccessLog row. Participant traffic (chat, keystrokes, survey ingest)
must never be logged — it is high-volume and not staff activity.

Coverage:
  - classify(): pure mapping of (method, path) to an action, no DB
  - client_ip(): picks the address the load balancer saw, not a spoofed one
  - changelist GET logs a view (sync and async stacks)
  - export POST logs an export
  - failed login logs login_failed with the username and no password
  - login / logout are logged once each
  - a participant chat request logs nothing, even from a logged-in user
  - a logging failure does not break the request
  - only superusers see the AccessLog admin; nobody can change or delete rows
"""

import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import AsyncClient, Client, RequestFactory

from chatbot.access_log import classify, client_ip
from chatbot.models import AccessLog

Action = AccessLog.Action
EDGE = "130.176.1.1"  # a CloudFront edge address

# ── classify() — pure, no fixtures ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        # admin pages
        ("GET", "/api/admin/", Action.VIEW),
        ("GET", "/api/admin/chatbot/conversation/", Action.VIEW),
        ("GET", "/api/admin/chatbot/utterance/12/change/", Action.VIEW),
        ("HEAD", "/api/admin/chatbot/utterance/", Action.VIEW),
        ("POST", "/api/admin/chatbot/bot/3/change/", Action.CHANGE),
        ("POST", "/api/admin/chatbot/bot/3/delete/", Action.CHANGE),
        # import_export: GET renders the format form, POST streams the file
        ("GET", "/api/admin/chatbot/conversation/export/", Action.VIEW),
        ("POST", "/api/admin/chatbot/conversation/export/", Action.EXPORT),
        # login/logout are recorded by auth signals, not twice by the middleware
        ("GET", "/api/admin/login/", None),
        ("POST", "/api/admin/login/", None),
        ("POST", "/api/admin/logout/", None),
        # staff-only API (services/bots.py, avatar.py, upload.py)
        ("GET", "/api/bots/", Action.VIEW),
        ("POST", "/api/bots/", Action.CHANGE),
        ("GET", "/api/bots/7/", Action.VIEW),
        ("PUT", "/api/bots/7/", Action.CHANGE),
        ("DELETE", "/api/bots/7/", Action.CHANGE),
        ("GET", "/api/avatar/", Action.VIEW),
        ("POST", "/api/avatar/", Action.CHANGE),
        ("POST", "/api/avatar/mybot/", Action.CHANGE),
        ("DELETE", "/api/avatar/mybot/", Action.CHANGE),
        ("POST", "/api/avatar-upload/", Action.CHANGE),
        # avatar detail GET is public — participants' chat page fetches it
        ("GET", "/api/avatar/mybot/", None),
        # participant endpoints
        ("POST", "/api/chatbot/", None),
        ("POST", "/api/initialize_conversation/", None),
        ("POST", "/api/followup/", None),
        ("POST", "/api/update_keystrokes/", None),
        ("POST", "/api/survey_response/", None),
        ("POST", "/api/session/", None),
        ("POST", "/api/upload_voice_utterance/", None),
        ("GET", "/health/", None),
        ("GET", "/", None),
        # CORS preflight is not activity
        ("OPTIONS", "/api/bots/", None),
        # prefix must match on a path boundary
        ("GET", "/api/administrator/", None),
        ("GET", "/api/botsx/", None),
    ],
)
def test_classify(method, path, expected):
    assert classify(method, path) == expected


@pytest.mark.parametrize(
    ("xff", "remote", "expected"),
    [
        # client, CloudFront edge, then our private hops (ALB, Docker)
        (f"158.130.0.9, {EDGE}, 10.0.1.5, 172.17.0.1", "127.0.0.1", "158.130.0.9"),
        # a client-supplied spoofed entry sits left of CloudFront's — ignored
        (f"1.2.3.4, 158.130.0.9, {EDGE}, 10.0.1.5", "127.0.0.1", "158.130.0.9"),
        # no CDN in front: the one public address is the client
        ("158.130.0.9, 10.0.1.5", "127.0.0.1", "158.130.0.9"),
        # local dev: no proxy header, fall back to the socket peer
        ("", "127.0.0.1", "127.0.0.1"),
        ("garbage, 10.0.0.1", "", None),
    ],
)
def test_client_ip(xff, remote, expected):
    request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR=xff, REMOTE_ADDR=remote)
    assert client_ip(request) == expected


# ── Integration ───────────────────────────────────────────────────────────────

PASSWORD = "s3cret-Passw0rd!"
CHANGELIST = "/api/admin/chatbot/conversation/"
ACCESS_LOG_ADMIN = "/api/admin/chatbot/accesslog/"


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser(
        username=f"admin_{uuid.uuid4().hex[:6]}", password=PASSWORD
    )


@pytest.fixture
def staff(db):
    return User.objects.create_user(
        username=f"staff_{uuid.uuid4().hex[:6]}", password=PASSWORD, is_staff=True
    )


@pytest.fixture
def admin_client(superuser):
    client = Client()
    client.force_login(superuser)
    AccessLog.objects.all().delete()  # drop the login row; tests count from here
    return client


@pytest.mark.django_db
def test_changelist_get_logs_a_view(admin_client, superuser):
    response = admin_client.get(CHANGELIST, HTTP_X_FORWARDED_FOR=f"158.130.0.9, {EDGE}")

    assert response.status_code == 200
    row = AccessLog.objects.get()
    assert row.user == superuser
    assert row.username == superuser.username
    assert row.action == Action.VIEW
    assert row.method == "GET"
    assert row.path == CHANGELIST
    assert row.status_code == 200
    assert row.ip == "158.130.0.9"


@pytest.mark.django_db
def test_query_string_is_recorded(admin_client):
    admin_client.get(CHANGELIST, {"q": "p001"})

    assert AccessLog.objects.get().query == "q=p001"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_changelist_get_logs_a_view_on_the_async_stack(superuser):
    # Production runs ASGI; the middleware must not touch the ORM synchronously.
    client = AsyncClient()
    await client.aforce_login(superuser)

    response = await client.get(CHANGELIST)

    assert response.status_code == 200
    row = await AccessLog.objects.exclude(action=Action.LOGIN).aget()
    assert row.action == Action.VIEW
    assert row.username == superuser.username


@pytest.mark.django_db
def test_export_logs_an_export(admin_client, make_conversation):
    make_conversation()

    response = admin_client.post(
        CHANGELIST + "export/",
        {"format": "0", "resource": "0", "conversationresource_id": "on"},
    )

    assert response.status_code == 200
    assert "attachment" in response["Content-Disposition"]
    assert AccessLog.objects.get().action == Action.EXPORT


@pytest.mark.django_db
def test_failed_login_logs_username_and_no_password(superuser):
    Client().post(
        "/api/admin/login/",
        {"username": superuser.username, "password": PASSWORD + "x"},
    )

    row = AccessLog.objects.get()
    assert row.action == Action.LOGIN_FAILED
    assert row.username == superuser.username
    assert row.user is None
    values = AccessLog.objects.filter(pk=row.pk).values().get()
    assert not any(PASSWORD in str(v) for v in values.values())


@pytest.mark.django_db
def test_login_and_logout_are_logged_once_each(superuser):
    client = Client()
    client.post(
        "/api/admin/login/", {"username": superuser.username, "password": PASSWORD}
    )
    client.post("/api/admin/logout/")

    actions = list(AccessLog.objects.order_by("id").values_list("action", "user"))
    assert actions == [(Action.LOGIN, superuser.pk), (Action.LOGOUT, superuser.pk)]


@pytest.mark.django_db
def test_participant_chat_request_logs_nothing(admin_client):
    # Even a logged-in researcher testing a bot is participant traffic.
    admin_client.post("/api/chatbot/", "{}", content_type="application/json")
    admin_client.post("/api/update_keystrokes/", "{}", content_type="application/json")

    assert not AccessLog.objects.exists()


@pytest.mark.django_db
def test_logging_failure_does_not_break_the_request(admin_client):
    with patch.object(AccessLog.objects, "create", side_effect=RuntimeError("db down")):
        response = admin_client.get(CHANGELIST)

    assert response.status_code == 200


@pytest.mark.django_db
def test_staff_who_is_not_superuser_cannot_see_access_log(staff):
    client = Client()
    client.force_login(staff)

    assert client.get(ACCESS_LOG_ADMIN).status_code == 403
    assert "accesslog" not in client.get("/api/admin/").content.decode()


@pytest.mark.django_db
def test_superuser_can_view_access_log(admin_client):
    assert admin_client.get(ACCESS_LOG_ADMIN).status_code == 200
    assert "accesslog" in admin_client.get("/api/admin/").content.decode()


@pytest.mark.django_db
def test_nobody_can_add_change_or_delete_rows(admin_client, superuser):
    row = AccessLog.objects.create(
        username="someone",
        action=Action.VIEW,
        method="GET",
        path="/x/",
        status_code=200,
    )
    change = f"{ACCESS_LOG_ADMIN}{row.pk}/change/"

    assert admin_client.post(ACCESS_LOG_ADMIN + "add/", {}).status_code == 403
    assert admin_client.post(change, {"username": "edited"}).status_code == 403
    assert (
        admin_client.post(
            f"{ACCESS_LOG_ADMIN}{row.pk}/delete/", {"post": "yes"}
        ).status_code
        == 403
    )
    admin_client.post(
        ACCESS_LOG_ADMIN,
        {"action": "delete_selected", "_selected_action": [row.pk], "post": "yes"},
    )

    row.refresh_from_db()
    assert row.username == "someone"
