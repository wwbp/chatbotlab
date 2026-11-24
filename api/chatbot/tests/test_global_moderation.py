from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from ..admin import UtteranceAdmin
from ..models import Bot, Model, ModelProvider, ModerationSettings, Utterance
from ..services.moderation import is_moderation_enabled, moderate_message


class TestGlobalModeration(TestCase):
    """Test global moderation enable/disable functionality."""

    def setUp(self):
        """Set up test data."""
        # Create default models
        Model.get_or_create_default_models()
        self.provider = ModelProvider.objects.get(name="OpenAI")
        self.model = Model.objects.filter(provider=self.provider).first()

        # Create a test bot
        self.bot = Bot.objects.create(
            name="test_moderation_bot",
            prompt="Test bot for moderation",
            ai_model=self.model,
        )

    def tearDown(self):
        """Clean up test data."""
        # Clean up moderation settings
        ModerationSettings.objects.all().delete()

    @patch("chatbot.services.moderation.OpenAI")
    def test_moderation_disabled_bypasses_api_call(self, mock_openai):
        """Test that when global moderation is disabled, OpenAI API is not called."""
        # Setup: Disable global moderation
        ModerationSettings.objects.create(enabled=False)

        # Create mock OpenAI instance
        mock_openai_instance = MagicMock()
        mock_openai.return_value = mock_openai_instance

        # Test message that would normally be blocked
        test_message = "This is a test message"

        # Call moderate_message
        result = moderate_message(test_message, self.bot)

        # Assertions
        assert result.category is None  # Not blocked
        assert result.scores is None  # API never called, so nothing was scored
        mock_openai_instance.moderations.create.assert_not_called()  # API should not be called

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("chatbot.services.moderation._MOCK_LLM", False)
    @patch("chatbot.services.moderation.OpenAI")
    def test_moderation_enabled_calls_api(self, mock_openai):
        """Test that when global moderation is enabled, OpenAI API is called."""
        # Setup: Enable global moderation
        ModerationSettings.objects.create(enabled=True)

        # Create mock OpenAI instance and response
        mock_openai_instance = MagicMock()
        mock_openai.return_value = mock_openai_instance

        # Mock the moderation response (no violations)
        mock_response = MagicMock()
        mock_result = MagicMock()
        mock_result.category_scores = None
        mock_response.results = [mock_result]
        mock_openai_instance.moderations.create.return_value = mock_response

        # Mock model_dump to return empty dict (no violations)
        with patch("chatbot.services.moderation.model_dump") as mock_model_dump:
            mock_model_dump.return_value = {}

            # Test message
            test_message = "This is a test message"

            # Call moderate_message
            result = moderate_message(test_message, self.bot)

            # Assertions
            assert result.category is None  # Not blocked
            assert result.scores == {}  # API was called, so scores are populated
            mock_openai_instance.moderations.create.assert_called_once()  # API should be called

    def test_is_moderation_enabled_helper_function(self):
        """Test the is_moderation_enabled helper function."""
        # Test when no settings exist (should default to True)
        assert is_moderation_enabled()

        # Test when enabled
        ModerationSettings.objects.create(enabled=True)
        assert is_moderation_enabled()

        # Test when disabled
        ModerationSettings.objects.all().delete()
        ModerationSettings.objects.create(enabled=False)
        assert not is_moderation_enabled()

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("chatbot.services.moderation._MOCK_LLM", False)
    @patch("chatbot.services.moderation.OpenAI")
    def test_moderation_enabled_with_violation_blocks_message(self, mock_openai):
        """Test that when moderation is enabled and violation detected, message is blocked."""
        # Setup: Enable global moderation
        ModerationSettings.objects.create(enabled=True)

        # Create mock OpenAI instance and response
        mock_openai_instance = MagicMock()
        mock_openai.return_value = mock_openai_instance

        # Mock the moderation response (harassment violation)
        mock_response = MagicMock()
        mock_result = MagicMock()
        mock_result.category_scores = None
        mock_response.results = [mock_result]
        mock_openai_instance.moderations.create.return_value = mock_response

        # Mock model_dump to return our test data
        with patch("chatbot.services.moderation.model_dump") as mock_model_dump:
            mock_model_dump.return_value = {"harassment": 0.8}  # Above threshold

            # Test message
            test_message = "This message has harassment content"

            # Call moderate_message
            result = moderate_message(test_message, self.bot)

            # Assertions
            assert result.category == "harassment"  # Should return violation category
            assert result.scores == {"harassment": 0.8}
            mock_openai_instance.moderations.create.assert_called_once()  # API should be called

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("chatbot.services.moderation._MOCK_LLM", False)
    @patch("chatbot.services.moderation.OpenAI")
    def test_blocked_result_keeps_every_category_score(self, mock_openai):
        """The full score map is preserved, not just the category that tripped —
        without the passing scores the thresholds cannot be re-tuned later."""
        ModerationSettings.objects.create(enabled=True)

        mock_openai_instance = MagicMock()
        mock_openai.return_value = mock_openai_instance
        mock_response = MagicMock()
        mock_result = MagicMock()
        mock_result.category_scores = None
        mock_response.results = [mock_result]
        mock_openai_instance.moderations.create.return_value = mock_response

        all_scores = {
            "harassment": 0.9,  # above the 0.5 default → blocks
            "hate": 0.01,
            "violence": 0.42,
            "sexual": 0.0,
        }
        with patch("chatbot.services.moderation.model_dump") as mock_model_dump:
            mock_model_dump.return_value = all_scores

            result = moderate_message("nasty message", self.bot)

            assert result.category == "harassment"
            assert result.scores == all_scores

    @override_settings(OPENAI_API_KEY="test-key")
    @patch("chatbot.services.moderation._MOCK_LLM", False)
    @patch("chatbot.services.moderation.OpenAI")
    def test_none_scores_are_skipped_without_blocking(self, mock_openai):
        """A null score must not be compared against a threshold."""
        ModerationSettings.objects.create(enabled=True)

        mock_openai_instance = MagicMock()
        mock_openai.return_value = mock_openai_instance
        mock_response = MagicMock()
        mock_result = MagicMock()
        mock_result.category_scores = None
        mock_response.results = [mock_result]
        mock_openai_instance.moderations.create.return_value = mock_response

        with patch("chatbot.services.moderation.model_dump") as mock_model_dump:
            mock_model_dump.return_value = {"harassment": None, "hate": 0.01}

            result = moderate_message("harmless message", self.bot)

            assert result.category is None
            assert result.scores == {"harassment": None, "hate": 0.01}


class TestModerationAdminSurface(TestCase):
    """The admin is how researchers actually reach this data — a field that is
    not filterable or on the change form is effectively invisible to them."""

    def setUp(self):
        self.admin = UtteranceAdmin(Utterance, None)

    def test_category_is_listed_and_filterable(self):
        assert "moderation_category" in self.admin.list_display
        assert any(
            getattr(f, "parameter_name", None) == "moderation_category"
            for f in self.admin.list_filter
        )

    def test_both_fields_appear_on_the_change_form(self):
        fields = {
            field for _, options in self.admin.fieldsets for field in options["fields"]
        }
        assert "moderation_category" in fields
        assert "moderation_scores" in fields

    def test_moderation_fields_are_readonly(self):
        """They are recorded by the moderation path; editing them by hand would
        corrupt the record of what actually happened."""
        assert "moderation_category" in self.admin.readonly_fields
        assert "moderation_scores" in self.admin.readonly_fields

    def test_export_resource_includes_moderation_category(self):
        """The export lists its columns explicitly; the category is one of them.
        The score map stays on the view page (it is only on blocked rows)."""
        exported = self.admin.resource_class().get_export_headers()
        assert "moderation_category" in exported
