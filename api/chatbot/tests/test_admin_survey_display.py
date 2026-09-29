"""
Tests for the survey-context columns in the admin (chatbot.admin)

These exist so a researcher can open the admin, scan a study, and tell at a
glance which participants actually reached the bot with their survey answers —
rather than reading raw JSON or a truncated prompt.

The distinction that matters: answers being *linked* to a conversation and
answers being *used* in a prompt are different things. A control-condition bot
links them and deliberately does not use them, and a mis-wired survey links
nothing at all. The columns have to tell those apart.

Coverage:
  - Conversation list shows how many answers were linked, and flags none
  - Conversation detail renders the answers readably rather than as raw JSON
  - Utterance list distinguishes used / linked-but-unused / none
"""

import html

import pytest

from chatbot.admin import ConversationAdmin, UtteranceAdmin
from chatbot.models import Conversation, Utterance

ANSWERS = [
    {"question": "What's been on your mind lately?", "answer": "My goldfish Wallace"},
    {"question": "How stressed have you felt?", "answer": "Very"},
]


@pytest.fixture
def conversation_admin():
    from django.contrib.admin.sites import site

    return ConversationAdmin(Conversation, site)


@pytest.fixture
def utterance_admin():
    from django.contrib.admin.sites import site

    return UtteranceAdmin(Utterance, site)


# ── Conversation list column ──────────────────────────────────────────────────


@pytest.mark.django_db
def test_conversation_list_shows_linked_answer_count(
    conversation_admin, make_conversation
):
    conversation = make_conversation(survey_context=ANSWERS)

    assert "2" in conversation_admin.survey_answers(conversation)


@pytest.mark.django_db
def test_conversation_list_flags_a_participant_with_no_answers(
    conversation_admin, make_conversation
):
    """
    The silent failure this whole feature is most prone to: the participant
    reached the bot but their answers never arrived.
    """
    conversation = make_conversation(survey_context=None)

    assert "none" in conversation_admin.survey_answers(conversation).lower()


# ── Conversation detail rendering ─────────────────────────────────────────────


@pytest.mark.django_db
def test_conversation_detail_renders_questions_and_answers(
    conversation_admin, make_conversation
):
    conversation = make_conversation(survey_context=ANSWERS)
    # Unescaped for comparison: the rendering escapes participant text, which
    # test_participant_text_is_escaped below pins deliberately.
    rendered = html.unescape(conversation_admin.survey_context_readable(conversation))

    assert "What's been on your mind lately?" in rendered
    assert "My goldfish Wallace" in rendered
    assert "How stressed have you felt?" in rendered


@pytest.mark.django_db
def test_conversation_detail_handles_no_answers(conversation_admin, make_conversation):
    conversation = make_conversation(survey_context=None)

    assert (
        "no survey answers"
        in conversation_admin.survey_context_readable(conversation).lower()
    )


# ── Utterance list column ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_utterance_shows_context_was_used(utterance_admin, make_conversation):
    """The answers are present in the prompt that produced this message."""
    conversation = make_conversation(survey_context=ANSWERS)
    utterance = Utterance.objects.create(
        conversation=conversation,
        speaker_id="assistant",
        text="Hello",
        instruction_prompt="Base prompt.\n\nIntro:\n\nQ: What's been on your mind lately?\nA: My goldfish Wallace",
    )

    assert "used" in utterance_admin.survey_context_state(utterance).lower()


@pytest.mark.django_db
def test_utterance_distinguishes_linked_but_unused(utterance_admin, make_conversation):
    """A control-condition bot: answers linked, deliberately not in the prompt."""
    conversation = make_conversation(survey_context=ANSWERS)
    utterance = Utterance.objects.create(
        conversation=conversation,
        speaker_id="assistant",
        text="Hello",
        instruction_prompt="Base prompt only.",
    )

    state = utterance_admin.survey_context_state(utterance).lower()
    assert "not used" in state


@pytest.mark.django_db
def test_utterance_shows_none_when_nothing_was_linked(
    utterance_admin, make_conversation
):
    conversation = make_conversation(survey_context=None)
    utterance = Utterance.objects.create(
        conversation=conversation,
        speaker_id="assistant",
        text="Hello",
        instruction_prompt="Base prompt only.",
    )

    assert "—" in utterance_admin.survey_context_state(utterance)


@pytest.mark.django_db
def test_participant_text_is_escaped(conversation_admin, make_conversation):
    """
    Survey answers are participant-supplied text rendered into admin HTML, so
    they must not be able to inject markup into the page.
    """
    conversation = make_conversation(
        survey_context=[
            {"question": "Anything else?", "answer": "<script>alert(1)</script>"},
        ],
    )
    rendered = conversation_admin.survey_context_readable(conversation)

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
