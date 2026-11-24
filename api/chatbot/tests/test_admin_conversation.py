"""
The Conversation admin is where researchers read and audit sessions.

Before this, reading one conversation meant paging through the Utterance list
(newest first, text cut at 100 characters), and the Conversation list rows were
dominated by raw bot_config JSON. These tests pin what replaced that:

  - the change page opens on a chronological, full-text transcript (escaped,
    moderated turns marked) and shows bot_config readably, not as JSON
  - the list is slim, message counts are annotated (sortable, no per-row query)
  - "Session health" filter finds broken sessions
  - cross-links from one conversation to its keystrokes, survey responses,
    utterances and avatar actually filter to that conversation
  - Data viewers read it; Config editors (no participant data) get 403
"""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group, User
from django.test import Client, RequestFactory
from django.urls import resolve
from django.utils import timezone

from chatbot.models import Avatar, Keystroke, SurveyResponse, Utterance

LIST_URL = "/api/admin/chatbot/conversation/"


def change_url(conversation):
    return f"{LIST_URL}{conversation.pk}/change/"


def _client(*groups, superuser=False):
    user = User.objects.create_user(
        f"u_{uuid.uuid4().hex[:8]}", password="x", is_staff=True
    )
    user.is_superuser = superuser
    user.save()
    user.groups.add(*Group.objects.filter(name__in=groups))
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def su(db):
    return _client(superuser=True)


def say(conversation, speaker, text, minutes, **kwargs):
    """Utterance at a fixed offset; created_time is auto_now_add, so set after."""
    u = Utterance.objects.create(
        conversation=conversation, speaker_id=speaker, text=text, **kwargs
    )
    Utterance.objects.filter(pk=u.pk).update(
        created_time=timezone.now() - timedelta(hours=1) + timedelta(minutes=minutes)
    )
    return u


# ── Transcript ────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_transcript_is_chronological_with_full_text(su, make_conversation):
    conv = make_conversation()
    long_reply = "word " * 60 + "THE-END"  # longer than the old 100-char preview
    say(conv, "assistant", long_reply, minutes=2)
    say(conv, "user", "first message", minutes=1)

    html = su.get(change_url(conv)).content.decode()

    assert "THE-END" in html
    assert html.index("first message") < html.index("THE-END")


@pytest.mark.django_db
def test_transcript_escapes_html(su, make_conversation):
    conv = make_conversation()
    say(conv, "user", "<script>alert(1)</script>", minutes=1)

    html = su.get(change_url(conv)).content.decode()

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.django_db
def test_transcript_marks_moderated_turns(su, make_conversation):
    conv = make_conversation()
    say(conv, "user", "flagged text", minutes=1, moderation_category="self-harm")

    html = su.get(change_url(conv)).content.decode()

    assert "tr-moderated" in html
    assert 'class="st-warn">self-harm' in html


@pytest.mark.django_db
def test_empty_transcript_says_no_messages(su, make_conversation):
    html = su.get(change_url(make_conversation())).content.decode()

    assert "No messages." in html


@pytest.mark.django_db
def test_bot_config_is_readable_not_raw_json(su, make_conversation):
    config = {"prompt": "You are <kind>.\nBe brief.", "temperature": 0.7}
    conv = make_conversation(bot_config=json.dumps(config))

    html = su.get(change_url(conv)).content.decode()

    assert "You are &lt;kind&gt;." in html
    assert "<dt>temperature</dt><dd>0.7</dd>" in html
    assert '"temperature": 0.7' not in html


# ── List ──────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_list_does_not_show_bot_config_json(su, make_conversation):
    make_conversation(bot_config=json.dumps({"prompt": "SECRET-PROMPT"}))

    html = su.get(LIST_URL).content.decode()

    assert "SECRET-PROMPT" not in html
    assert "Condition" in html


@pytest.mark.django_db
def test_message_count_is_sortable(su, make_conversation):
    busy, quiet = make_conversation(), make_conversation()
    for i in range(3):
        say(busy, "user", f"m{i}", minutes=i)
    say(quiet, "user", "only", minutes=0)

    response = su.get(LIST_URL)
    columns = list(response.context["cl"].list_display)
    o = columns.index("message_count")
    descending = su.get(f"{LIST_URL}?o=-{o}").context["cl"].result_list

    assert [c.pk for c in descending][:2] == [busy.pk, quiet.pk]
    assert descending[0].message_count == 3


