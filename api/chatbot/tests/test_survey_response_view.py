"""
Tests for survey_response  (POST /api/survey_response/)

The endpoint the survey tool calls to hand us a participant's answers before
they reach the chatbot. Unlike the other endpoints in this project it takes
participant data from the open internet, so it is authenticated with a shared
secret and validates its payload strictly.

Coverage:
  - Authentication: missing, wrong, and correct token
  - Payload validation: bad JSON, missing fields, malformed answers
  - Storage: a row is created with answers and order intact
  - Idempotency: a repeat POST overwrites rather than duplicating
  - Method: anything other than POST is rejected
"""

import json

import pytest
from django.test import Client

from chatbot.models import SurveyResponse

URL = "/api/survey_response/"
TOKEN = "test-survey-token"

ANSWERS = [
    {"question": "How stressed have you felt this week?", "answer": "Very stressed"},
    {"question": "What's been on your mind?", "answer": "Mostly work deadlines."},
]


@pytest.fixture
def client():
    return Client()


@pytest.fixture(autouse=True)
def _token(settings):
    """Every test in this module runs with a known ingest token configured."""
    settings.SURVEY_INGEST_TOKEN = TOKEN


def post(client, payload, token=TOKEN, **kwargs):
    headers = {"HTTP_X_SURVEY_TOKEN": token} if token is not None else {}
    return client.post(
        URL,
        data=json.dumps(payload),
        content_type="application/json",
        **headers,
        **kwargs,
    )


def valid_payload(**overrides):
    return {
        "survey_id": "SV_abc123",
        "participant_id": "R_xyz789",
        "answers": ANSWERS,
        **overrides,
    }


# ── Authentication ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_missing_token_is_rejected(client):
    r = post(client, valid_payload(), token=None)
    assert r.status_code == 403
    assert not SurveyResponse.objects.exists()


@pytest.mark.django_db
def test_wrong_token_is_rejected(client):
    r = post(client, valid_payload(), token="not-the-token")
    assert r.status_code == 403
    assert not SurveyResponse.objects.exists()


@pytest.mark.django_db
def test_unconfigured_token_rejects_everything(client, settings):
    """
    Fail closed: if the deployment has no token set, the endpoint refuses all
    traffic rather than silently accepting anonymous participant data.
    """
    settings.SURVEY_INGEST_TOKEN = ""
    r = post(client, valid_payload(), token="")
    assert r.status_code == 403
    assert not SurveyResponse.objects.exists()


# ── Payload validation ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_bad_json_returns_400(client):
    r = client.post(
        URL,
        data="not valid json",
        content_type="application/json",
        HTTP_X_SURVEY_TOKEN=TOKEN,
    )
    assert r.status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["survey_id", "participant_id"])
def test_missing_required_field_returns_400(client, field):
    payload = valid_payload()
    del payload[field]
    r = post(client, payload)
    assert r.status_code == 400
    assert not SurveyResponse.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "answers",
    [
        {"question": "q", "answer": "a"},  # dict, not a list
        "q: a",  # string
        [{"question": "orphan question"}],  # entry missing 'answer'
        [{"answer": "orphan answer"}],  # entry missing 'question'
        ["not a dict"],
    ],
)
def test_malformed_answers_returns_400(client, answers):
    """
    Malformed data is rejected at the boundary, while the survey tool can still
    see the error — rather than being discovered mid-conversation.
    """
    r = post(client, valid_payload(answers=answers))
    assert r.status_code == 400
    assert not SurveyResponse.objects.exists()


@pytest.mark.django_db
def test_empty_answers_list_is_accepted(client):
    """A participant who skipped every question is valid, not an error."""
    r = post(client, valid_payload(answers=[]))
    assert r.status_code == 200
    assert SurveyResponse.objects.get().answers == []


# ── Storage ───────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_stores_answers_in_order(client):
    r = post(client, valid_payload())
    assert r.status_code == 200

    row = SurveyResponse.objects.get(survey_id="SV_abc123", participant_id="R_xyz789")
    assert row.answers == ANSWERS


@pytest.mark.django_db
def test_accepts_many_long_answers(client):
    """Question count and answer length are deliberately uncapped."""
    answers = [
        {"question": f"Question {i}", "answer": "word " * 2000} for i in range(40)
    ]
    r = post(client, valid_payload(answers=answers))
    assert r.status_code == 200
    assert len(SurveyResponse.objects.get().answers) == 40


# ── Idempotency ───────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_repeat_post_overwrites_rather_than_duplicating(client):
    """The survey tool may retry, and participants may restart a survey."""
    post(client, valid_payload())
    updated = [{"question": "Changed?", "answer": "Yes"}]
    r = post(client, valid_payload(answers=updated))

    assert r.status_code == 200
    assert SurveyResponse.objects.count() == 1
    assert SurveyResponse.objects.get().answers == updated


@pytest.mark.django_db
def test_different_participants_get_separate_rows(client):
    post(client, valid_payload(participant_id="R_one"))
    post(client, valid_payload(participant_id="R_two"))
    assert SurveyResponse.objects.count() == 2


# ── Method ────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_get_is_rejected(client):
    r = client.get(URL, HTTP_X_SURVEY_TOKEN=TOKEN)
    assert r.status_code == 405
