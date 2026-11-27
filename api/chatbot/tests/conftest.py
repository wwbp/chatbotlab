"""
Shared pytest fixtures for all chatbot tests.

Conventions:
  - `db_models`: autouse — ensures default AI models exist in test DB.
  - `mock_llm`: opt-in — patches engine factory + Kani so no real API calls are made.
  - `make_bot` / `make_conversation`: lightweight factory fixtures.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chatbot.models import Bot, Conversation, Model


@pytest.fixture
def db_models(db):
    """Ensure default providers and models exist. Request this explicitly in tests that need it."""
    Model.get_or_create_default_models()


@pytest.fixture
def openai_model(db_models):
    return Model.objects.get(provider__name="OpenAI", model_id="gpt-4o-mini")


@pytest.fixture
def make_bot(openai_model):
    """Factory: create a minimal Bot. Keyword args override defaults."""
    created = []

    def _make(**kwargs):
        defaults = {
            "name": f"bot_{uuid.uuid4().hex[:8]}",
            "prompt": "You are a test assistant.",
            "ai_model": openai_model,
        }
        defaults.update(kwargs)
        bot = Bot.objects.create(**defaults)
        created.append(bot)
        return bot

    yield _make

    for b in created:
        # A bot with conversations can't be deleted (Bot.delete); tests that
        # start conversations through the views leave some behind.
        Conversation.objects.filter(bot_name=b.name).delete()
        b.delete()


@pytest.fixture
def make_conversation(make_bot):
    """Factory: create a Conversation (creates a bot if not provided)."""
    created = []

    def _make(bot=None, **kwargs):
        if bot is None:
            bot = make_bot()
        defaults = {
            "conversation_id": f"conv_{uuid.uuid4().hex[:8]}",
            "bot_name": bot.name,
            "participant_id": "p001",
        }
        defaults.update(kwargs)
        conv = Conversation.objects.create(**defaults)
        created.append(conv)
        return conv

    yield _make

    for c in created:
        c.delete()


@pytest.fixture
def mock_llm():
    """
    Patches the LLM engine factory and Kani class so no real API calls are made.

    Usage:
        def test_something(mock_llm):
            mock_llm.response = "Custom reply."
            # run chat round ...

    The fixture yields an object with a `response` attribute you can set.
    """
    controller = MagicMock()
    controller.response = "Mock LLM response."

    mock_kani = AsyncMock()

    async def _full_round(query, **kwargs):
        msg = MagicMock()
        msg.text = controller.response
        yield msg

    mock_kani.full_round = _full_round

    with (
        patch(
            "chatbot.services.runchat.get_or_create_engine_from_model",
            return_value=MagicMock(),
        ),
        patch("chatbot.services.runchat.Kani", return_value=mock_kani),
    ):
        yield controller
