"""
A stack built by infra/terraform keeps its secrets in AWS Secrets Manager and
passes only their names: APP_SECRET_ID (Django's SECRET_KEY and the API keys,
one JSON object) and DATABASE_SECRET_ID (the RDS-managed master credentials).
settings.py reads them once per process at start. Without those names (local
development, CI) the same values come from environment variables.

Secrets Manager is faked with botocore's Stubber, which checks each call
against the real API.
"""

import json
import logging
import runpy
from pathlib import Path

import boto3
import pytest
import requests
from botocore.stub import Stubber

from generic_chatbot import aws_secrets

SETTINGS = Path(__file__).resolve().parents[2] / "generic_chatbot" / "settings.py"
APP_ID = "chatbotlab-production/app"
DB_ID = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-1234-AbCdEf"
APP_VALUES = {
    "SECRET_KEY": "sk-django-from-secrets-manager-" + "x" * 30,
    "OPENAI_API_KEY": "sk-openai-from-secrets-manager",
    "SURVEY_INGEST_TOKEN": "survey-token-from-secrets-manager",
}
DB_VALUES = {"username": "chatbot_user", "password": "rds-rotated-password"}
SECRET_NAMES = ("SECRET_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SURVEY_INGEST_TOKEN",
                "DATABASE_USER", "DATABASE_PASSWORD")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    # delenv records the current value, so whatever the loader writes into
    # os.environ is undone after the test.
    for name in (*SECRET_NAMES, "APP_SECRET_ID", "DATABASE_SECRET_ID"):
        monkeypatch.delenv(name, raising=False)
    aws_secrets.read.cache_clear()
    yield
    aws_secrets.read.cache_clear()


def fake_secrets_manager(monkeypatch, secrets):
    """boto3.client("secretsmanager") answers from `secrets`, in call order:
    a str is the SecretString, None means the secret has no value."""
    client = boto3.client("secretsmanager", region_name="eu-west-1",
                          aws_access_key_id="x", aws_secret_access_key="x")
    stubber = Stubber(client)
    for secret_id, value in secrets:
        if value is None:
            stubber.add_client_error("get_secret_value", "ResourceNotFoundException",
                                     expected_params={"SecretId": secret_id})
        else:
            stubber.add_response("get_secret_value", {"SecretString": value},
                                 {"SecretId": secret_id})
    stubber.activate()
    calls = []

    def make_client(service, **kwargs):
        calls.append((service, kwargs.get("region_name")))
        return client

    monkeypatch.setattr(boto3, "client", make_client)
    return calls


def production(monkeypatch):
    """What the Terraform stack's Elastic Beanstalk environment sets."""
    monkeypatch.setenv("DEBUG", "False")
    monkeypatch.setenv("BACKEND_ENVIRONMENT", "production")
    monkeypatch.setenv("REDIS_URL", "rediss://cache.example:6379")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("APP_SECRET_ID", APP_ID)
    monkeypatch.setenv("DATABASE_SECRET_ID", DB_ID)

    def no_metadata_service(*args, **kwargs):
        raise requests.exceptions.ConnectionError

    monkeypatch.setattr(requests, "put", no_metadata_service)


def load_settings():
    return runpy.run_path(str(SETTINGS))


def test_production_reads_its_secrets_from_secrets_manager(monkeypatch):
    production(monkeypatch)
    calls = fake_secrets_manager(monkeypatch, [(APP_ID, json.dumps(APP_VALUES)),
                                               (DB_ID, json.dumps(DB_VALUES))])

    settings = load_settings()

    assert settings["SECRET_KEY"] == APP_VALUES["SECRET_KEY"]
    assert settings["OPENAI_API_KEY"] == APP_VALUES["OPENAI_API_KEY"]
    assert settings["SURVEY_INGEST_TOKEN"] == APP_VALUES["SURVEY_INGEST_TOKEN"]
    assert settings["DATABASES"]["default"]["USER"] == "chatbot_user"
    assert settings["DATABASES"]["default"]["PASSWORD"] == "rds-rotated-password"
    # The engines read the API keys with os.getenv().
    import os

    assert os.getenv("OPENAI_API_KEY") == APP_VALUES["OPENAI_API_KEY"]
    # With the instance role, in the stack's region.
    assert calls and all(c == ("secretsmanager", "eu-west-1") for c in calls)


def test_secrets_are_read_once_per_process(monkeypatch):
    production(monkeypatch)
    # Exactly one response per secret: a second call would find none.
    fake_secrets_manager(monkeypatch, [(APP_ID, json.dumps(APP_VALUES)),
                                       (DB_ID, json.dumps(DB_VALUES))])
    load_settings()
    load_settings()


