"""
Sign-in protection for the admin: login throttling, password length, session
age. See docs/deployment/access-control.rst, "Sign-in protection".
"""

from unittest.mock import patch

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.test import Client

from chatbot.models import AccessLog

LOGIN = "/api/admin/login/"
THROTTLED = "Too many sign-in attempts. Try again in 15 minutes."
PASSWORD = "Correct-horse-42"


@pytest.fixture
def fresh_cache(settings):
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def staff(db):
    return User.objects.create_user("alice", password=PASSWORD, is_staff=True)


def _login(client, username, password="wrong"):
    return client.post(LOGIN, {"username": username, "password": password})


def _throttled(response):
    return THROTTLED in response.content.decode()


def _logged_in(client):
    return "_auth_user_id" in client.session


def _failed_rows():
    return AccessLog.objects.filter(action=AccessLog.Action.LOGIN_FAILED).count()


@pytest.mark.django_db
def test_eleventh_attempt_for_a_username_is_refused(fresh_cache, staff):
    client = Client()
    for _ in range(10):
        assert not _throttled(_login(client, "alice"))
    with patch("django.contrib.auth.forms.authenticate") as auth:
        # Even the right password, and the username key is case-insensitive.
        r = _login(client, "ALICE", PASSWORD)
    assert _throttled(r)
    auth.assert_not_called()
    assert not _logged_in(client)


@pytest.mark.django_db
def test_throttled_attempts_write_no_login_failed_rows(fresh_cache, staff):
    client = Client()
    for _ in range(10):
        _login(client, "alice")
    assert _failed_rows() == 10
    for _ in range(5):
        _login(client, "alice")
    assert _failed_rows() == 10


@pytest.mark.django_db
def test_one_ip_is_refused_after_30_attempts_across_usernames(fresh_cache, staff):
    lab = Client(REMOTE_ADDR="203.0.113.7")
    for i in range(30):
        assert not _throttled(_login(lab, f"user{i}"))
    assert _throttled(_login(lab, "someone-new"))
    other = Client(REMOTE_ADDR="198.51.100.9")
    _login(other, "alice", PASSWORD)
    assert _logged_in(other)


@pytest.mark.django_db
def test_other_usernames_are_unaffected_by_one_being_throttled(fresh_cache, staff):
    client = Client()
    for _ in range(11):
        _login(client, "mallory")
    _login(client, "alice", PASSWORD)
    assert _logged_in(client)


@pytest.mark.django_db
def test_correct_credentials_within_the_limit_log_in(fresh_cache, staff):
    client = Client()
    for _ in range(9):
        _login(client, "alice")
    _login(client, "alice", PASSWORD)
    assert _logged_in(client)


def test_passwords_need_12_characters():
    with pytest.raises(ValidationError):
        validate_password("Xq7#mPz!vR2")
    validate_password("Xq7#mPz!vR2k")


def test_sessions_last_12_hours():
    assert django_settings.SESSION_COOKIE_AGE == 12 * 60 * 60
