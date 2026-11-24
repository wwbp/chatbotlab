"""
Tests for the Utterance list in the admin (UtteranceAdmin).

The list is where a researcher reads transcripts and finds moderation events,
so it should read like a transcript (chronological within each conversation),
lead with what was said, and filter by the conversation's study and condition.
It must also stay cheap on a large table and must not put participants' full
messages into the page HTML (a `title=` tooltip did).

Coverage:
  - columns and their order; no prompt/history previews in the list
  - long text is truncated, and the full text is nowhere in the list HTML
  - the conversation column links to the conversation, by its conversation_id
  - filters: study, condition, flagged / not flagged
  - default order is chronological within each conversation
  - the changelist's query count does not grow with the number of rows
  - the view page keeps the prompt fields, in a collapsed fieldset
  - Keystroke rows link to their conversation (looked up by conversation_id)
"""

import pytest
from django.urls import reverse

from chatbot.models import Utterance

CHANGELIST = "/api/admin/chatbot/utterance/"


def _say(conversation, text="hello", speaker="user", **kwargs):
    return Utterance.objects.create(
        conversation=conversation,
        speaker_id=speaker,
        bot_name=conversation.bot_name,
        participant_id=conversation.participant_id,
        text=text,
        **kwargs,
    )


def _rows(client, query=""):
    response = client.get(CHANGELIST + query)
    assert response.status_code == 200
    return list(response.context["cl"].result_list)


@pytest.mark.django_db
def test_columns_lead_with_time_speaker_and_message(admin_client, make_conversation):
    _say(make_conversation())

    columns = admin_client.get(CHANGELIST).context["cl"].list_display

    assert [c for c in columns if c != "action_checkbox"] == [
        "created_time",
        "speaker_id",
        "text_preview",
        "moderation_category",
        "conversation_link",
        "bot_name",
    ]


@pytest.mark.django_db
def test_list_has_no_prompt_or_history_previews(admin_client, make_conversation):
    _say(make_conversation(), instruction_prompt="SECRET PROMPT")

    html = admin_client.get(CHANGELIST).content.decode()

    assert "SECRET PROMPT" not in html
    assert "column-instruction_prompt_preview" not in html
    assert "column-chat_history_used_preview" not in html


@pytest.mark.django_db
def test_long_text_is_truncated_and_full_text_is_not_in_the_page(
    admin_client, make_conversation
):
    text = "a" * 120 + "TAIL-ONLY-IN-FULL-TEXT"
    _say(make_conversation(), text=text)

    html = admin_client.get(CHANGELIST).content.decode()

    assert "a" * 120 in html
    assert "TAIL-ONLY-IN-FULL-TEXT" not in html


@pytest.mark.django_db
def test_conversation_column_links_by_conversation_id(admin_client, make_conversation):
    conversation = make_conversation(conversation_id="R_qualtrics123")
    _say(conversation)

    html = admin_client.get(CHANGELIST).content.decode()

    url = reverse("admin:chatbot_conversation_change", args=[conversation.pk])
    assert f'href="{url}"' in html
    assert "R_qualtrics123" in html


@pytest.mark.django_db
def test_filter_by_study_and_condition(admin_client, make_conversation):
    wanted = _say(make_conversation(study_name="stress", user_group="treatment"))
    _say(make_conversation(study_name="stress", user_group="control"))
    _say(make_conversation(study_name="other", user_group="treatment"))

    rows = _rows(
        admin_client,
        "?conversation__study_name=stress&conversation__user_group=treatment",
    )

    assert rows == [wanted]


@pytest.mark.django_db
def test_flagged_filter(admin_client, make_conversation):
    conversation = make_conversation()
    flagged = _say(conversation, moderation_category="harassment")
    clean = _say(conversation)

    assert _rows(admin_client, "?flagged=yes") == [flagged]
    assert _rows(admin_client, "?flagged=no") == [clean]


@pytest.mark.django_db
def test_default_order_is_chronological_within_each_conversation(
    admin_client, make_conversation
):
    first, second = make_conversation(), make_conversation()
    a1 = _say(first)
    b1 = _say(second)
    a2 = _say(first)

    assert _rows(admin_client) == [a1, a2, b1]


@pytest.mark.django_db
def test_changelist_query_count_is_bounded(
    admin_client, make_conversation, django_assert_max_num_queries
):
    conversations = [make_conversation(survey_context=[{"q": "x"}]) for _ in range(5)]
    for i in range(50):
        _say(conversations[i % 5], text=f"message {i}", instruction_prompt="p" * 500)
    admin_client.get(CHANGELIST)  # warm the session/permission caches

    with django_assert_max_num_queries(15):
        admin_client.get(CHANGELIST)


@pytest.mark.django_db
def test_view_page_keeps_prompt_fields_collapsed(admin_client, make_conversation):
    utterance = _say(make_conversation(), instruction_prompt="THE PROMPT")

    response = admin_client.get(f"{CHANGELIST}{utterance.pk}/change/")

    assert response.status_code == 200
    fieldsets = dict(response.context["adminform"].model_admin.fieldsets)
    prompt = next(
        opts for opts in fieldsets.values() if "instruction_prompt" in opts["fields"]
    )
    assert "collapse" in prompt["classes"]
    assert "THE PROMPT" in response.content.decode()


@pytest.mark.django_db
def test_keystroke_rows_link_to_their_conversation(admin_client, make_conversation):
    """Keystroke.conversation_id is a string, not a ForeignKey: the link is
    resolved by lookup, and a keystroke with no conversation shows plain text."""
    from django.utils import timezone

    from chatbot.models import Keystroke

    conversation = make_conversation(conversation_id="R_linked")
    for cid in ("R_linked", "R_orphan"):
        Keystroke.objects.create(
            conversation_id=cid,
            total_time_on_page=1,
            total_time_away_from_page=0,
            keystroke_count=1,
            timestamp=timezone.now(),
        )

    html = admin_client.get("/api/admin/chatbot/keystroke/").content.decode()

    url = reverse("admin:chatbot_conversation_change", args=[conversation.pk])
    assert f'href="{url}">R_linked</a>' in html
    assert "R_orphan" in html
