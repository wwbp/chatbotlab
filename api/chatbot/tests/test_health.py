"""Liveness (/health/) and readiness (/api/health/ready/) endpoints."""

from unittest.mock import patch

import pytest

READY = "/api/health/ready/"
SECRET = "db.internal.example password=hunter2"


@pytest.mark.django_db
def test_ready_ok_against_real_database_and_cache(client):
    response = client.get(READY)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "cache": "ok"},
    }


@pytest.mark.django_db
def test_ready_is_not_cached_and_sets_no_cookie(client):
    response = client.get(READY)

    assert "no-store" in response["Cache-Control"]
    assert not response.cookies


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("broken", "other"),
    [("database", "cache"), ("cache", "database")],
)
def test_ready_503_names_the_failed_check_without_leaking(client, broken, other):
    with patch(f"chatbot.views._check_{broken}", side_effect=Exception(SECRET)):
        response = client.get(READY)

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {broken: "failed", other: "ok"},
    }
    assert "hunter2" not in response.content.decode()
    assert "db.internal" not in response.content.decode()


def test_cache_check_fails_when_the_value_does_not_round_trip():
    # django-redis runs with IGNORE_EXCEPTIONS, so a dead Redis does not raise:
    # the write is dropped and the read returns None.
    from chatbot.views import _check_cache

    with (
        patch("chatbot.views.cache.get", return_value=None),
        pytest.raises(RuntimeError, match="round-trip"),
    ):
        _check_cache()


def test_liveness_does_not_depend_on_the_database(client):
    # No django_db mark: pytest-django raises on any database access here, so
    # this passing proves /health/ never touches the database.
    with patch("chatbot.views._check_database", side_effect=Exception(SECRET)):
        response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
