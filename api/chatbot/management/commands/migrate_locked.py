import os

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections


def lock_name():
    # GET_LOCK names are server-wide, so scope the name to this database
    # (the test database, and other environments, can share a server).
    return f"{settings.DATABASES[DEFAULT_DB_ALIAS]['NAME']}.startup"


class Command(BaseCommand):
    help = (
        "Run the startup DB steps (migrate, load_bots, createsuperuser) under a "
        "database named lock, so instances booting together take turns."
    )

    def handle(self, *args, **kwargs):
        timeout = int(os.environ.get("MIGRATE_LOCK_TIMEOUT", "600"))
        name = lock_name()
        # The lock belongs to a DB session, so it gets a connection of its own:
        # if the steps' connection is recycled mid-migration the lock survives.
        # If this process dies, the server drops the session and frees the lock
        # itself, so a crashed instance cannot leave it stuck.
        conn = connections.create_connection(DEFAULT_DB_ALIAS)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT GET_LOCK(%s, %s)", [name, timeout])
                if cursor.fetchone()[0] != 1:
                    # Refuse to start: better a failed instance than one serving
                    # traffic against a schema another instance is still changing.
                    raise CommandError(
                        f"Timed out after {timeout}s waiting for database lock "
                        f"'{name}': another instance is still running startup "
                        "migrations. Not starting. (MIGRATE_LOCK_TIMEOUT sets "
                        "the wait.)"
                    )
                try:
                    self.run_steps()
                finally:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", [name])
        finally:
            conn.close()

    def run_steps(self):
        call_command("migrate", interactive=False)
        call_command("load_bots")
        if os.environ.get("DJANGO_SUPERUSER_PASSWORD"):
            try:
                call_command(
                    "createsuperuser",
                    interactive=False,
                    username=os.environ.get("DJANGO_SUPERUSER_USERNAME") or "admin",
                    email=os.environ.get("DJANGO_SUPERUSER_EMAIL")
                    or "admin@example.com",
                )
            except CommandError as exc:
                # Expected on every boot after the first: the user exists.
                self.stderr.write(f"createsuperuser skipped: {exc}")
