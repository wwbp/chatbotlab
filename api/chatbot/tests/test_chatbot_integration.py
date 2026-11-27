import asyncio
from unittest.mock import MagicMock, patch

from asgiref.sync import sync_to_async
from django.test import TestCase

from chatbot.models import Bot, Conversation, Model, ModelProvider, Utterance
from chatbot.services.moderation import ModerationResult
from chatbot.services.runchat import run_chat_round


class TestChatbotIntegration(TestCase):
    """Minimal integration test to verify chatbot conversation flow works"""

    def setUp(self):
        """Set up test data"""
        # Get or create default models
        Model.get_or_create_default_models()

        # Get the OpenAI provider
        self.provider = ModelProvider.objects.get(name="OpenAI")

        # Get an existing model for testing (preferably GPT-4o Mini)
        self.model = (
            Model.objects.filter(
                provider=self.provider,
                model_id="gpt-4o-mini",
            ).first()
            or Model.objects.filter(provider=self.provider).first()
        )

        import time

        timestamp = int(time.time() * 1000)

        # Create a bot with the new model structure
        self.bot = Bot.objects.create(
            name=f"test_integration_bot_{timestamp}",
            prompt="You are a helpful assistant. Keep responses brief and friendly.",
            ai_model=self.model,
            max_transcript_length=2,  # Keep some chat history for testing
        )

        # Create a conversation
        self.conversation = Conversation.objects.create(
            conversation_id=f"test_integration_conversation_{timestamp}",
            bot_name=self.bot.name,
            participant_id="test_user",
        )

    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    async def test_basic_conversation_flow(self, mock_get_engine, mock_moderate):
        """Test basic conversation flow with new model structure"""
        # Mock moderation to allow all messages
        mock_moderate.return_value = ModerationResult()

        # Mock engine
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        # Mock Kani responses
        mock_kani = MagicMock()

        # Create an async iterator for the full_round method
        async def mock_full_round(*args, **kwargs):
            yield MagicMock(
                text="Hello! I'm your helpful assistant. How can I help you today?",
            )

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Test 1: Initial message
            response1, _ = await run_chat_round(
                bot_name=self.bot.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_user",
                message="Hello, how are you?",
            )

            # Verify response
            assert isinstance(response1, str)
            assert len(response1) > 0

            # Test 2: Follow-up message (should include chat history)
            response2, _ = await run_chat_round(
                bot_name=self.bot.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_user",
                message="What's the weather like?",
            )

            # Verify response
            assert isinstance(response2, str)
            assert len(response2) > 0

            # Test 3: Verify database records were created
            utterances = await sync_to_async(list)(
                Utterance.objects.filter(
                    conversation=self.conversation,
                ).order_by("created_time"),
            )

            assert len(utterances) == 4  # 2 user messages + 2 bot responses

            # Verify user messages
            user_utterances = [u for u in utterances if u.speaker_id == "user"]
            assert len(user_utterances) == 2
            assert user_utterances[0].text == "Hello, how are you?"
            assert user_utterances[1].text == "What's the weather like?"

            # Verify bot messages (bot messages use 'assistant' as speaker_id)
            bot_utterances = [u for u in utterances if u.speaker_id == "assistant"]
            assert len(bot_utterances) == 2
            assert len(bot_utterances[0].text) > 0
            assert len(bot_utterances[1].text) > 0

            # Test 4: Verify bot model relationship
            def verify_bot_model_relationship():
                bot = Bot.objects.get(name=self.bot.name)
                assert bot.ai_model == self.model
                assert bot.ai_model.provider.name == "OpenAI"
                assert bot.ai_model.model_id == self.model.model_id

            await sync_to_async(verify_bot_model_relationship)()

    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    async def test_legacy_bot_compatibility(self, mock_get_engine, mock_moderate):
        """Test that legacy bots with old model fields still work"""
        # Mock moderation to allow all messages
        mock_moderate.return_value = ModerationResult()

        # Mock engine
        mock_engine = MagicMock()
        mock_get_engine.return_value = mock_engine

        # Mock Kani responses
        mock_kani = MagicMock()

        # Create an async iterator for the full_round method
        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Hello! I'm a legacy bot. How can I help you?")

        mock_kani.full_round = mock_full_round

        # Create a legacy bot with old model fields
        import time

        timestamp = int(time.time() * 1000)

        def create_legacy_bot():
            return Bot.objects.create(
                name=f"test_legacy_bot_{timestamp}",
                prompt="You are a legacy assistant.",
                model_type="OpenAI",
                model_id="gpt-3.5-turbo",
                ai_model=self.model,  # Also set the new field for compatibility
            )

        legacy_bot = await sync_to_async(create_legacy_bot)()

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            response, _ = await run_chat_round(
                bot_name=legacy_bot.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_user",
                message="Hello, legacy bot!",
            )

            # Verify response
            assert isinstance(response, str)
            assert len(response) > 0

            # Verify legacy bot has ai_model and legacy fields
            def verify_legacy_bot():
                bot = Bot.objects.get(name=legacy_bot.name)
                assert bot.ai_model is not None
                assert bot.model_type == "OpenAI"
                assert bot.model_id == "gpt-3.5-turbo"

            await sync_to_async(verify_legacy_bot)()

            # Clean up legacy bot (its utterances first, or it can't be deleted)
            await sync_to_async(
                Utterance.objects.filter(bot_name=legacy_bot.name).delete
            )()
            await sync_to_async(legacy_bot.delete)()

    def test_model_capabilities_integration(self):
        """Test that model capabilities are properly accessible"""
        # Test capabilities are stored and accessible
        assert isinstance(self.model.capabilities, list)
        assert len(self.model.capabilities) > 0
        assert "Chat" in self.model.capabilities

        # Test capabilities through bot relationship
        assert self.bot.ai_model.capabilities == self.model.capabilities

    def test_provider_model_bot_chain(self):
        """Test the complete chain from provider to model to bot"""
        # Test the complete chain
        provider_name = self.provider.name
        model_id = self.model.model_id
        bot_name = self.bot.name

        assert provider_name == "OpenAI"
        assert model_id == self.model.model_id  # Use actual model ID
        assert bot_name.startswith("test_integration_bot_")

        # Test reverse relationships
        provider_models = self.provider.models.all()
        assert provider_models.count() >= 1  # Should have at least our test model
        assert self.model in provider_models  # Our test model should be in the list

        model_bots = self.model.bots.all()
        assert model_bots.count() >= 1  # Should have at least our test bot
        assert self.bot in model_bots  # Our test bot should be in the list


def run_integration_tests():
    """Run the integration tests"""
    import os

    import django

    # Setup Django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "generic_chatbot.settings")
    django.setup()

    # Create test instance and run tests
    test_instance = TestChatbotIntegration()
    test_instance.setUp()

    # Run the tests
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Test model capabilities integration
        test_instance.test_model_capabilities_integration()

        # Test provider-model-bot chain
        test_instance.test_provider_model_bot_chain()

        # Test legacy bot compatibility
        loop.run_until_complete(test_instance.test_legacy_bot_compatibility())

        # Test basic conversation flow
        loop.run_until_complete(test_instance.test_basic_conversation_flow())

    finally:
        test_instance.tearDown()
        loop.close()


if __name__ == "__main__":
    run_integration_tests()
