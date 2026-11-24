from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from chatbot.models import AccessLog


class Command(BaseCommand):
    help = "Delete AccessLog rows older than a retention period"

    def add_arguments(self, parser):
        # Required, no default: how long to keep the log is an IRB decision.
        parser.add_argument("--older-than-days", type=int, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, older_than_days, dry_run, **kwargs):
        cutoff = timezone.now() - timedelta(days=older_than_days)
        rows = AccessLog.objects.filter(timestamp__lt=cutoff)
        if dry_run:
            self.stdout.write(f"Would delete {rows.count()} rows before {cutoff}.")
            return
        deleted, _ = rows.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {deleted} rows before {cutoff}.")
        )
