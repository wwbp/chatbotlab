"""
Tests for the analysis-ready admin exports (the Resource classes in chatbot.admin).

An export is what a researcher loads into R, pandas or Excel, so its default
columns are the ones an analysis joins on: the conversation_id the survey tool
knows (not the database pk), study and condition. Prompt and history fields are
several KB per row and are left out. Keystrokes and survey responses are
exportable too. Every export is permission-gated (Data viewer) and recorded in
the AccessLog.

Coverage:
  - Utterance CSV: exact header, conversation_id strings, no prompt columns,
    text with commas/newlines survives, UTF-8 BOM so Excel reads accents
  - Conversation CSV: exact header, message count
  - Keystroke and SurveyResponse export as a Data viewer, logged as "export"
  - a Config editor cannot export participant data
"""

import csv
import io
import json
import uuid

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client
from django.utils import timezone

from chatbot.admin import (
    ConversationResource,
    KeystrokeResource,
    SurveyResponseResource,
    UtteranceResource,
)
from chatbot.models import AccessLog, Keystroke, SurveyResponse, Utterance

EXPORT_URL = "/api/admin/chatbot/{}/export/"


def _client_for(group_name):
    user = User.objects.create_user(
        username=f"u_{uuid.uuid4().hex[:8]}", password="pass", is_staff=True
    )
    user.groups.add(Group.objects.get(name=group_name))
    client = Client()
    client.force_login(user)
    return client


def _export(client, model, resource):
    """POST the export form as the admin page would: CSV, every field ticked."""
    prefix = resource.__name__.lower()
    data = {"format": "0", "resource": "0"}
    data.update({f"{prefix}_{f}": "on" for f in resource().get_export_order()})
    return client.post(EXPORT_URL.format(model), data)


def _csv_rows(response):
    assert response.status_code == 200
    assert "attachment" in response["Content-Disposition"]
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.mark.django_db
def test_utterance_export_has_analysis_columns(make_conversation):
    conversation = make_conversation(
        conversation_id="R_abc123",
        study_name="stress",
        user_group="treatment",
        survey_id="SV_1",
    )
    Utterance.objects.create(
        conversation=conversation,
        speaker_id="user",
        bot_name=conversation.bot_name,
        participant_id="p001",
        text='Hé, "quoted", comma\nnewline',
        instruction_prompt="SECRET PROMPT",
        chat_history_used="[]",
        moderation_category="harassment",
    )

    response = _export(_client_for("Data viewer"), "utterance", UtteranceResource)
    header, row = _csv_rows(response)

    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM: Excel reads UTF-8
    assert header == [
        "conversation_id",
        "study_name",
        "condition",
        "survey_id",
        "participant_id",
        "bot_name",
        "speaker_id",
        "text",
        "created_time",
        "moderation_category",
    ]
    record = dict(zip(header, row, strict=True))
    assert record["conversation_id"] == "R_abc123"
    assert record["condition"] == "treatment"
    assert record["text"] == 'Hé, "quoted", comma\nnewline'
    assert "SECRET PROMPT" not in ",".join(row)


@pytest.mark.django_db
def test_utterance_export_is_logged(make_conversation):
    Utterance.objects.create(conversation=make_conversation(), text="hi")

    _export(_client_for("Data viewer"), "utterance", UtteranceResource)

    assert AccessLog.objects.filter(action=AccessLog.Action.EXPORT).count() == 1


@pytest.mark.django_db
def test_conversation_export_has_analysis_columns(make_conversation):
    conversation = make_conversation(
        conversation_id="R_abc123",
        study_name="stress",
        user_group="control",
        bot_config="{" + '"x": 1, ' * 500 + "}",
    )
    for _ in range(3):
        Utterance.objects.create(conversation=conversation, text="hi")

    header, row = _csv_rows(
        _export(_client_for("Data viewer"), "conversation", ConversationResource)
    )

    assert header == [
        "conversation_id",
        "participant_id",
        "study_name",
        "condition",
        "survey_id",
        "bot_name",
        "started_time",
        "message_count",
    ]
    record = dict(zip(header, row, strict=True))
    assert record["conversation_id"] == "R_abc123"
    assert record["message_count"] == "3"


@pytest.mark.django_db
def test_keystroke_export(make_conversation):
    Keystroke.objects.create(
        conversation_id="R_abc123",
        total_time_on_page=12.5,
        total_time_away_from_page=1.0,
        keystroke_count=40,
        timestamp=timezone.now(),
    )

    header, row = _csv_rows(
        _export(_client_for("Data viewer"), "keystroke", KeystrokeResource)
    )

    assert header == [
        "conversation_id",
        "keystroke_count",
        "total_time_on_page",
        "total_time_away_from_page",
        "timestamp",
    ]
    assert row[:2] == ["R_abc123", "40"]
    assert AccessLog.objects.filter(action=AccessLog.Action.EXPORT).count() == 1


@pytest.mark.django_db
def test_survey_response_export():
    answers = [{"question": "How stressed?", "answer": "Very"}]
    SurveyResponse.objects.create(
        survey_id="SV_1", participant_id="p001", answers=answers
    )

    header, row = _csv_rows(
        _export(_client_for("Data viewer"), "surveyresponse", SurveyResponseResource)
    )

    assert header == ["survey_id", "participant_id", "received_at", "answers"]
    record = dict(zip(header, row, strict=True))
    assert record["participant_id"] == "p001"
    assert json.loads(record["answers"]) == answers
    assert AccessLog.objects.filter(action=AccessLog.Action.EXPORT).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("model", ["utterance", "keystroke", "surveyresponse"])
def test_config_editor_cannot_export_participant_data(model):
    client = _client_for("Config editor")

    response = client.post(EXPORT_URL.format(model), {"format": "0", "resource": "0"})

    assert response.status_code == 403
