"""
Comprehensive engine testing - minimal, DRY, focused.
Tests engine initialization, real API calls, and engine agnosticism.
"""

import os

import pytest
from kani import Kani
from kani.engines.anthropic import AnthropicEngine
from kani.engines.openai import OpenAIEngine

from chatbot.engines.bedrock_engine import BedrockEngine
from server.engine import initialize_engine


class TestEngines:
    """Comprehensive engine testing - all functionality in one file."""

    SYSTEM_PROMPT = "You are a helpful assistant."
    TEST_PROMPT = "Hello, how are you? Please respond in one sentence."

    # Engine initialization tests
    def test_initialize_engine_bedrock(self):
        """Test BedrockEngine creation through initialize_engine."""
        with pytest.MonkeyPatch().context() as m:
            m.setenv("AWS_ACCESS_KEY_ID", "test-key")
            m.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
            m.setenv("AWS_REGION", "us-east-1")

            engine = initialize_engine("Bedrock", "meta.llama3-8b-instruct-v1:0")
            assert isinstance(engine, BedrockEngine)
            assert engine.model_id == "meta.llama3-8b-instruct-v1:0"
            assert engine.max_tokens == 1000
            assert engine.temperature == 0.7

    # Real API call tests (sample models only)
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_openai_real_api(self):
        """Test OpenAI engine with real API call."""
        openai_key = os.getenv("OPENAI_API_KEY")
        if not openai_key:
            pytest.skip("OPENAI_API_KEY not set")

        engine = OpenAIEngine(api_key=openai_key, model="gpt-4o-mini")
        kani = Kani(engine, system_prompt=self.SYSTEM_PROMPT)
        response = await kani.chat_round_str(self.TEST_PROMPT)

        assert response is not None
        assert len(response) > 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_anthropic_real_api(self):
        """Test Anthropic engine with real API call."""
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not anthropic_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        engine = AnthropicEngine(
            api_key=anthropic_key, model="claude-sonnet-4-20250514"
        )
        kani = Kani(engine, system_prompt=self.SYSTEM_PROMPT)
        response = await kani.chat_round_str(self.TEST_PROMPT)

        assert response is not None
        assert len(response) > 0

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_bedrock_real_api(self):
        """Test Bedrock engine with real API call."""
        aws_key = os.getenv("AWS_ACCESS_KEY_ID")
        aws_secret = os.getenv("AWS_SECRET_ACCESS_KEY")
        if not aws_key or not aws_secret:
            pytest.skip("AWS credentials not set")

        engine = BedrockEngine(
            model_id="meta.llama3-8b-instruct-v1:0",
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
        kani = Kani(engine, system_prompt=self.SYSTEM_PROMPT)
        response = await kani.chat_round_str(self.TEST_PROMPT)

        assert response is not None
        assert len(response) > 0

    def _has_credentials(self, provider):
        """Check if credentials are available for the provider"""
        if provider == "OpenAI":
            return bool(os.getenv("OPENAI_API_KEY"))
        elif provider == "Anthropic":
            return bool(os.getenv("ANTHROPIC_API_KEY"))
        elif provider == "Bedrock":
            return bool(
                os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY")
            )
        return False

    # Engine agnosticism test
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_engine_agnosticism(self):
        """Test that all engines work identically through initialization."""
        # Test one model from each provider
        test_cases = [
            ("OpenAI", "gpt-4o-mini"),
            ("Anthropic", "claude-sonnet-4-20250514"),
            ("Bedrock", "meta.llama3-8b-instruct-v1:0"),
        ]

        for provider, model_id in test_cases:
            # Check credentials first to avoid try-except in loop
            if not self._has_credentials(provider):
                pytest.skip(f"Credentials not available for {provider}")

            engine = initialize_engine(provider, model_id)
            kani = Kani(engine, system_prompt=self.SYSTEM_PROMPT)
            response = await kani.chat_round_str(self.TEST_PROMPT)
            assert response is not None
            assert len(response) > 0
