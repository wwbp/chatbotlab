"""
Tests for ChatbotAPIView  (POST /api/chatbot/)

Coverage:
  - Input validation (missing fields → 400)
  - Happy path: response shape, bot_config fields forwarded
  - chunk_messages=False returns single chunk
  - Utterances saved to DB after a successful round
  - No double bot fetch regression: view must not re-query Bot after run_chat_round
  - Moderation blocked: warning text returned, both utterances saved to DB
  - Moderation blocked: category recorded on both rows, never leaked to the client
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.test import AsyncClient

from chatbot.models import Bot, Conversation, Model, Utterance
from chatbot.services.moderation import ModerationResult

URL = "/api/chatbot/"


def _async_full_round(text="Mock reply."):
    async def _inner(*args, **kwargs):
        msg = MagicMock()
        msg.text = text
        yield msg

    return _inner


def _mock_kani(text="Mock reply."):
    k = MagicMock()
    k.full_round = _async_full_round(text)
    return k


class TestChatbotView:
    """Tests for POST /api/chatbot/ — uses class setUp/tearDown via sync_to_async
    to match the pattern used by other async tests in this codebase."""

    def setUp(self, bot_kwargs=None):
        Model.get_or_create_default_models()
        self.model = Model.objects.get(provider__name="OpenAI", model_id="gpt-4o-mini")
        kwargs = {
            "name": f"bot_{uuid.uuid4().hex[:8]}",
            "prompt": "You are a test assistant.",
            "ai_model": self.model,
            "chunk_messages": True,
            "humanlike_delay": True,
        }
        if bot_kwargs:
            kwargs.update(bot_kwargs)
        self.bot = Bot.objects.create(**kwargs)
        self.conv = Conversation.objects.create(
            conversation_id=f"conv_{uuid.uuid4().hex[:8]}",
            bot_name=self.bot.name,
            participant_id="p_test",
        )

    def tearDown(self):
        Conversation.objects.filter(pk=self.conv.pk).delete()
        Bot.objects.filter(pk=self.bot.pk).delete()

    def _post(self, client, message="Hello", **extra):
        import json

        payload = {
            "bot_name": self.bot.name,
            "conversation_id": self.conv.conversation_id,
            "participant_id": "p_test",
            "message": message,
            **extra,
        }
        return client.post(
            URL, data=json.dumps(payload), content_type="application/json"
        )

    # ── Input validation ──────────────────────────────────────────────────────

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_missing_message_returns_400(self):
        await sync_to_async(self.setUp)()
        try:
            import json

            client = AsyncClient()
            r = await client.post(
                URL,
                data=json.dumps(
                    {
                        "bot_name": self.bot.name,
                        "conversation_id": self.conv.conversation_id,
                    }
                ),
                content_type="application/json",
            )
            assert r.status_code == 400
            assert "error" in r.json()
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_missing_bot_name_returns_400(self):
        await sync_to_async(self.setUp)()
        try:
            import json

            client = AsyncClient()
            r = await client.post(
                URL,
                data=json.dumps(
                    {"message": "Hi", "conversation_id": self.conv.conversation_id}
                ),
                content_type="application/json",
            )
            assert r.status_code == 400
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_missing_conversation_id_returns_400(self):
        await sync_to_async(self.setUp)()
        try:
            import json

            client = AsyncClient()
            r = await client.post(
                URL,
                data=json.dumps({"message": "Hi", "bot_name": self.bot.name}),
                content_type="application/json",
            )
            assert r.status_code == 400
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_nonexistent_conversation_returns_400(self):
        # A chat for a conversation_id with no DB row must return a clean 400
        # (client never initialized it), not a generic 500.
        await sync_to_async(self.setUp)()
        try:
            import json

            client = AsyncClient()
            with patch(
                "chatbot.services.runchat.moderate_message",
                return_value=ModerationResult(),
            ):
                r = await client.post(
                    URL,
                    data=json.dumps(
                        {
                            "message": "Hi",
                            "bot_name": self.bot.name,
                            "conversation_id": "does-not-exist-xyz",
                            "participant_id": "p_test",
                        }
                    ),
                    content_type="application/json",
                )
            assert r.status_code == 400
            assert "not found" in r.json()["error"].lower()
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_nonexistent_bot_returns_404(self):
        # A chat naming a bot with no DB row must return a clean 404 — the same
        # status /api/initialize_conversation/ gives for this condition — not a
        # generic 500. Regression: Bot.DoesNotExist used to fall through to the
        # catch-all handler, so any mistyped bot_name looked like a server fault.
        await sync_to_async(self.setUp)()
        try:
            import json

            client = AsyncClient()
            with patch(
                "chatbot.services.runchat.moderate_message",
                return_value=ModerationResult(),
            ):
                r = await client.post(
                    URL,
                    data=json.dumps(
                        {
                            "message": "Hi",
                            "bot_name": "no-such-bot-xyz",
                            "conversation_id": self.conv.conversation_id,
                            "participant_id": "p_test",
                        }
                    ),
                    content_type="application/json",
                )
            assert r.status_code == 404
            assert "no-such-bot-xyz" in r.json()["error"]
        finally:
            await sync_to_async(self.tearDown)()

    # ── Happy path ────────────────────────────────────────────────────────────

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_chat_returns_200_with_expected_fields(self):
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch(
                    "chatbot.services.runchat.Kani", return_value=_mock_kani("Great!")
                ),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(),
                ),
            ):
                r = await self._post(client)

            assert r.status_code == 200
            data = r.json()
            for field in (
                "response",
                "response_chunks",
                "humanlike_delay",
                "chunk_messages",
                "delay_config",
            ):
                assert field in data, f"missing field: {field}"
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_response_text_matches_llm_output(self):
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch("chatbot.services.runchat._MOCK_LLM", False),
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch(
                    "chatbot.services.runchat.Kani",
                    return_value=_mock_kani("Specific reply."),
                ),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(),
                ),
            ):
                r = await self._post(client, message="Tell me something")

            assert r.json()["response"] == "Specific reply."
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_delay_config_shape(self):
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch("chatbot.services.runchat.Kani", return_value=_mock_kani()),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(),
                ),
            ):
                r = await self._post(client)

            dc = r.json()["delay_config"]
            assert "reading_time" in dc
            assert "min_reading_delay" in dc
            assert isinstance(dc["response_segments"], list)
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_chunk_messages_false_returns_single_chunk(self):
        await sync_to_async(self.setUp)(
            bot_kwargs={"chunk_messages": False, "humanlike_delay": False}
        )
        try:
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch(
                    "chatbot.services.runchat.Kani",
                    return_value=_mock_kani("One. Two. Three."),
                ),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(),
                ),
            ):
                r = await self._post(client)

            data = r.json()
            assert data["chunk_messages"] is False
            assert len(data["response_chunks"]) == 1
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_utterances_saved_to_db(self):
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch("chatbot.services.runchat._MOCK_LLM", False),
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch(
                    "chatbot.services.runchat.Kani",
                    return_value=_mock_kani("Bot reply."),
                ),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(),
                ),
            ):
                await self._post(client, message="User msg")

            utterances = await sync_to_async(list)(
                Utterance.objects.filter(conversation=self.conv).order_by(
                    "created_time"
                )
            )
            assert len(utterances) == 2
            assert utterances[0].speaker_id == "user"
            assert utterances[0].text == "User msg"
            assert utterances[1].speaker_id == "assistant"
            assert utterances[1].text == "Bot reply."
            # An unmoderated round leaves the moderation columns empty.
            assert utterances[0].moderation_category is None
            assert utterances[1].moderation_category is None
        finally:
            await sync_to_async(self.tearDown)()

    # ── No double bot fetch ───────────────────────────────────────────────────

    def test_views_does_not_import_bot_model(self):
        """Regression: views.py must not import Bot — it gets it from run_chat_round's return value."""
        import chatbot.views as views_module

        assert not hasattr(views_module, "Bot"), (
            "chatbot.views imported Bot — double fetch regression: "
            "run_chat_round already returns the bot object"
        )

    # ── Moderation blocked path ───────────────────────────────────────────────

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_moderation_blocked_returns_warning_text(self):
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch("chatbot.services.runchat.Kani"),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(category="harassment"),
                ),
            ):
                r = await self._post(client, message="Bad message")

            assert r.status_code == 200
            assert "could not be processed" in r.json()["response"]
            # The tripped category must never reach the client.
            assert "harassment" not in r.json()["response"]
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_moderation_blocked_saves_both_utterances(self):
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch("chatbot.services.runchat.Kani"),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(category="harassment"),
                ),
            ):
                await self._post(client, message="Bad message")

            utterances = await sync_to_async(list)(
                Utterance.objects.filter(conversation=self.conv).order_by(
                    "created_time"
                )
            )
            assert len(utterances) == 2
            assert utterances[0].speaker_id == "user"
            assert utterances[0].text == "Bad message"
            assert utterances[1].speaker_id == "assistant"
            assert "could not be processed" in utterances[1].text
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_moderation_blocked_records_category_on_both_rows(self):
        """Both rows of a blocked exchange carry the category, so one filter
        returns the complete event without relying on row adjacency."""
        await sync_to_async(self.setUp)()
        try:
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch("chatbot.services.runchat.Kani"),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(category="self-harm/intent"),
                ),
            ):
                await self._post(client, message="Bad message")

            utterances = await sync_to_async(list)(
                Utterance.objects.filter(
                    conversation=self.conv, moderation_category__isnull=False
                ).order_by("created_time")
            )
            assert len(utterances) == 2
            assert [u.speaker_id for u in utterances] == ["user", "assistant"]
            assert all(u.moderation_category == "self-harm/intent" for u in utterances)
        finally:
            await sync_to_async(self.tearDown)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    async def test_moderation_blocked_records_scores_on_user_row_only(self):
        """Scores describe the message that was scored, so they go on the user
        row; duplicating the blob onto the canned warning adds no information."""
        await sync_to_async(self.setUp)()
        try:
            scores = {"harassment": 0.91, "hate": 0.02, "violence": 0.4}
            client = AsyncClient()
            with (
                patch(
                    "chatbot.services.runchat.get_or_create_engine_from_model",
                    return_value=MagicMock(),
                ),
                patch("chatbot.services.runchat.Kani"),
                patch(
                    "chatbot.services.runchat.moderate_message",
                    return_value=ModerationResult(category="harassment", scores=scores),
                ),
            ):
                await self._post(client, message="Bad message")

            utterances = await sync_to_async(list)(
                Utterance.objects.filter(conversation=self.conv).order_by(
                    "created_time"
                )
            )
            user_row, assistant_row = utterances
            assert user_row.moderation_scores == scores
            assert assistant_row.moderation_scores is None
        finally:
            await sync_to_async(self.tearDown)()
