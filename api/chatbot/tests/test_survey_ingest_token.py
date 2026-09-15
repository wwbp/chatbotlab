"""
Tests for admin-managed survey ingest tokens (chatbot.models.SurveyIngestToken)

Researchers need to issue a token per study without an AWS console or a
redeploy, so tokens live in the database and are created from the admin panel.
The SURVEY_INGEST_TOKEN environment variable keeps working alongside them, for
deployments that prefer to manage it as infrastructure.

Coverage:
  - A token is generated automatically, and is long and unique
  - An active database token authenticates the ingest endpoint
  - Deactivating a token revokes it without deleting the record
  - The environment variable still works when no database token matches
  - Fail closed: no environment variable and no active token rejects everything
"""

import json

import pytest
from django.test import Client

from chatbot.models import SurveyIngestToken, SurveyResponse

URL = "/api/survey_response/"

PAYLOAD = {
    "survey_id": "SV_abc123",
    "participant_id": "R_xyz789",
    "answers": [{"question": "Feeling?", "answer": "Fine"}],
}


@pytest.fixture
def client():
    return Client()


@pytest.fixture(autouse=True)
def _no_env_token(settings):
    """Default to no environment token, so these tests exercise the DB path."""
    settings.SURVEY_INGEST_TOKEN = ""


def post(client, token):
    return client.post(
        URL,
        data=json.dumps(PAYLOAD),
        content_type="application/json",
        HTTP_X_SURVEY_TOKEN=token,
    )


# ── Token generation ──────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_token_is_generated_automatically():
    """The researcher supplies a note; the secret itself is never hand-typed."""
    issued = SurveyIngestToken.objects.create(note="Stress study, wave 1")

    assert issued.token
    assert len(issued.token) >= 32
    assert issued.note == "Stress study, wave 1"
    assert issued.is_active is True


@pytest.mark.django_db
def test_generated_tokens_are_unique():
    tokens = {SurveyIngestToken.objects.create(note=f"s{i}").token for i in range(10)}
    assert len(tokens) == 10


# ── Authentication via the database ───────────────────────────────────────────


@pytest.mark.django_db
def test_active_token_authenticates(client):
    issued = SurveyIngestToken.objects.create(note="Stress study")

    assert post(client, issued.token).status_code == 200
    assert SurveyResponse.objects.count() == 1


@pytest.mark.django_db
def test_inactive_token_is_rejected(client):
    """Revoking a study's token must not require deleting the audit record."""
    issued = SurveyIngestToken.objects.create(note="Finished study", is_active=False)

    assert post(client, issued.token).status_code == 403
    assert not SurveyResponse.objects.exists()


@pytest.mark.django_db
def test_unknown_token_is_rejected(client):
    SurveyIngestToken.objects.create(note="Some study")

    assert post(client, "not-a-real-token").status_code == 403
    assert not SurveyResponse.objects.exists()


@pytest.mark.django_db
def test_one_of_several_active_tokens_authenticates(client):
    """Several studies can run at once, each with its own token."""
    SurveyIngestToken.objects.create(note="Study A")
    study_b = SurveyIngestToken.objects.create(note="Study B")

    assert post(client, study_b.token).status_code == 200


# ── Interaction with the environment variable ─────────────────────────────────


@pytest.mark.django_db
def test_environment_token_still_works(client, settings):
    settings.SURVEY_INGEST_TOKEN = "env-configured-token"

    assert post(client, "env-configured-token").status_code == 200


@pytest.mark.django_db
def test_database_token_works_alongside_an_environment_token(client, settings):
    settings.SURVEY_INGEST_TOKEN = "env-configured-token"
    issued = SurveyIngestToken.objects.create(note="Study A")

    assert post(client, issued.token).status_code == 200


@pytest.mark.django_db
def test_fails_closed_with_no_env_var_and_no_tokens(client):
    """An unconfigured deployment refuses all traffic rather than accepting it."""
    assert post(client, "anything").status_code == 403
    assert post(client, "").status_code == 403
    assert not SurveyResponse.objects.exists()
