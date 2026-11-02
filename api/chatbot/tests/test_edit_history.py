"""
Tests for edit history on configuration models (django-simple-history)

With named accounts, "who changed this bot's prompt mid-study?" has to have an
answer. Researcher-edited configuration keeps a field-level history; anything
participants write does not, because a history table per Utterance would
double the largest tables in the database.

Coverage:
  - An admin edit to a Bot records the editing user and the changed field
  - A participant chat round creates no history rows anywhere
  - Participant-written models carry no history at all
  - A SurveyIngestToken's secret is never copied into its history
"""

import json
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict
from django.test import AsyncClient, Client

from chatbot.models import (
    Avatar,
    Bot,
    Conversation,
    Keystroke,
    ModerationSettings,
    Persona,
    SurveyIngestToken,
    SurveyResponse,
    Utterance,
)
from chatbot.services.moderation import ModerationResult

TRACKED = (Bot, Persona, ModerationSettings, SurveyIngestToken)


def history_rows():
    return sum(model.history.count() for model in TRACKED)


@pytest.fixture
def staff_client(db):
    user = get_user_model().objects.create_superuser("researcher", "r@x.org", "pw")
    client = Client()
    client.force_login(user)
    return client, user


@pytest.mark.django_db
def test_admin_bot_edit_records_user_and_changed_field(staff_client, make_bot):
    client, user = staff_client
    bot = make_bot()
    data = {
        k: ("" if v is None else v) for k, v in model_to_dict(bot).items() if k != "id"
    }
    data["personas"] = []
    data["prompt"] = "A revised prompt."

    r = client.post(f"/api/admin/chatbot/bot/{bot.pk}/change/", data)

    assert r.status_code == 302, r.content
    latest = bot.history.latest()
    assert latest.history_user == user
    # Not an exact match: the form also saves blank nullable text fields as ""
    # where the fixture left None, which history rightly reports as a change.
    changed = latest.diff_against(latest.prev_record).changed_fields
    assert "prompt" in changed
    assert latest.prompt == "A revised prompt."


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_participant_chat_creates_no_history(mock_llm, make_conversation):
    conv = await sync_to_async(make_conversation)()
    before = await sync_to_async(history_rows)()

    with patch(
        "chatbot.services.runchat.moderate_message", return_value=ModerationResult()
    ):
        r = await AsyncClient().post(
            "/api/chatbot/",
            data=json.dumps(
                {
                    "bot_name": conv.bot_name,
                    "conversation_id": conv.conversation_id,
                    "participant_id": conv.participant_id,
                    "message": "Hello",
                }
            ),
            content_type="application/json",
        )

    assert r.status_code == 200, r.content
    assert await sync_to_async(history_rows)() == before


@pytest.mark.parametrize(
    "model", [Utterance, Conversation, Keystroke, SurveyResponse, Avatar]
)
def test_participant_written_models_have_no_history(model):
    assert not hasattr(model, "history")


@pytest.mark.django_db
def test_survey_token_secret_not_copied_into_history():
    issued = SurveyIngestToken.objects.create(note="Stress study, wave 1")
    issued.is_active = False
    issued.save()

    assert issued.history.count() == 2
    latest = issued.history.latest()
    assert latest.note == "Stress study, wave 1"
    assert not hasattr(latest, "token")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path",
    [
        "bot/",
        "conversation/",
        "utterance/",
        "moderationsettings/",  # custom changelist_view auto-creates the row
        "surveyingesttoken/",
    ],
)
def test_admin_pages_still_render_with_history_admin(staff_client, path):
    """ExportMixin and the custom changelist_view sit on top of SimpleHistoryAdmin."""
    client, _ = staff_client
    assert client.get(f"/api/admin/chatbot/{path}").status_code == 200


@pytest.mark.django_db
def test_history_page_renders(staff_client, make_bot):
    client, _ = staff_client
    bot = make_bot()
    r = client.get(f"/api/admin/chatbot/bot/{bot.pk}/history/")
    assert r.status_code == 200
