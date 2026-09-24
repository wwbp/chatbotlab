"""
Survey context: answers collected in a survey tool (Qualtrics, LimeSurvey,
REDCap) before the participant reaches the chatbot, fed to the bot as context.

The flow has three steps, each recorded in the database so a conversation can
be reproduced after the fact:

  1. The survey tool POSTs the participant's answers to /api/survey_response/,
     which stores them on a SurveyResponse row.
  2. /api/initialize_conversation/ looks that row up and snapshots a copy onto
     Conversation.survey_context, freezing what this conversation actually saw.
  3. render_survey_context() turns that snapshot into a block of text appended
     to the bot's system prompt, which every Utterance records in
     instruction_prompt.

Whether a bot uses any of this is controlled by Bot.survey_context_preamble:
leave it blank and nothing is appended, which is how a control condition is
configured. See docs/survey-integration/qualtrics.rst.
"""

import hmac
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import SurveyIngestToken, SurveyResponse

logger = logging.getLogger(__name__)


def render_survey_context(preamble, answers):
    """
    Render survey answers as a block of text for a bot's system prompt.

    Pure: no database access and no I/O, so it is cheap to test and safe to
    call from either the chat or the follow-up path.

    Args:
        preamble: Bot.survey_context_preamble — the sentence introducing the
            answers to the model. Blank means this bot does not use survey
            context, and nothing is rendered.
        answers: list of {"question": ..., "answer": ...} dicts, in the order
            they were asked. Neither the number of questions nor the length of
            an answer is capped. Entries missing either key are skipped, so a
            row hand-edited in the admin cannot break a live conversation.

    Returns:
        The rendered block, or "" when there is nothing to append. An empty
        result is the normal no-op path — a bot that opts out and a
        participant whose survey data never arrived both land here.
    """
    preamble = (preamble or "").strip()
    if not preamble:
        return ""

    if not isinstance(answers, list):
        # None when no survey data was linked to this conversation. Anything
        # else means malformed data, which we skip rather than raise on —
        # mid-conversation is the worst possible place to fail.
        if answers is not None:
            logger.warning(
                "Ignoring survey answers of unexpected type %s", type(answers).__name__
            )
        return ""

    blocks = [
        f"Q: {entry['question']}\nA: {entry['answer']}"
        for entry in answers
        if isinstance(entry, dict) and "question" in entry and "answer" in entry
    ]
    if not blocks:
        return ""

    return preamble + "\n\n" + "\n\n".join(blocks)


def context_for_prompt(bot, conversation, *, is_followup=False):
    """
    Decide whether this conversation's survey context applies to a prompt.

    Pure. Regular chat always uses whatever was snapshotted at init; idle
    follow-ups additionally respect Bot.survey_context_in_followup, so a study
    can feed survey answers into the conversation without repeating them in
    unprompted nudges.

    Returns the answers list, or None when no context should be appended.
    """
    if is_followup and not bot.survey_context_in_followup:
        return None
    return conversation.survey_context


# Top-level keys that are the request's own fields rather than a question.
RESERVED_KEYS = frozenset({"survey_id", "participant_id", "answers"})


def answers_from_payload(data):
    """
    Pull the answers out of a request body, in whichever shape it arrived.

    Pure. Prefers an explicit "answers" field. Failing that, treats every other
    top-level key as a question and its value as the answer.

    The fallback exists for Qualtrics. Its Web Service editor escapes a String
    body parameter correctly, but substitutes piped text verbatim into a raw
    JSON value — so an answer containing a double quote or a line break
    produces a malformed request. Sending each question as its own parameter
    sidesteps that: the survey tool does the escaping it is good at, and we
    never ask it to build nested JSON around text it does not control.
    """
    if "answers" in data:
        return data["answers"]

    return {k: v for k, v in data.items() if k not in RESERVED_KEYS}


def normalize_answers(answers):
    """
    Accept either supported answers shape and return the canonical one.

    Pure. Two shapes are accepted because survey tools differ:

      canonical  [{"question": ..., "answer": ...}, ...]
      flat       {"<question>": "<answer>", ...}

    The flat mapping exists for Qualtrics, whose Web Service body is a list of
    flat key/value rows — a nested array of objects is painful to build there,
    while a single JSON object is straightforward. Both carry the same
    information, and normalizing here means everything downstream (storage,
    rendering, the admin, analysis) only ever sees one shape.

    Returns the canonical list, or None if the input is neither shape.
    Question order is preserved: JSON objects keep their insertion order, and
    that is the order the questions were asked in.
    """
    if isinstance(answers, list):
        return answers

    if not isinstance(answers, dict):
        return None

    # A single canonical entry sent unwrapped — {"question": ..., "answer": ...}
    # — would otherwise parse as a flat mapping with two questions literally
    # named "question" and "answer". That is almost certainly a caller who
    # forgot the enclosing list, so refuse rather than store nonsense.
    if set(answers) == {"question", "answer"}:
        return None

    normalized = []
    for question, answer in answers.items():
        # A nested value has no sensible rendering, so refuse rather than guess.
        if isinstance(answer, (dict, list)):
            return None
        # A survey tool may send a number for a rating; the prompt needs text.
        normalized.append({"question": str(question), "answer": str(answer)})

    return normalized


