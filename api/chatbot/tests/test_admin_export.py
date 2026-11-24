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
  - formula escaping: in CSV and TSV a text cell that a spreadsheet would run
    as a formula gets an apostrophe in front; numbers, other text and the
    other formats are untouched; the apostrophe can be removed exactly
"""

import csv
import io
import json
import re
import uuid

import pytest
from django.apps import apps
from django.contrib import admin
from django.contrib.auth.models import Group, User
from django.test import Client
from django.utils import timezone
from import_export.admin import ExportMixin

from chatbot.admin import (
    BotResource,
    ConversationResource,
    FormulaSafeExportMixin,
    FormulaSafeResource,
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


CSV, TSV, JSON = "0", "1", "2"  # positions in the export form's format list


def _export(client, model, resource, fmt=CSV):
    """POST the export form as the admin page would: CSV, every field ticked."""
    prefix = resource.__name__.lower()
    data = {"format": fmt, "resource": "0"}
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

    response = _export(_client_for("Data viewer"), "conversation", ConversationResource)
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM, as for utterances
    header, row = _csv_rows(response)

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


# ── Formula escaping ─────────────────────────────────────────────────────────
#
# A spreadsheet runs a cell starting with one of these as a formula, and
# participants type the cells. The export puts an apostrophe in front, which
# spreadsheets read as "this is text". Nothing is removed from the cell.

FORMULA_STARTS = ["=1+1", "+1+1", "-1+1", "@SUM(A1)", "\t=1+1", "\r=1+1"]


def _unescape(cell):
    """What the docs tell an analyst to do to get the typed text back."""
    return re.sub(r"^'(?='*[=+\-@\t\r])", "", cell)


def _utterance_records(make_conversation, texts, fmt=CSV, delimiter=","):
    conversation = make_conversation()
    for text in texts:
        Utterance.objects.create(
            conversation=conversation, speaker_id="user", text=text
        )
    response = _export(_client_for("Data viewer"), "utterance", UtteranceResource, fmt)
    assert response.status_code == 200
    body = response.content.decode("utf-8-sig")
    return response, list(
        csv.DictReader(io.StringIO(body, newline=""), delimiter=delimiter)
    )


@pytest.mark.django_db
@pytest.mark.parametrize("text", FORMULA_STARTS)
def test_csv_export_puts_an_apostrophe_before_a_formula_cell(make_conversation, text):
    response, (record,) = _utterance_records(make_conversation, [text])

    assert record["text"] == "'" + text
    assert response.content.startswith(b"\xef\xbb\xbf")  # Excel BOM is kept


@pytest.mark.django_db
def test_tsv_export_is_escaped_too(make_conversation):
    _, (record,) = _utterance_records(make_conversation, ["=1+1"], TSV, "\t")

    assert record["text"] == "'=1+1"


@pytest.mark.django_db
def test_json_export_is_not_escaped(make_conversation):
    """JSON is read by programs, not spreadsheets: it gets the text as typed."""
    conversation = make_conversation()
    Utterance.objects.create(conversation=conversation, speaker_id="user", text="=1+1")

    response = _export(_client_for("Data viewer"), "utterance", UtteranceResource, JSON)

    assert [row["text"] for row in json.loads(response.content)] == ["=1+1"]


@pytest.mark.django_db
def test_other_cells_are_exported_unchanged(make_conversation):
    texts = [
        "plain text",
        "a = b + c - d @ e",  # the characters are only special at the start
        " =1+1",  # a leading space already makes it text to a spreadsheet
        "'quoted",
        "",
    ]
    _, records = _utterance_records(make_conversation, texts)

    assert sorted(r["text"] for r in records) == sorted(texts)
    assert {r["moderation_category"] for r in records} == {""}  # NULL stays empty
    assert all(r["created_time"].startswith("20") for r in records)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "text", [*FORMULA_STARTS, "'=1+1", "''-x", "'quoted", "plain", "", "'"]
)
def test_removing_the_apostrophe_gives_back_exactly_what_was_typed(
    make_conversation, text
):
    """Including text that itself began with an apostrophe and then a formula
    character: it gets one more, so the escaping can always be undone."""
    _, (record,) = _utterance_records(make_conversation, [text])

    assert _unescape(record["text"]) == text


@pytest.mark.django_db
def test_keystroke_export_escapes_ids_but_never_numbers():
    Keystroke.objects.create(
        conversation_id="=R_1",
        total_time_on_page=-12.5,
        total_time_away_from_page=1.0,
        keystroke_count=-40,
        timestamp=timezone.now(),
    )

    response = _export(_client_for("Data viewer"), "keystroke", KeystrokeResource)
    header, row = _csv_rows(response)
    record = dict(zip(header, row, strict=True))

    assert record["conversation_id"] == "'=R_1"
    # A negative number is a number, not a formula: it must stay one.
    assert record["keystroke_count"] == "-40"
    assert record["total_time_on_page"] == "-12.5"
    assert response.content.startswith(b"\xef\xbb\xbf")


@pytest.mark.django_db
def test_ids_are_escaped_the_same_way_in_every_table(make_conversation):
    """So the files still join on them."""
    conversation = make_conversation(
        conversation_id="=R_1", study_name="+study", user_group="-control"
    )
    Utterance.objects.create(conversation=conversation, speaker_id="user", text="hi")
    client = _client_for("Data viewer")

    _, c = _csv_rows(_export(client, "conversation", ConversationResource))
    _, u = _csv_rows(_export(client, "utterance", UtteranceResource))

    assert c[0] == u[0] == "'=R_1"
    assert (c[2], c[3]) == (u[1], u[2]) == ("'+study", "'-control")
    assert c[-1] == "1"  # message_count


@pytest.mark.django_db
def test_bot_export_is_escaped_through_the_same_mixin(make_bot):
    make_bot(name="bot_formula", prompt="=HYPERLINK(1)")

    response = _export(_client_for("Data viewer"), "bot", BotResource)
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))

    assert [r["prompt"] for r in rows if r["name"] == "bot_formula"] == [
        "'=HYPERLINK(1)"
    ]


def test_only_text_formats_are_offered():
    """XLSX is not installed, so it is not offered. If that changes, decide
    whether it needs escaping (openpyxl writes a string starting with "=" as a
    formula) and add it to FORMULA_ESCAPED_FORMATS in chatbot/admin.py."""
    from import_export.formats.base_formats import DEFAULT_FORMATS

    assert [f.__name__ for f in DEFAULT_FORMATS] == [
        "CSV",
        "TSV",
        "JSON",
        "YAML",
        "HTML",
    ]


def test_every_export_is_formula_safe():
    """Escaping needs both halves; an Export button added without them is a hole."""
    admins = [
        admin.site.get_model_admin(m)
        for m in apps.get_models()
        if admin.site.is_registered(m)
    ]
    exporting = [a for a in admins if isinstance(a, ExportMixin)]

    assert len(exporting) == 5  # conversations, utterances, keystrokes, surveys, bots
    for model_admin in exporting:
        assert isinstance(model_admin, FormulaSafeExportMixin), model_admin
        for resource in model_admin.get_export_resource_classes(None):
            assert issubclass(resource, FormulaSafeResource), resource
