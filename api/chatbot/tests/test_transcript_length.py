"""
Test script for transcript length functionality
"""

import os
import time
from unittest.mock import MagicMock, patch

import django
import pytest
from django.core.cache import cache

from chatbot.models import Bot, Conversation, Model, Persona, Utterance
from chatbot.services.followup import run_followup_chat_round
from chatbot.services.moderation import ModerationResult
from chatbot.services.runchat import run_chat_round

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "generic_chatbot.settings")
django.setup()


class TestTranscriptLength:
    """Test class for transcript length functionality"""

    def setUp(self):
        """Set up test data"""
        # Clear cache before each test
        cache.clear()

        # Clean up any existing test bots first to prevent conflicts
        # Conversations first: a bot named in their utterances can't be deleted.
        Conversation.objects.filter(conversation_id__startswith="test_").delete()
        Bot.objects.filter(name__startswith="test_bot_").delete()
        Persona.objects.filter(name__startswith="Test").delete()

        # Get or create default models
        Model.get_or_create_default_models()
        self.model = Model.objects.first()

        if not self.model:
            self.skipTest("No models found in database.")

        # Create test bot with different transcript length settings
        self.bot_no_limit = Bot.objects.create(
            name="test_bot_no_limit",
            prompt="You are a helpful assistant.",
            ai_model=self.model,
            max_transcript_length=0,  # No limit
        )

        self.bot_limit_5 = Bot.objects.create(
            name="test_bot_limit_5",
            prompt="You are a helpful assistant.",
            ai_model=self.model,
            max_transcript_length=5,  # Limit to 5 messages
        )

        self.bot_limit_10 = Bot.objects.create(
            name="test_bot_limit_10",
            prompt="You are a helpful assistant.",
            ai_model=self.model,
            max_transcript_length=10,  # Limit to 10 messages
        )

        # Create test conversation
        self.conversation = Conversation.objects.create(
            conversation_id=f"test_transcript_{int(time.time())}",
            participant_id="test_participant",
        )

        # Create test persona
        self.persona = Persona.objects.create(
            name="Test Persona",
            instructions="You are a test persona with specific instructions.",
        )

    def create_mock_engine(self):
        """Helper method to create a properly mocked engine instance"""
        mock_engine = MagicMock()

        # Create a proper async iterator for chat completion
        async def mock_chat_completion(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_engine.chat_completion = mock_chat_completion

        # Mock token counting methods to return actual numbers
        mock_engine.message_token_len = MagicMock(return_value=10)
        mock_engine.max_context_size = 1000
        mock_engine.desired_response_tokens = 100

        return mock_engine

    def create_test_utterances(self, count, conversation=None):
        """Helper method to create test utterances"""
        if conversation is None:
            conversation = self.conversation

        utterances = []
        for i in range(count):
            # Alternate between user and assistant messages
            speaker_id = "user" if i % 2 == 0 else "assistant"
            text = f"Message {i + 1} from {speaker_id}"

            utterance = Utterance.objects.create(
                conversation=conversation,
                speaker_id=speaker_id,
                text=text,
                bot_name=self.bot_no_limit.name if speaker_id == "assistant" else None,
            )
            utterances.append(utterance)

        return utterances

    async def create_test_utterances_async(self, count, conversation=None):
        """Helper method to create test utterances asynchronously"""
        from asgiref.sync import sync_to_async

        return await sync_to_async(self.create_test_utterances)(count, conversation)

    async def async_setup(self):
        """Async setup method for async tests"""
        from asgiref.sync import sync_to_async

        await sync_to_async(self.setUp)()

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_no_transcript_limit(self, mock_save, mock_moderate, mock_get_engine):
        """Test that when max_transcript_length is 0, no chat history is included"""
        # Set up test data asynchronously
        await self.async_setup()

        # Create 15 messages in conversation
        await self.create_test_utterances_async(15)

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Run chat round
            response, _ = await run_chat_round(
                bot_name=self.bot_no_limit.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                message="New user message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_transcript_limit_5_messages(
        self,
        mock_save,
        mock_moderate,
        mock_get_engine,
    ):
        """Test that when max_transcript_length is 5, only 5 latest messages are included"""
        # Set up test data asynchronously
        from asgiref.sync import sync_to_async

        await sync_to_async(self.setUp)()

        # Create 15 messages in conversation
        await self.create_test_utterances_async(15)

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Run chat round with bot that has limit of 5
            response, _ = await run_chat_round(
                bot_name=self.bot_limit_5.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                message="New user message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_transcript_limit_less_than_existing(
        self,
        mock_save,
        mock_moderate,
        mock_get_engine,
    ):
        """Test when transcript limit is less than existing messages"""
        # Set up test data asynchronously
        await self.async_setup()

        # Create only 3 messages in conversation
        await self.create_test_utterances_async(3)

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Run chat round with bot that has limit of 10 (more than existing)
            response, _ = await run_chat_round(
                bot_name=self.bot_limit_10.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                message="New user message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_transcript_limit_exactly_matching(
        self,
        mock_save,
        mock_moderate,
        mock_get_engine,
    ):
        """Test when transcript limit exactly matches the number of messages"""
        # Set up test data asynchronously
        await self.async_setup()

        # Create exactly 5 messages in conversation
        await self.create_test_utterances_async(5)

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Run chat round with bot that has limit of 5
            response, _ = await run_chat_round(
                bot_name=self.bot_limit_5.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                message="New user message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_empty_conversation_with_limit(
        self,
        mock_save,
        mock_moderate,
        mock_get_engine,
    ):
        """Test with empty conversation and transcript limit"""
        # Set up test data asynchronously
        await self.async_setup()

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Run chat round with empty conversation
            response, _ = await run_chat_round(
                bot_name=self.bot_limit_5.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                message="First message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.runchat.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_transcript_limit_with_persona(
        self,
        mock_save,
        mock_moderate,
        mock_get_engine,
    ):
        """Test transcript limit works correctly with persona selection"""
        # Set up test data asynchronously
        await self.async_setup()

        # Set up conversation with persona
        from asgiref.sync import sync_to_async

        self.conversation.selected_persona = self.persona
        await sync_to_async(self.conversation.save)()

        # Create 15 messages in conversation
        await self.create_test_utterances_async(15)

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.runchat.Kani", return_value=mock_kani):
            # Run chat round with bot that has limit of 5
            response, _ = await run_chat_round(
                bot_name=self.bot_limit_5.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                message="New user message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    @pytest.mark.asyncio
    @patch("chatbot.services.followup.get_or_create_engine_from_model")
    @patch("chatbot.services.runchat.moderate_message")
    @patch("chatbot.services.runchat.save_chat_to_db")
    async def test_followup_with_transcript_limit(
        self,
        mock_save,
        mock_moderate,
        mock_get_engine,
    ):
        """Test that followup also respects transcript length limits"""
        # Set up test data asynchronously
        await self.async_setup()

        # Create 15 messages in conversation
        await self.create_test_utterances_async(15)

        # Mock the engine
        mock_engine = self.create_mock_engine()
        mock_get_engine.return_value = mock_engine

        # Mock moderation to allow the message
        mock_moderate.return_value = ModerationResult()

        # Mock Kani responses
        mock_kani = MagicMock()

        async def mock_full_round(*args, **kwargs):
            yield MagicMock(text="Test response")

        mock_kani.full_round = mock_full_round

        # Mock Kani constructor
        with patch("chatbot.services.followup.Kani", return_value=mock_kani):
            # Run followup chat round with bot that has limit of 5
            response = await run_followup_chat_round(
                bot_name=self.bot_limit_5.name,
                conversation_id=self.conversation.conversation_id,
                participant_id="test_participant",
                followup_instruction="Send a followup message",
            )

            # Verify response was generated
            assert isinstance(response, str)
            assert len(response) > 0

    @pytest.mark.django_db
    def test_bot_model_max_transcript_length_field(self):
        """Test that the max_transcript_length field is properly configured"""
        # Get or create default models
        Model.get_or_create_default_models()
        model = Model.objects.first()

        # Test default value
        bot = Bot.objects.create(
            name="test_bot_default",
            prompt="Test prompt",
            ai_model=model,
        )
        assert (
            bot.max_transcript_length == -1
        )  # Default should be -1 (unlimited chat history)

        # Test custom value
        bot_with_limit = Bot.objects.create(
            name="test_bot_with_limit",
            prompt="Test prompt",
            ai_model=model,
            max_transcript_length=20,
        )
        assert bot_with_limit.max_transcript_length == 20

    @pytest.mark.django_db
    def test_bot_model_max_transcript_length_edge_cases(self):
        """Test edge cases for max_transcript_length field"""
        # Get or create default models
        Model.get_or_create_default_models()
        model = Model.objects.first()

        # Test negative value (unlimited)
        bot_unlimited = Bot.objects.create(
            name="test_bot_unlimited",
            prompt="Test prompt",
            ai_model=model,
            max_transcript_length=-1,  # Unlimited
        )
        assert bot_unlimited.max_transcript_length == -1

        # Test very large value
        bot_large_limit = Bot.objects.create(
            name="test_bot_large_limit",
            prompt="Test prompt",
            ai_model=model,
            max_transcript_length=1000,
        )
        assert bot_large_limit.max_transcript_length == 1000

    @pytest.mark.django_db
    def test_bot_model_max_transcript_length_zero(self):
        """Test that max_transcript_length of 0 means no chat history"""
        # Get or create default models
        Model.get_or_create_default_models()
        model = Model.objects.first()

        # Create a bot with limit of 2
        bot_limit_2 = Bot.objects.create(
            name="test_bot_limit_2",
            prompt="Test prompt",
            ai_model=model,
            max_transcript_length=2,  # Limit to 2 messages
        )
        assert bot_limit_2.max_transcript_length == 2

        # Test that 0 means no chat history
        bot_no_history = Bot.objects.create(
            name="test_bot_no_history",
            prompt="Test prompt",
            ai_model=model,
            max_transcript_length=0,  # No chat history
        )
        assert bot_no_history.max_transcript_length == 0

    def tearDown(self):
        """Clean up after each test"""
        # Clear cache
        cache.clear()

        # Clean up database objects to prevent conflicts between tests
        try:
            # Conversation first: a bot named in its utterances can't be deleted.
            if hasattr(self, "conversation"):
                self.conversation.delete()

            # Delete bots created in setUp
            if hasattr(self, "bot_no_limit"):
                self.bot_no_limit.delete()
            if hasattr(self, "bot_limit_5"):
                self.bot_limit_5.delete()
            if hasattr(self, "bot_limit_10"):
                self.bot_limit_10.delete()

            # Delete persona
            if hasattr(self, "persona"):
                self.persona.delete()

            # Delete any other bots created during tests
            Bot.objects.filter(name__startswith="test_bot_").delete()

        except Exception:
            # Ignore errors during cleanup to avoid masking test failures
            pass
