from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from chatbot.models import AccessLog


def _log(days_old):
    row = AccessLog.objects.create(username="r", action=AccessLog.Action.VIEW)
    # timestamp is auto_now_add, so backdate it with an update.
    AccessLog.objects.filter(pk=row.pk).update(
        timestamp=timezone.now() - timedelta(days=days_old)
    )
    return row


@pytest.mark.django_db
def test_prune_deletes_only_rows_older_than_the_period():
    old, recent = _log(400), _log(10)
    call_command("prune_access_log", "--older-than-days", "365")
    assert list(AccessLog.objects.values_list("pk", flat=True)) == [recent.pk]
    assert not AccessLog.objects.filter(pk=old.pk).exists()


@pytest.mark.django_db
def test_prune_dry_run_deletes_nothing():
    _log(400)
    call_command("prune_access_log", "--older-than-days", "365", "--dry-run")
    assert AccessLog.objects.count() == 1


def test_prune_requires_an_explicit_period():
    # Retention is an IRB decision, so there is deliberately no default.
    with pytest.raises(CommandError, match="--older-than-days"):
        call_command("prune_access_log")
