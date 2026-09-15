"""
Tests for survey context linking in InitializeConversationAPIView
(POST /api/initialize_conversation/)

At init we look up the participant's SurveyResponse by
(survey_id, participant_id) and snapshot a COPY of the answers onto
Conversation.survey_context. The copy is the point: it records what this
conversation actually saw, so a study remains reproducible even if the survey
tool later overwrites the source row.

Coverage:
  - Answers are snapshotted onto the conversation when a matching row exists
  - No matching row leaves survey_context null (missing data appends nothing)
  - The snapshot does not move when the source SurveyResponse changes
  - Lookup is scoped to both survey_id and participant_id
"""

import json
import uuid

import pytest
from django.test import Client

from chatbot.models import Conversation, SurveyResponse

URL = "/api/initialize_conversation/"

ANSWERS = [
    {"question": "How stressed have you felt this week?", "answer": "Very stressed"},
    {"question": "What's been on your mind?", "answer": "Mostly work deadlines."},
]


@pytest.fixture
def client():
    return Client()


def post_init(client, bot_name, **extra):
    payload = {
        "bot_name": bot_name,
        "conversation_id": f"conv_{uuid.uuid4().hex[:8]}",
        "participant_id": "R_xyz789",
        "survey_id": "SV_abc123",
        **extra,
    }
    response = client.post(
        URL, data=json.dumps(payload), content_type="application/json"
    )
    return response, payload["conversation_id"]


@pytest.fixture
def survey_row(db):
    return SurveyResponse.objects.create(
        survey_id="SV_abc123",
        participant_id="R_xyz789",
        answers=ANSWERS,
    )


# ── Snapshotting ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_answers_are_snapshotted_onto_the_conversation(client, make_bot, survey_row):
    bot = make_bot()
    response, conversation_id = post_init(client, bot.name)

    assert response.status_code == 200
    assert (
        Conversation.objects.get(conversation_id=conversation_id).survey_context
        == ANSWERS
    )


@pytest.mark.django_db
def test_no_survey_row_leaves_context_null(client, make_bot):
    """
    Missing survey data is not an error — the participant simply gets no
    context appended. A null here is also how analysis spots a participant
    whose survey tool never delivered their answers.
    """
    bot = make_bot()
    response, conversation_id = post_init(client, bot.name)

    assert response.status_code == 200
    assert (
        Conversation.objects.get(conversation_id=conversation_id).survey_context is None
    )


@pytest.mark.django_db
def test_snapshot_is_frozen_against_later_changes(client, make_bot, survey_row):
    """The whole reason we copy instead of following a foreign key."""
    _, conversation_id = post_init(client, bot_name=make_bot().name)

    survey_row.answers = [{"question": "Rewritten", "answer": "Later"}]
    survey_row.save()

    assert (
        Conversation.objects.get(conversation_id=conversation_id).survey_context
        == ANSWERS
    )


# ── Lookup scoping ────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_other_participants_answers_are_not_used(client, make_bot, survey_row):
    bot = make_bot()
    _, conversation_id = post_init(client, bot.name, participant_id="R_someone_else")

    assert (
        Conversation.objects.get(conversation_id=conversation_id).survey_context is None
    )


@pytest.mark.django_db
def test_other_surveys_answers_are_not_used(client, make_bot, survey_row):
    bot = make_bot()
    _, conversation_id = post_init(client, bot.name, survey_id="SV_different")

    assert (
        Conversation.objects.get(conversation_id=conversation_id).survey_context is None
    )


# ── End to end across both endpoints ──────────────────────────────────────────


@pytest.mark.django_db
def test_answers_posted_to_the_endpoint_reach_the_conversation(
    client, make_bot, settings
):
    """
    The full path a study actually takes: the survey tool POSTs answers, then
    the participant opens the chatbot. Guards the seam between the two
    endpoints — the header name, and the identifiers each side keys on.
    """
    settings.SURVEY_INGEST_TOKEN = "test-survey-token"

    ingest = client.post(
        "/api/survey_response/",
        data=json.dumps(
            {
                "survey_id": "SV_abc123",
                "participant_id": "R_xyz789",
                "answers": ANSWERS,
            }
        ),
        content_type="application/json",
        HTTP_X_SURVEY_TOKEN="test-survey-token",
    )
    assert ingest.status_code == 200

    response, conversation_id = post_init(client, make_bot().name)

    assert response.status_code == 200
    assert (
        Conversation.objects.get(conversation_id=conversation_id).survey_context
        == ANSWERS
    )
