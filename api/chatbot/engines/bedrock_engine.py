"""
Amazon Bedrock engine for Kani framework.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
from botocore.exceptions import ClientError
from kani.engines.base import BaseCompletion, BaseEngine
from kani.models import ChatMessage, ChatRole
from kani.prompts.pipeline import PromptPipeline


class BedrockCompletion(BaseCompletion):
    """Completion wrapper for Bedrock responses."""

    def __init__(
        self,
        message: ChatMessage,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ):
        self._message = message
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens

    @property
    def message(self) -> ChatMessage:
        return self._message

    @property
    def prompt_tokens(self) -> int | None:
        return self._prompt_tokens

    @property
    def completion_tokens(self) -> int | None:
        return self._completion_tokens


class BedrockEngine(BaseEngine):
    """Amazon Bedrock engine for Kani framework."""

    def __init__(
        self,
        model_id: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        region_name: str = "us-east-1",
        max_tokens: int = 1000,
        temperature: float = 0.7,
        top_p: float = 0.9,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p

        # Initialize Bedrock client
        if aws_access_key_id and aws_secret_access_key:
            # Use explicit credentials (local dev or explicit service account)
            self.client = boto3.client(
                "bedrock-runtime",
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                region_name=region_name,
            )
        else:
            # Use credential chain (AWS - IAM instance profile)
            self.client = boto3.client("bedrock-runtime", region_name=region_name)

        # Set context size based on model
        self.max_context_size = self._get_model_context_size(model_id)

        # Thread pool for running sync Bedrock calls
        self._executor = ThreadPoolExecutor(max_workers=1)

        # Create prompt pipeline
        self.pipeline = self._create_pipeline()

    def _get_model_context_size(self, model_id: str) -> int:
        """Get context size for the model."""
        context_sizes = {
            "meta.llama3-8b-instruct-v1:0": 8192,
            "meta.llama3-70b-instruct-v1:0": 8192,
            "anthropic.claude-3-sonnet-20240229-v1:0": 200000,
            "anthropic.claude-3-haiku-20240307-v1:0": 200000,
            "anthropic.claude-3-5-sonnet-20240620-v1:0": 200000,
            "us.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
            "us.anthropic.claude-opus-4-1-20250805-v1:0": 200000,
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0": 200000,
            "us.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
            "us.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
            "us.anthropic.claude-opus-4-6-v1": 200000,
            "us.anthropic.claude-sonnet-4-6": 200000,
            "us.anthropic.claude-opus-4-7": 200000,
            "us.anthropic.claude-opus-4-8": 200000,
            "us.anthropic.claude-haiku-5-5": 200000,
            "us.anthropic.claude-sonnet-5-5": 200000,
            "us.anthropic.claude-opus-5-5": 200000,
        }
        return context_sizes.get(model_id, 8192)

    def _create_pipeline(self) -> PromptPipeline:
        """Create the prompt pipeline for Bedrock message conversion."""
        return (
            PromptPipeline()
            # Convert to Bedrock format: list of dicts with role/content
            .conversation_dict(
                system_role="user",  # Map system to user for Bedrock
                user_role="user",
                assistant_role="assistant",
                function_role="user",  # Map function to user for Bedrock
                content_transform=self._transform_content,
            )
            # Ensure conversation starts with user message (after role mapping)
            .macro_apply(self._ensure_starts_with_user)
            # Ensure conversation ends with user message (Bedrock requirement)
            .macro_apply(self._ensure_ends_with_user)
        )

    def _transform_content(self, message: ChatMessage) -> list[dict[str, str]]:
        """Transform message content to Bedrock format."""
        if isinstance(message.content, str):
            return [{"text": message.content}]
        elif isinstance(message.content, list):
            return message.content
        else:
            return [{"text": str(message.content)}]

    def _ensure_starts_with_user(
        self, messages: list[dict], functions: list
    ) -> list[dict]:
        """Ensure the conversation starts with a user message (system prompt)."""
        if not messages or messages[0]["role"] != "user":
            # If no system prompt at start, add a default one
            messages.insert(
                0,
                {
                    "role": "user",
                    "content": [{"text": "You are a helpful assistant."}],
                },
            )
        return messages

    def _ensure_ends_with_user(
        self, messages: list[dict], functions: list
    ) -> list[dict]:
        """Ensure the conversation ends with a user message (Bedrock requirement)."""
        if not messages or messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": [{"text": "Continue"}]})
        return messages

    def message_len(self, message: ChatMessage) -> int:
        """Estimate message length in tokens."""
        if isinstance(message.content, str):
            # Rough estimation: 1 token ≈ 4 chars
            return len(message.content) // 4
        return 50  # Default for complex content

    async def prompt_len(
        self, messages: list[ChatMessage], functions: list | None = None, **kwargs
    ) -> int:
        """Approximate prompt length for Bedrock models."""
        total = sum(self.message_len(message) for message in messages)
        total += self.function_token_reserve(functions)
        return total

    async def predict(
        self,
        messages: list[ChatMessage],
        functions: list | None = None,
        **kwargs,
    ) -> BedrockCompletion:
        """Generate a response from Bedrock."""
        try:
            # Convert messages using pipeline
            conversation = self.pipeline(messages, functions or [])

            # Call Bedrock in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self._executor,
                self._call_bedrock,
                conversation,
            )

            # Join the text blocks: newer Claude models can put a reasoning
            # block (no "text" key) before the reply.
            response_text = "".join(
                block.get("text", "")
                for block in response["output"]["message"]["content"]
            ).strip()

            # Create response message
            response_message = ChatMessage(
                role=ChatRole.ASSISTANT, content=response_text
            )

            return BedrockCompletion(response_message)

        except Exception as e:
            raise RuntimeError(f"Bedrock API call failed: {e}")

    async def stream(
        self,
        messages: list[ChatMessage],
        functions: list | None = None,
        **kwargs,
    ):
        """Stream a response from Bedrock."""
        try:
            # Convert messages using pipeline
            conversation = self.pipeline(messages, functions or [])

            # Call Bedrock in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self._executor,
                self._call_bedrock_stream,
                conversation,
            )

            # Process the streaming response
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    # Extract text chunk from the delta
                    text_chunk = event["contentBlockDelta"]["delta"]["text"]
                    yield text_chunk

        except Exception as e:
            raise RuntimeError(f"Bedrock streaming API call failed: {e}")

    # Models that reject temperature/top_p (the 5.5 models reject any
    # non-default value, so they are sent none)
    _NO_SAMPLING_PARAMS_MODELS = {
        "us.anthropic.claude-opus-4-7",
        "anthropic.claude-opus-4-7",
        "us.anthropic.claude-opus-4-8",
        "anthropic.claude-opus-4-8",
        "us.anthropic.claude-haiku-5-5",
        "anthropic.claude-haiku-5-5",
        "us.anthropic.claude-sonnet-5-5",
        "anthropic.claude-sonnet-5-5",
        "us.anthropic.claude-opus-5-5",
        "anthropic.claude-opus-5-5",
    }

    def _build_inference_config(self) -> dict[str, Any]:
        """Build inferenceConfig, omitting sampling params for models that don't support them."""
        config: dict[str, Any] = {"maxTokens": self.max_tokens}
        if self.model_id not in self._NO_SAMPLING_PARAMS_MODELS:
            config["temperature"] = self.temperature
        return config

    def _call_bedrock(self, conversation: list[dict]) -> dict[str, Any]:
        """Make the actual Bedrock API call."""
        try:
            response = self.client.converse(
                modelId=self.model_id,
                messages=conversation,
                inferenceConfig=self._build_inference_config(),
            )
            return response
        except ClientError as e:
            raise RuntimeError(f"Bedrock API call failed: {e}")

    def _call_bedrock_stream(self, conversation: list[dict]) -> dict[str, Any]:
        """Make the actual Bedrock streaming API call."""
        try:
            response = self.client.converse_stream(
                modelId=self.model_id,
                messages=conversation,
                inferenceConfig=self._build_inference_config(),
            )
            return response
        except ClientError as e:
            raise RuntimeError(f"Bedrock streaming API call failed: {e}")

    async def close(self):
        """Close the engine and cleanup resources."""
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=True)

    def explain_pipeline(self):
        """Print an explanation of the configured prompt pipeline."""
        # This method is for debugging purposes
        # In production, consider using logging instead of print
