"""
Deleting a model must never take bots (and their studies' configuration) with it.

Bot.ai_model used on_delete=CASCADE, so deleting one Model row deleted every
bot on it, and validate_bedrock_models deleted failing models with --delete,
and deactivated them by default. Now the database refuses to delete a model a
bot uses, and the command only reports unless told --apply.

Coverage:
  - deleting a model (or its provider) that a bot uses raises ProtectedError
  - validate_bedrock_models: report-only by default, --delete alone too
  - --apply deactivates failing models; --apply --delete deletes only models
    no bot uses, and refuses the rest
  - its probe sends no sampling params (newer Claude models reject them)
"""

import uuid
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from django.core.management import call_command
from django.db.models import ProtectedError

from chatbot.models import Bot, Model, ModelProvider

COMMAND = "chatbot.management.commands.validate_bedrock_models.boto3.client"


@pytest.fixture
def bedrock(db):
    provider, _ = ModelProvider.objects.get_or_create(
        name="Bedrock", defaults={"display_name": "Amazon Bedrock"}
    )
    Model.objects.filter(provider=provider).update(is_active=False)
    ok = Model.objects.create(provider=provider, model_id="ok-model", display_name="Ok")
    used = Model.objects.create(
        provider=provider, model_id="dead-used", display_name="Dead used"
    )
    unused = Model.objects.create(
        provider=provider, model_id="dead-unused", display_name="Dead unused"
    )
    bot = Bot.objects.create(
        name=f"b_{uuid.uuid4().hex[:6]}", prompt="p", ai_model=used
    )
    return {"ok": ok, "used": used, "unused": unused, "bot": bot}


def _run(*args):
    def converse(modelId, **kwargs):
        if modelId.startswith("dead"):
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "EOL"}},
                "Converse",
            )
        return {}

    with patch(COMMAND) as client:
        client.return_value.converse.side_effect = converse
        call_command("validate_bedrock_models", *args)
    return client.return_value.converse


def _state(models):
    return {
        k: Model.objects.filter(pk=m.pk).values_list("is_active", flat=True).first()
        for k, m in models.items()
        if k != "bot"
    }


def test_deleting_a_model_a_bot_uses_is_refused(bedrock):
    with pytest.raises(ProtectedError):
        bedrock["used"].delete()
    assert Bot.objects.filter(pk=bedrock["bot"].pk).exists()


def test_deleting_its_provider_is_refused_too(bedrock):
    with pytest.raises(ProtectedError):
        bedrock["used"].provider.delete()
    assert Bot.objects.filter(pk=bedrock["bot"].pk).exists()


@pytest.mark.parametrize("args", [(), ("--delete",), ("--dry-run",)])
def test_validate_is_report_only_without_apply(bedrock, args):
    before = _state(bedrock)
    _run(*args)
    assert _state(bedrock) == before


def test_validate_apply_deactivates_failing_models(bedrock):
    _run("--apply")
    assert _state(bedrock) == {"ok": True, "used": False, "unused": False}


def test_validate_apply_delete_spares_models_bots_use(bedrock, capsys):
    _run("--apply", "--delete")
    assert not Model.objects.filter(pk=bedrock["unused"].pk).exists()
    assert Model.objects.filter(pk=bedrock["used"].pk).exists()
    bedrock["bot"].refresh_from_db()
    assert bedrock["bot"].ai_model_id == bedrock["used"].pk
    assert "refused" in capsys.readouterr().out.lower()


def test_validate_probe_sends_no_sampling_params(bedrock):
    converse = _run()
    config = converse.call_args.kwargs["inferenceConfig"]
    assert "temperature" not in config
    assert "topP" not in config