def test_production_without_its_secret_fails_at_start(monkeypatch):
    production(monkeypatch)
    fake_secrets_manager(monkeypatch, [(APP_ID, None)])
    monkeypatch.setenv("SECRET_KEY", "an-env-value-must-not-be-used-instead")

    with pytest.raises(Exception, match="ResourceNotFoundException"):
        load_settings()


def test_production_secret_without_secret_key_fails_at_start(monkeypatch):
    production(monkeypatch)
    fake_secrets_manager(monkeypatch, [(APP_ID, json.dumps({"OPENAI_API_KEY": "sk-x"})),
                                       (DB_ID, json.dumps(DB_VALUES))])

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        load_settings()


def test_unknown_key_in_the_secret_fails_without_showing_values(monkeypatch):
    production(monkeypatch)
    fake_secrets_manager(monkeypatch, [(APP_ID, json.dumps({**APP_VALUES, "OPENAI_KEY": "sk-typo-value"}))])

    with pytest.raises(RuntimeError, match="OPENAI_KEY") as raised:
        load_settings()
    assert "sk-typo-value" not in str(raised.value)


def test_local_development_uses_environment_variables(monkeypatch):
    monkeypatch.setenv("DEBUG", "True")
    monkeypatch.setenv("SECRET_KEY", "local-dev-secret-key")
    monkeypatch.setenv("DATABASE_PASSWORD", "local-password")

    def no_aws(*args, **kwargs):
        raise AssertionError("local development must not call AWS")

    monkeypatch.setattr(boto3, "client", no_aws)

    settings = load_settings()

    assert settings["SECRET_KEY"] == "local-dev-secret-key"
    assert settings["DATABASES"]["default"]["PASSWORD"] == "local-password"


def test_no_secret_value_is_logged_or_printed(monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    production(monkeypatch)
    fake_secrets_manager(monkeypatch, [(APP_ID, json.dumps(APP_VALUES)),
                                       (DB_ID, json.dumps(DB_VALUES))])
    load_settings()
    # A malformed secret: the error must not quote it.
    aws_secrets.read.cache_clear()
    fake_secrets_manager(monkeypatch, [(APP_ID, '{"SECRET_KEY": "half-written-value')])
    with pytest.raises(RuntimeError) as raised:
        load_settings()

    out = capsys.readouterr()
    seen = caplog.text + out.out + out.err + str(raised.value) + repr(raised.value.__cause__)
    for value in (*APP_VALUES.values(), *DB_VALUES.values(), "half-written-value"):
        if value != "chatbot_user":
            assert value not in seen


# RDS rotates the master password (every 7 days by default) and the old one
# stops working at once; the process read it at start.
def wrapper_with(monkeypatch, outcomes):
    from django.db.backends.mysql import base as mysql

    from generic_chatbot.db import base

    attempts = []

    def connect(self, conn_params):
        attempts.append(dict(conn_params))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mysql.DatabaseWrapper, "get_new_connection", connect)
    wrapper = base.DatabaseWrapper({"USER": "chatbot_user", "PASSWORD": "old", "NAME": "x", "HOST": "h",
                                    "PORT": "", "OPTIONS": {}, "TIME_ZONE": None, "CONN_MAX_AGE": 0,
                                    "CONN_HEALTH_CHECKS": False, "AUTOCOMMIT": True, "ATOMIC_REQUESTS": False,
                                    "TEST": {}}, alias="rotation-test")
    return wrapper, attempts, mysql.Database.OperationalError


def test_a_rotated_database_password_is_read_again(monkeypatch):
    monkeypatch.setenv("DATABASE_SECRET_ID", DB_ID)
    fake_secrets_manager(monkeypatch, [(DB_ID, json.dumps(DB_VALUES))])
    from django.db.backends.mysql import base as mysql

    wrapper, attempts, error = wrapper_with(monkeypatch, [mysql.Database.OperationalError(1045, "Access denied"),
                                                          "connection"])

    assert wrapper.get_new_connection({"user": "chatbot_user", "password": "old"}) == "connection"
    assert [a["password"] for a in attempts] == ["old", "rds-rotated-password"]
    assert wrapper.settings_dict["PASSWORD"] == "rds-rotated-password"


@pytest.mark.parametrize("secret_id, code", [(DB_ID, 2003), (None, 1045)])
def test_other_connection_errors_are_not_retried(monkeypatch, secret_id, code):
    if secret_id:
        monkeypatch.setenv("DATABASE_SECRET_ID", secret_id)
    from django.db.backends.mysql import base as mysql

    wrapper, attempts, error = wrapper_with(monkeypatch, [mysql.Database.OperationalError(code, "no")])

    with pytest.raises(error):
        wrapper.get_new_connection({"user": "chatbot_user", "password": "old"})
    assert len(attempts) == 1
