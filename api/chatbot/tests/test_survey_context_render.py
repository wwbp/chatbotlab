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

from chatbot.services.survey import normalize_answers, render_survey_context

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


# ── Flat mapping form, for survey tools that cannot express nested arrays ─────


def test_flat_mapping_is_normalized_to_question_answer_pairs():
    """
    Qualtrics' Web Service body is flat key/value rows, so a nested array of
    objects is awkward to build there. A {question: answer} mapping carries the
    same information and is normalized to the canonical list on the way in.
    """
    assert normalize_answers({"How are you?": "Fine", "Pets?": "A goldfish"}) == [
        {"question": "How are you?", "answer": "Fine"},
        {"question": "Pets?", "answer": "A goldfish"},
    ]


def test_flat_mapping_preserves_question_order():
    mapping = {f"Question {i}": f"Answer {i}" for i in range(20)}
    normalized = normalize_answers(mapping)

    assert [entry["question"] for entry in normalized] == list(mapping)


def test_canonical_list_passes_through_unchanged():
    assert normalize_answers(ANSWERS) == ANSWERS


@pytest.mark.parametrize("empty", [{}, []])
def test_empty_answers_normalize_to_an_empty_list(empty):
    assert normalize_answers(empty) == []


def test_non_string_values_are_stringified():
    """A survey tool may send a numeric rating; the prompt needs text."""
    assert normalize_answers({"Rating out of 10": 7}) == [
        {"question": "Rating out of 10", "answer": "7"},
    ]


@pytest.mark.parametrize("bad", ["a string", 5, None])
def test_unsupported_shapes_normalize_to_none(bad):
    assert normalize_answers(bad) is None


def test_nested_values_are_rejected():
    """A nested structure has no sensible rendering, so it is not guessed at."""
    assert normalize_answers({"Q": {"nested": "object"}}) is None
    assert normalize_answers({"Q": ["a", "list"]}) is None


def test_render_accepts_a_flat_mapping():
    """End to end: the renderer works from either shape."""
    assert (
        render_survey_context(
            PREAMBLE, normalize_answers({"Pets?": "A goldfish named Wallace"})
        )
        == f"{PREAMBLE}\n\nQ: Pets?\nA: A goldfish named Wallace"
    )


def test_a_single_unwrapped_entry_is_refused_as_ambiguous():
    """
    {"question": ..., "answer": ...} could be read as a flat mapping with two
    questions named "question" and "answer". That is almost certainly a
    forgotten enclosing list, and guessing would store nonsense silently.
    """
    assert normalize_answers({"question": "Pets?", "answer": "A goldfish"}) is None
