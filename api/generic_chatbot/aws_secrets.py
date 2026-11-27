"""Secrets from AWS Secrets Manager, read once per process at start.

A stack built by infra/terraform passes only the names of its secrets, so the
values never sit in Terraform state or the Elastic Beanstalk configuration:

    APP_SECRET_ID       <prefix>-<env>/app, a JSON object with SECRET_KEY and,
                        when used, OPENAI_API_KEY, ANTHROPIC_API_KEY and
                        SURVEY_INGEST_TOKEN (put there by a person; see
                        docs/deployment/technical-reference.rst, "Secrets")
    DATABASE_SECRET_ID  the secret RDS keeps the master user in
                        (manage_master_user_password): {"username", "password"}

A named secret is required: a missing secret or value, or no permission to
read it, stops the process at start. Without the names (local development,
CI, and the hand-built live environment until the cutover) the same values
come from environment variables. Errors name secrets and keys, never values.
"""

import json
import os
from functools import cache

import boto3

APP_KEYS = {"SECRET_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SURVEY_INGEST_TOKEN"}


@cache
def read(secret_id):
    client = boto3.client(
        "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
    )
    text = client.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        # from None: a JSON error can carry the text it failed on.
        raise TypeError(f"secret {secret_id} is not a JSON object") from None
    return value


def load_into_environ():
    """Put the stack's secrets into os.environ, where settings.py and the code
    that reads the environment directly (the LLM engines) find them."""
    # ponytail: os.environ, not settings, so those direct readers need no change.
    app_id = os.getenv("APP_SECRET_ID")
    if app_id:
        values = read(app_id)
        unknown = sorted(set(values) - APP_KEYS)
        if unknown:
            raise RuntimeError(
                f"secret {app_id} has keys the application does not read: {unknown}"
            )
        os.environ.update(values)
    db_id = os.getenv("DATABASE_SECRET_ID")
    if db_id:
        db = read(db_id)
        os.environ.update(
            DATABASE_USER=db["username"], DATABASE_PASSWORD=db["password"]
        )