def validate_answers(answers):
    """
    Check the shape of an incoming answers payload.

    Pure. Returns an error message describing the first problem found, or None
    when the payload is well-formed. Validating here, at the boundary, means a
    survey tool sending the wrong shape sees a 400 while someone is still
    watching — instead of the problem surfacing as missing context weeks later.

    An empty list is valid: a participant may have skipped every question.
    """
    if not isinstance(answers, list):
        return (
            "'answers' must be either a list of {'question': ..., 'answer': ...} "
            "objects, or a flat {'<question>': '<answer>'} mapping. Nested values "
            "inside a flat mapping are not supported."
        )

    for i, entry in enumerate(answers):
        if not isinstance(entry, dict):
            return f"answers[{i}] must be an object, got {type(entry).__name__}."
        missing = [key for key in ("question", "answer") if key not in entry]
        if missing:
            return f"answers[{i}] is missing {' and '.join(missing)}."

    return None


def _token_is_valid(request):
    """
    Compare the request's shared secret against the configured one.

    Fails closed: a deployment that never set SURVEY_INGEST_TOKEN rejects every
    request rather than accepting anonymous participant data.
    """
    provided = request.META.get("HTTP_X_SURVEY_TOKEN", "")
    if not provided:
        return False

    # Two sources, either of which is enough: the environment variable, for
    # deployments that manage the secret as infrastructure, and tokens issued
    # from the admin panel, so a researcher can start a study without a
    # redeploy. compare_digest keeps the check constant-time either way.
    candidates = [getattr(settings, "SURVEY_INGEST_TOKEN", "") or ""]
    candidates += SurveyIngestToken.objects.filter(is_active=True).values_list(
        "token", flat=True
    )
    candidates = [c for c in candidates if c]

    if not candidates:
        logger.error(
            "No survey ingest token is configured — rejecting survey response. "
            "Issue one under 'Survey ingest tokens' in the admin panel, or set "
            "the SURVEY_INGEST_TOKEN environment variable.",
        )
        return False

    provided_bytes = provided.encode()
    # Check every candidate rather than short-circuiting, so the time taken
    # does not reveal which token matched.
    return any(
        hmac.compare_digest(provided_bytes, candidate.encode())
        for candidate in candidates
    )


@csrf_exempt
@require_POST
def survey_response(request):
    """
    Receive a participant's survey answers from the survey tool.

    Called server-to-server (e.g. a Qualtrics Web Service element) while the
    participant is still in the survey, before they reach the chatbot. The
    answers are keyed by (survey_id, participant_id), which is what
    initialize_conversation later looks them up by.

    Authenticated with the X-Survey-Token header. Repeat calls for the same
    participant overwrite the stored answers, so the survey tool is free to
    retry and participants are free to restart the survey.
    """
    if not _token_is_valid(request):
        return JsonResponse({"error": "Invalid or missing survey token."}, status=403)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError as e:
        # Survey tools do not escape piped answers, so an answer containing a
        # quote or a newline arrives as broken JSON. Report where it broke:
        # "Invalid JSON format" alone leaves whoever configured the survey with
        # nothing to act on, and this is the failure they are most likely to
        # hit. The parser's message carries a position and a reason but never
        # the body, so participant answers stay out of responses and logs.
        logger.warning(
            "Malformed JSON from survey tool: %s (body %d bytes, content-type %s)",
            e.msg,
            len(request.body or b""),
            request.META.get("CONTENT_TYPE", "unset"),
        )
        return JsonResponse(
            {
                "error": (
                    f"Invalid JSON format: {e.msg} at line {e.lineno} column "
                    f"{e.colno}. If an answer contains a double quote or a line "
                    "break, your survey tool has not escaped it — see the "
                    "Qualtrics integration guide."
                ),
            },
            status=400,
        )

    if not isinstance(data, dict):
        return JsonResponse(
            {"error": "Request body must be a JSON object."}, status=400
        )

    survey_id = data.get("survey_id")
    participant_id = data.get("participant_id")
    if not survey_id or not participant_id:
        return JsonResponse(
            {"error": "Both 'survey_id' and 'participant_id' are required."},
            status=400,
        )

    # Normalize first, so the stored shape is canonical no matter which form
    # the survey tool was able to send.
    answers = normalize_answers(answers_from_payload(data))
    error = validate_answers(answers)
    if error:
        return JsonResponse({"error": error}, status=400)

    try:
        _, created = SurveyResponse.objects.update_or_create(
            survey_id=survey_id,
            participant_id=participant_id,
            defaults={"answers": answers},
        )
    except Exception:
        logger.exception("Error saving survey response")
        return JsonResponse({"error": "An unexpected error occurred."}, status=500)

    logger.info(
        "Survey response %s for participant %s in survey %s (%d answers)",
        "created" if created else "updated",
        participant_id,
        survey_id,
        len(answers),
    )
    return JsonResponse(
        {
            "message": "Survey response saved",
            "created": created,
            "answer_count": len(answers),
        },
    )
