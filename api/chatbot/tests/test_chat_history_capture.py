"""
Test script for chat history capture functionality
"""

import json
import time

from django.test import TestCase

from chatbot.models import Bot, Conversation, Model, Utterance


class TestChatHistoryCapture(TestCase):
    """Test class for chat history capture functionality"""

    def setUp(self):
        """Set up test data"""
        # Get or create default models
        Model.get_or_create_default_models()
        self.model = Model.objects.first()

        if not self.model:
            self.skipTest("No models found in database.")

        # Create test bot
        self.bot = Bot.objects.create(
            name="test_bot_history",
            prompt="You are a helpful assistant.",
            ai_model=self.model,
            max_transcript_length=5,  # Limit to 5 messages
        )

        # Create test conversation
        self.conversation = Conversation.objects.create(
            conversation_id=f"test_history_{int(time.time())}",
            participant_id="test_participant",
        )

    def create_test_utterances(self, count):
        """Helper method to create test utterances"""
        utterances = []
        for i in range(count):
            speaker_id = "user" if i % 2 == 0 else "assistant"
            utterance = Utterance.objects.create(
                conversation=self.conversation,
                speaker_id=speaker_id,
                text=f"Message {i + 1} from {speaker_id}",
                bot_name=self.bot.name,
            )
            utterances.append(utterance)
        return utterances

    def test_chat_history_field_save(self):
        """Test that chat history field can be saved and retrieved"""
        # Create a test utterance with chat history
        test_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        utterance = Utterance.objects.create(
            conversation=self.conversation,
            speaker_id="assistant",
            text="I'm doing well, thanks!",
            bot_name=self.bot.name,
            chat_history_used=json.dumps(test_history, indent=2),
        )

        # Verify the field was saved
        assert utterance.chat_history_used == json.dumps(test_history, indent=2)

    def test_chat_history_json_format(self):
        """Test that chat history is stored in proper JSON format"""
        # Create a test utterance with chat history
        test_history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
        ]

        utterance = Utterance.objects.create(
            conversation=self.conversation,
            speaker_id="assistant",
            text="I'm doing well, thanks!",
            bot_name=self.bot.name,
            chat_history_used=json.dumps(test_history, indent=2),
        )

        # Verify the JSON can be parsed back
        parsed_history = json.loads(utterance.chat_history_used)
        assert len(parsed_history) == 3
        assert parsed_history[0]["role"] == "user"
        assert parsed_history[0]["content"] == "Hello"
        assert parsed_history[1]["role"] == "assistant"
        assert parsed_history[1]["content"] == "Hi there!"

    def test_chat_history_with_transcript_limit(self):
        """Test that chat history reflects the transcript limit"""
        # Create 10 messages in conversation
        self.create_test_utterances(10)

        # Simulate the conversation history that would be loaded
        conversation_history = []
        utterances = Utterance.objects.filter(conversation=self.conversation).order_by(
            "created_time",
        )

        for utterance in utterances:
            role = "user" if utterance.speaker_id == "user" else "assistant"
            conversation_history.append({"role": role, "content": utterance.text})

        # Add a new message
        conversation_history.append({"role": "user", "content": "New user message"})

        # Apply transcript limit (should keep only 5 messages)
        if self.bot.max_transcript_length > 0:
            conversation_history = conversation_history[
                -self.bot.max_transcript_length :
            ]

        # Create a test utterance with the limited history (excluding new user message)
        chat_history_used = conversation_history[:-1]  # Exclude the new user message
        test_utterance = Utterance.objects.create(
            conversation=self.conversation,
            speaker_id="assistant",
            text="Test response",
            bot_name=self.bot.name,
            chat_history_used=json.dumps(chat_history_used, indent=2),
        )

        # Verify the history was limited correctly
        parsed_history = json.loads(test_utterance.chat_history_used)
        assert (
            len(parsed_history) == 4
        )  # Should be limited to 4 messages (excluding new user message)

        # Should contain the most recent messages from history
        message_contents = [msg["content"] for msg in parsed_history]
        assert "Message 10 from assistant" in message_contents
        assert "Message 9 from user" in message_contents
        assert (
            "New user message" not in message_contents
        )  # New user message should not be in chat history

    def test_chat_history_with_zero_limit(self):
        """Test that chat history with zero limit only contains current message"""
        # Create a bot with zero limit
        bot_zero = Bot.objects.create(
            name="test_bot_zero",
            prompt="You are a helpful assistant.",
            ai_model=self.model,
            max_transcript_length=0,  # No chat history
        )

        # Create 5 messages in conversation
        self.create_test_utterances(5)

        # Simulate the conversation history
        conversation_history = []
        utterances = Utterance.objects.filter(conversation=self.conversation).order_by(
            "created_time",
        )

        for utterance in utterances:
            role = "user" if utterance.speaker_id == "user" else "assistant"
            conversation_history.append({"role": role, "content": utterance.text})

        # Add a new message
        conversation_history.append({"role": "user", "content": "New user message"})

        # Apply zero limit (should keep only current message)
        if bot_zero.max_transcript_length == 0:
            conversation_history = [conversation_history[-1]]  # Only the new message

        # Create a test utterance with the limited history (excluding new user message)
        chat_history_used = conversation_history[:-1]  # Exclude the new user message
        test_utterance = Utterance.objects.create(
            conversation=self.conversation,
            speaker_id="assistant",
            text="Test response",
            bot_name=bot_zero.name,
            chat_history_used=json.dumps(chat_history_used, indent=2),
        )

        # Verify the history was limited correctly
        parsed_history = json.loads(test_utterance.chat_history_used)
        assert len(parsed_history) == 0  # Should be 0 messages (no chat history)
        # No messages should be in chat history since new user message is passed separately
