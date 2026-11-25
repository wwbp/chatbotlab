"""
check_models: one tiny real call per model, through the app's own engine path.

The command builds each engine with server.engine.initialize_engine_from_model
and sends one message with services.runchat.kani_reply, which is what
run_chat_round uses. Tests swap in fake engines: no network, no keys.

Also here: the curated models must work through that path, so the request the
app sends to each is checked (gpt-5 models reject max_tokens and non-default
temperature; Claude Opus 4.7 and the 5.5 models reject sampling params; newer
Claude models can return a reasoning block before the text).

Coverage:
  - every active model is called once and reported OK with latency
  - a failure (exception, or an empty reply) is reported and exits non-zero
  - --all includes retired models, whose failures don't fail the run
  - the command never writes to the database
  - the request the app sends gpt-5-mini / the Bedrock curated models
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from kani import ChatMessage
from kani.engines.base import BaseEngine, Completion
from openai.types.chat import ChatCompletion

from chatbot.engines.bedrock_engine import BedrockEngine
from chatbot.models import CURATED_MODELS, Model
from chatbot.services.runchat import kani_reply
from server.engine import initialize_engine_from_model

FACTORY = "chatbot.management.commands.check_models.initialize_engine_from_model"
BEDROCK_CURATED = [m["model_id"] for m in CURATED_MODELS["Bedrock"]]


class FakeEngine(BaseEngine):
    max_context_size = 10_000

    def __init__(self, reply="OK", error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def message_len(self, message):
        return 1

    async def prompt_len(self, messages, functions=None, **kwargs):
        return len(messages)

    async def predict(self, messages, functions=None, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return Completion(ChatMessage.assistant(self.reply))


def _run(engines, *args):
    """Run the command; engines maps model_id -> FakeEngine (default: OK)."""
    made = {}

    def factory(model):
        made[model.model_id] = engines.get(model.model_id) or FakeEngine()
        return made[model.model_id]

    with patch(FACTORY, side_effect=factory):
        try:
            call_command("check_models", *args)
            error = None
        except CommandError as e:
            error = e
    return made, error


def _active_ids():
    return set(
        Model.objects.filter(is_active=True, provider__is_active=True).values_list(
            "model_id", flat=True
        )
    )


@pytest.mark.django_db
def test_every_active_model_is_called_once_and_reported(capsys):
    made, error = _run({})
    out = capsys.readouterr().out
    assert error is None
    assert set(made) == _active_ids()
    assert all(engine.calls == 1 for engine in made.values())
    for model_id in made:
        line = next(line for line in out.splitlines() if model_id in line)
        assert "OK" in line
        assert "ms" in line


@pytest.mark.django_db
def test_a_failing_model_is_reported_and_exits_non_zero(capsys):
    _, error = _run({"gpt-4.1": FakeEngine(error=RuntimeError("boom\ntrace"))})
    out = capsys.readouterr().out
    assert error is not None
    line = next(line for line in out.splitlines() if "gpt-4.1" in line)
    assert "FAIL" in line
    assert "boom" in line
    assert "trace" not in out  # one short line per model


@pytest.mark.django_db
def test_an_empty_reply_is_a_failure(capsys):
    """A reasoning model that spends its output on thinking returns no text."""
    _, error = _run({"gpt-5-mini": FakeEngine(reply="")})
    assert error is not None
    line = next(ln for ln in capsys.readouterr().out.splitlines() if "gpt-5-mini" in ln)
    assert "FAIL" in line


@pytest.mark.django_db
def test_an_engine_that_cannot_be_built_is_a_failure(capsys):
    def factory(model):
        raise ValueError("Missing OPENAI_API_KEY.")

    with patch(FACTORY, side_effect=factory), pytest.raises(CommandError):
        call_command("check_models")
    assert "Missing OPENAI_API_KEY" in capsys.readouterr().out


@pytest.mark.django_db
def test_all_includes_retired_models_without_failing_on_them(capsys):
    made, error = _run({"gpt-4": FakeEngine(error=RuntimeError("deprecated"))}, "--all")
    assert "gpt-4" in made  # retired since 0048
    assert set(made) == set(Model.objects.values_list("model_id", flat=True))
    assert error is None


@pytest.mark.django_db
def test_never_writes_to_the_database():
    with CaptureQueriesContext(connection) as queries:
        _run({"gpt-4.1": FakeEngine(error=RuntimeError("x"))}, "--all")
    writes = [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes == []


# ── The request the app sends to the curated models ──────────────────────────


@pytest.mark.django_db
def test_gpt5_mini_request_carries_no_params_it_rejects():
    model = Model.objects.get(provider__name="OpenAI", model_id="gpt-5-mini")
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
        engine = initialize_engine_from_model(model)
    completion = ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-5-mini",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "OK"},
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
    )
    create = AsyncMock(return_value=completion)
    with patch.object(engine.client.chat.completions, "create", create):
        reply = async_to_sync(kani_reply)(engine, "Be brief.", [], "Say OK.")
    assert reply == "OK"
    sent = create.call_args.kwargs
    assert sent["model"] == "gpt-5-mini"
    for rejected in ("max_tokens", "temperature", "top_p"):
        assert rejected not in sent


@pytest.mark.parametrize(
    "model_id",
    [m for m in BEDROCK_CURATED if m != "us.anthropic.claude-sonnet-4-6"],
)
def test_bedrock_models_that_reject_sampling_params_get_none(model_id):
    config = BedrockEngine(model_id=model_id)._build_inference_config()  # noqa: SLF001
    assert "temperature" not in config
    assert "topP" not in config


@pytest.mark.parametrize("model_id", BEDROCK_CURATED)
def test_bedrock_curated_models_get_a_full_context_window(model_id):
    # The 8192 fallback would make kani drop chat history these models can hold.
    assert BedrockEngine(model_id=model_id).max_context_size >= 200_000


def test_bedrock_reply_skips_a_leading_reasoning_block():
    engine = BedrockEngine(model_id="us.anthropic.claude-opus-5-5")
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": ""}}},
                    {"text": "OK"},
                ],
            }
        }
    }
    with patch.object(engine, "_call_bedrock", return_value=response):
        reply = async_to_sync(kani_reply)(engine, "Be brief.", [], "Say OK.")
    assert reply == "OK"


def test_reasoning_only_reply_is_not_silently_empty():
    """Thinking can use up maxTokens; an empty reply must be an error, not saved."""
    engine = BedrockEngine(model_id="us.anthropic.claude-opus-5-5")
    response = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"reasoningContent": {"reasoningText": {"text": ""}}}],
            }
        }
    }
    with (
        patch.object(engine, "_call_bedrock", return_value=response),
        pytest.raises(RuntimeError, match="no text"),
    ):
        async_to_sync(kani_reply)(engine, "Be brief.", [], "Say OK.")


@pytest.mark.parametrize(
    "model_id",
    [
        "us.anthropic.claude-sonnet-4-6",
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
    ],
)
def test_older_bedrock_models_keep_their_request(model_id):
    """Frozen bots on older models must send exactly what they always sent."""
    config = BedrockEngine(model_id=model_id)._build_inference_config()  # noqa: SLF001
    assert config == {"maxTokens": 1000, "temperature": 0.7}
