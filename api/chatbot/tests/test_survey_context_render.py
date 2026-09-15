"""
Tests for render_survey_context()  (chatbot.services.survey)

This is the pure function that turns survey answers collected in Qualtrics
into the block of text appended to a bot's system prompt. It touches no
database and performs no I/O, so these tests need no fixtures.

Coverage:
  - Disabled by default: a blank preamble renders nothing
  - No answers renders nothing (missing survey data appends nothing at all)
  - Question/answer pairs are rendered in the order they were collected
  - Malformed entries are skipped rather than raising mid-conversation
  - A preamble with nothing left to render produces no dangling header
"""

import pytest

from chatbot.services.survey import render_survey_context

PREAMBLE = "The participant answered these questions before talking to you:"

ANSWERS = [
    {"question": "How stressed have you felt this week?", "answer": "Very stressed"},
    {"question": "What's been on your mind?", "answer": "Mostly work deadlines."},
]


# ── Disabled / empty paths ────────────────────────────────────────────────────


@pytest.mark.parametrize("preamble", ["", "   ", None])
def test_blank_preamble_renders_nothing(preamble):
    """A blank preamble is how a bot opts out — the control condition."""
    assert render_survey_context(preamble, ANSWERS) == ""


@pytest.mark.parametrize("answers", [[], None])
def test_no_answers_renders_nothing(answers):
    """No survey data means nothing is appended, not an empty header."""
    assert render_survey_context(PREAMBLE, answers) == ""


def test_non_list_answers_renders_nothing():
    """Defensive: a row hand-edited in the admin should not break a chat."""
    assert render_survey_context(PREAMBLE, {"question": "q", "answer": "a"}) == ""


# ── Rendering ─────────────────────────────────────────────────────────────────


def test_renders_preamble_then_each_pair_in_order():
    assert render_survey_context(PREAMBLE, ANSWERS) == (
        f"{PREAMBLE}\n"
        "\n"
        "Q: How stressed have you felt this week?\n"
        "A: Very stressed\n"
        "\n"
        "Q: What's been on your mind?\n"
        "A: Mostly work deadlines."
    )


def test_question_count_is_not_fixed():
    """Studies vary in how many questions they ask; nothing caps the count."""
    answers = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(50)]
    rendered = render_survey_context(PREAMBLE, answers)
    assert rendered.count("Q: ") == 50
    assert rendered.index("Q: q0") < rendered.index("Q: q49")


def test_long_answers_are_not_truncated():
    """Answer length is deliberately uncapped — free-text responses run long."""
    long_answer = "word " * 5000
    rendered = render_survey_context(
        PREAMBLE,
        [{"question": "Tell us more", "answer": long_answer}],
    )
    assert long_answer in rendered


def test_preamble_is_stripped():
    assert render_survey_context(f"  {PREAMBLE}  ", ANSWERS).startswith(PREAMBLE)


# ── Malformed entries ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_entry",
    [
        {"question": "orphan question"},
        {"answer": "orphan answer"},
        {},
        "not a dict",
        None,
    ],
)
def test_malformed_entries_are_skipped(bad_entry):
    rendered = render_survey_context(PREAMBLE, [*ANSWERS, bad_entry])
    assert rendered == render_survey_context(PREAMBLE, ANSWERS)


def test_preamble_alone_renders_nothing_when_every_entry_is_malformed():
    """No dangling header when nothing survives filtering."""
    assert render_survey_context(PREAMBLE, [{}, "junk", None]) == ""
