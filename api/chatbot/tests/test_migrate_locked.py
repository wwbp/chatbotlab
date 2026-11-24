"""migrate_locked: startup DB steps are serialised by a MariaDB named lock.

These use real, separate DB connections (the command opens its own for the
lock), so the blocking seen here is the server's GET_LOCK, not a mock. The
wrapped steps themselves are patched out: they touch no tables, which is why
plain ``django_db`` is enough and nothing needs ``transaction=True``.
"""

import threading
from unittest.mock import call, patch

import pytest
from django.core.management import CommandError, call_command
from django.db import DEFAULT_DB_ALIAS, connections

from chatbot.management.commands import migrate_locked

STEPS = "chatbot.management.commands.migrate_locked.Command.run_steps"


@pytest.fixture
def other_instance():
    """A second DB session, standing in for another instance."""
    conn = connections.create_connection(DEFAULT_DB_ALIAS)
    yield conn
    conn.close()


def _scalar(conn, sql):
    with conn.cursor() as cursor:
        cursor.execute(sql, [migrate_locked.lock_name()])
        return cursor.fetchone()[0]


def test_lock_name_is_scoped_to_the_database(settings):
    # GET_LOCK names are server-wide; two databases on one server must not share.
    assert settings.DATABASES["default"]["NAME"] in migrate_locked.lock_name()


@pytest.mark.django_db
def test_concurrent_runs_are_serialised():
    events, errors = [], []
    first_inside, let_first_finish = threading.Event(), threading.Event()

    def steps(self):
        me = threading.current_thread().name
        events.append(f"enter {me}")
        if me == "first":
            first_inside.set()
            let_first_finish.wait(10)
        events.append(f"exit {me}")

    def run():
        try:
            call_command("migrate_locked")
        except Exception as exc:  # surfaced through the assert below
            errors.append(exc)
        finally:
            connections.close_all()

    with patch(STEPS, steps):
        first = threading.Thread(target=run, name="first")
        second = threading.Thread(target=run, name="second")
        first.start()
        assert first_inside.wait(10)
        second.start()
        # Give the second run ample time to (wrongly) get in.
        second.join(1.5)
        assert second.is_alive()
        assert events == ["enter first"]
        let_first_finish.set()
        first.join(10)
        second.join(10)

    assert errors == []
    assert events == ["enter first", "exit first", "enter second", "exit second"]


@pytest.mark.django_db
def test_timeout_fails_loudly_without_running_steps(other_instance, monkeypatch):
    monkeypatch.setenv("MIGRATE_LOCK_TIMEOUT", "1")
    assert _scalar(other_instance, "SELECT GET_LOCK(%s, 0)") == 1
    with (
        patch(STEPS) as steps,
        pytest.raises(CommandError, match="Timed out after 1s waiting for"),
    ):
        call_command("migrate_locked")
    steps.assert_not_called()


@pytest.mark.django_db
def test_lock_is_released_when_a_step_raises(other_instance):
    with (
        patch(STEPS, side_effect=RuntimeError("migration blew up")),
        pytest.raises(RuntimeError, match="migration blew up"),
    ):
        call_command("migrate_locked")
    assert _scalar(other_instance, "SELECT IS_FREE_LOCK(%s)") == 1


@pytest.mark.django_db
def test_lock_is_released_on_success(other_instance):
    with patch(STEPS):
        call_command("migrate_locked")
    assert _scalar(other_instance, "SELECT IS_FREE_LOCK(%s)") == 1


def test_steps_match_the_old_entrypoint(monkeypatch):
    monkeypatch.delenv("DJANGO_SUPERUSER_PASSWORD", raising=False)
    with patch.object(migrate_locked, "call_command") as cc:
        migrate_locked.Command().run_steps()
    assert cc.call_args_list == [
        call("migrate", interactive=False),
        call("load_bots"),
    ]


def test_superuser_step_only_with_password_and_never_fatal(monkeypatch):
    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "x")
    monkeypatch.delenv("DJANGO_SUPERUSER_USERNAME", raising=False)
    monkeypatch.delenv("DJANGO_SUPERUSER_EMAIL", raising=False)

    def fake(name, **kwargs):
        if name == "createsuperuser":
            # What Django raises on every boot after the first.
            raise CommandError("Error: That username is already taken.")

    with patch.object(migrate_locked, "call_command", side_effect=fake) as cc:
        migrate_locked.Command().run_steps()
    assert cc.call_args_list[-1] == call(
        "createsuperuser",
        interactive=False,
        username="admin",
        email="admin@example.com",
    )
