"""
Production runs on the Elastic Beanstalk instance role: Terraform sets no
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (env_parity.tftest.hcl). Every boto3
client must then come from the default credential chain, never from explicit
keys. Explicit keys stay an optional local-dev path.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings

from server.engine import initialize_engine, initialize_engine_from_model

KEYS = ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
BEDROCK_CLIENT = "chatbot.engines.bedrock_engine.boto3.client"


@pytest.fixture
def no_static_keys(monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def dev_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "dev-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dev-secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _no_credentials(factory):
    assert factory.called
    for call in factory.call_args_list:
        assert not set(KEYS) & set(call.kwargs), call


BEDROCK_MODEL = SimpleNamespace(
    provider=SimpleNamespace(name="Bedrock"), model_id="us.anthropic.claude-haiku-5-5"
)


@pytest.mark.parametrize(
    "build",
    [
        lambda: initialize_engine("Bedrock", "us.anthropic.claude-haiku-5-5"),
        lambda: initialize_engine_from_model(BEDROCK_MODEL),
    ],
)
def test_bedrock_engine_uses_default_chain_without_keys(no_static_keys, build):
    with patch(BEDROCK_CLIENT) as factory:
        build()
    _no_credentials(factory)
    assert factory.call_args.kwargs["region_name"] == "us-east-1"


@pytest.mark.parametrize(
    "build",
    [
        lambda: initialize_engine("Bedrock", "us.anthropic.claude-haiku-5-5"),
        lambda: initialize_engine_from_model(BEDROCK_MODEL),
    ],
)
def test_bedrock_engine_passes_dev_keys_when_set(dev_keys, build):
    with patch(BEDROCK_CLIENT) as factory:
        build()
    assert factory.call_args.kwargs["aws_access_key_id"] == "dev-key"
    assert factory.call_args.kwargs["aws_secret_access_key"] == "dev-secret"


# SESBackend and validate_bedrock_models: test_email_backend.py and
# test_model_protection.py already assert their exact boto3.client calls.


@pytest.mark.django_db
@override_settings(BACKEND_ENVIRONMENT="production")
def test_upload_presign_uses_default_chain_in_production(no_static_keys):
    from chatbot.services.upload import get_presigned_url

    request = RequestFactory().get(
        "/", {"filename": "a.png", "content_type": "image/png"}
    )
    with (
        patch("chatbot.services.upload.require_perm", return_value=None),
        patch("chatbot.services.upload.boto3.client") as factory,
    ):
        get_presigned_url(request)
    _no_credentials(factory)


def test_s3_helper_uses_default_chain_in_production(no_static_keys, monkeypatch):
    monkeypatch.setenv("BACKEND_ENVIRONMENT", "production")
    with patch("boto3.client") as factory:
        from chatbot.services import s3_helper

        importlib.reload(s3_helper)
    _no_credentials(factory)
    importlib.reload(s3_helper)  # back to a real client for other tests