@pytest.mark.django_db
def test_changelist_has_no_per_row_queries(
    su, make_conversation, django_assert_max_num_queries
):
    for _ in range(10):
        conv = make_conversation()
        say(conv, "user", "hi", minutes=0)
        say(conv, "assistant", "hello", minutes=1)

    su.get(LIST_URL)  # warm session/content-type caches
    with django_assert_max_num_queries(20):
        assert su.get(LIST_URL).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("health", "expected"),
    [("no_reply", {"silent"}), ("short", {"silent", "short"}), ("no_survey", None)],
)
def test_session_health_filter(su, make_conversation, health, expected):
    convs = {
        "silent": make_conversation(survey_context=[{"q": 1}]),
        "short": make_conversation(survey_context=[{"q": 1}]),
        "healthy": make_conversation(survey_context=[{"q": 1}]),
        "no_answers_null": make_conversation(survey_context=None),
        "no_answers_empty": make_conversation(survey_context=[]),
    }
    say(convs["silent"], "user", "hello?", minutes=0)
    say(convs["short"], "user", "hi", minutes=0)
    say(convs["short"], "assistant", "hello", minutes=1)
    for name in ("healthy", "no_answers_null", "no_answers_empty"):
        for i in range(2):
            say(convs[name], "user", "hi", minutes=2 * i)
            say(convs[name], "assistant", "hello", minutes=2 * i + 1)
    if expected is None:
        expected = {"no_answers_null", "no_answers_empty"}

    response = su.get(f"{LIST_URL}?health={health}")

    got = {c.pk for c in response.context["cl"].result_list}
    assert got == {convs[name].pk for name in expected}


@pytest.mark.django_db
def test_search_by_exact_conversation_id(su, make_conversation):
    wanted = make_conversation(conversation_id="R_abc123")
    make_conversation(conversation_id="R_abc1234")

    response = su.get(f"{LIST_URL}?q=R_abc123")

    assert [c.pk for c in response.context["cl"].result_list] == [wanted.pk]


# ── Cross-links ───────────────────────────────────────────────────────────────


def _href(html, startswith):
    start = html.index(f'href="{startswith}')
    return html[start + 6 : html.index('"', start + 6)].replace("&amp;", "&")


def _rows(client, url):
    """The rows a changelist URL selects, without rendering the page.

    Resolving through the admin (not just matching the URL text) proves the
    admin accepts the lookup and applies it; rendering is the target admin's
    own business and tested there.
    """
    model_admin = resolve(url.partition("?")[0]).func.model_admin
    request = RequestFactory().get(url)
    request.user = User.objects.get(pk=client.session["_auth_user_id"])
    changelist = model_admin.get_changelist_instance(request)
    return list(changelist.get_queryset(request))


@pytest.mark.django_db
def test_cross_links_resolve_and_filter(su, make_conversation):
    conv = make_conversation(participant_id="P-42")
    other = make_conversation(participant_id="P-7")
    now = timezone.now()
    for cid, count in ((conv.conversation_id, 30), (conv.conversation_id, 12)):
        Keystroke.objects.create(
            conversation_id=cid,
            keystroke_count=count,
            total_time_on_page=1,
            total_time_away_from_page=0,
            timestamp=now,
        )
    Keystroke.objects.create(
        conversation_id=other.conversation_id,
        keystroke_count=99,
        total_time_on_page=1,
        total_time_away_from_page=0,
        timestamp=now,
    )
    SurveyResponse.objects.create(survey_id="S", participant_id="P-42", answers=[])
    SurveyResponse.objects.create(survey_id="S", participant_id="P-7", answers=[])
    late = say(conv, "assistant", "second", minutes=2)
    early = say(conv, "user", "first", minutes=1)
    say(other, "user", "elsewhere", minutes=1)

    html = su.get(change_url(conv)).content.decode()

    assert "42 keystrokes" in html
    keystrokes = _rows(su, _href(html, "/api/admin/chatbot/keystroke/?"))
    assert {k.conversation_id for k in keystrokes} == {conv.conversation_id}
    surveys = _rows(su, _href(html, "/api/admin/chatbot/surveyresponse/?"))
    assert [s.participant_id for s in surveys] == ["P-42"]
    utterances = _rows(su, _href(html, "/api/admin/chatbot/utterance/?"))
    assert [u.pk for u in utterances] == [early.pk, late.pk]


@pytest.mark.django_db
def test_utterance_link_is_chronological_for_data_viewer_too(make_conversation):
    """The sort is a column index; the action checkbox shifts it per user."""
    conv = make_conversation()
    late = say(conv, "assistant", "second", minutes=2)
    early = say(conv, "user", "first", minutes=1)
    viewer = _client("Data viewer")

    html = viewer.get(change_url(conv)).content.decode()
    utterances = _rows(viewer, _href(html, "/api/admin/chatbot/utterance/?"))

    assert [u.pk for u in utterances] == [early.pk, late.pk]


@pytest.mark.django_db
def test_avatar_thumbnail_when_conversation_has_one(su, make_bot, make_conversation):
    bot = make_bot()
    conv = make_conversation(bot=bot)
    Avatar.objects.create(
        bot=bot, bot_conversation=conv.conversation_id, participant_avatar="a.png"
    )

    with patch("chatbot.admin.get_presigned_url", return_value="https://s3.test/a.png"):
        html = su.get(change_url(conv)).content.decode()

    assert 'src="https://s3.test/a.png"' in html


# ── Roles ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_data_viewer_reads_conversation_and_config_editor_cannot(make_conversation):
    conv = make_conversation()
    say(conv, "user", "hello there", minutes=0)

    viewer = _client("Data viewer").get(change_url(conv))
    assert viewer.status_code == 200
    assert "hello there" in viewer.content.decode()
    assert 'name="_save"' not in viewer.content.decode()

    assert _client("Config editor").get(change_url(conv)).status_code == 403
    assert _client("Config editor").get(LIST_URL).status_code == 403
