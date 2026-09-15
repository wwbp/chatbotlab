#!/usr/bin/env python3
# ruff: noqa: T201
"""
End-to-end smoke test for survey context against a DEPLOYED environment.

Run this before wiring up a real survey. It exercises the same three calls a
study makes — deliver answers, start a conversation, talk to the bot — and
proves the bot actually read the answers, by asking it something it can only
answer from the survey context.

Doing this first separates "is the server configured correctly?" from "is my
Qualtrics survey flow correct?", which are otherwise very hard to tell apart.

Usage:
    python smoke_survey_context.py \
        --base-url https://dev.bot.wwbp.org \
        --token "$SURVEY_INGEST_TOKEN" \
        --bot-name my_staging_bot

The bot must already exist and have a "Survey context preamble" set in the
admin panel — without one it is opted out by design and this test will fail,
which is itself a useful check.

Uses only the standard library, so it runs anywhere with no install step.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

# A fact the model cannot know from anywhere except the survey answers. If it
# comes back in the reply, the context genuinely reached the prompt.
SECRET_PET = "Wallace"

ANSWERS = [
    {
        "question": "How stressed have you felt this week?",
        "answer": "Very stressed, work has been relentless.",
    },
    {
        "question": "Tell us about a pet you have.",
        "answer": f"I have a goldfish named {SECRET_PET}.",
    },
]


def post(url, payload, headers=None):
    """POST JSON and return (status, parsed_body). Never raises on HTTP errors."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return e.code, {"raw": body.decode(errors="replace")[:500]}
    except OSError as e:
        return 0, {"error": f"could not reach {url}: {e}"}


def step(number, description):
    print(f"\n[{number}] {description}")


def fail(message, detail=None):
    print(f"    FAIL — {message}")
    if detail:
        print(f"    {json.dumps(detail, indent=2)[:800]}")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", required=True, help="e.g. https://dev.bot.wwbp.org"
    )
    parser.add_argument("--token", required=True, help="SURVEY_INGEST_TOKEN")
    parser.add_argument(
        "--bot-name", required=True, help="a bot with a survey context preamble set"
    )
    parser.add_argument("--survey-id", default=f"SMOKE_{uuid.uuid4().hex[:8]}")
    parser.add_argument("--participant-id", default=f"P_{uuid.uuid4().hex[:8]}")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    conversation_id = f"smoke_{uuid.uuid4().hex[:12]}"

    print(f"Target:       {base}")
    print(f"Bot:          {args.bot_name}")
    print(f"Participant:  {args.participant_id}")
    print(f"Conversation: {conversation_id}")

    # ── 1. Deliver the answers, as the survey tool would ─────────────────────
    step(1, "POST /api/survey_response/  (what Qualtrics' Web Service does)")
    status, body = post(
        f"{base}/api/survey_response/",
        {
            "survey_id": args.survey_id,
            "participant_id": args.participant_id,
            "answers": ANSWERS,
        },
        headers={"X-Survey-Token": args.token},
    )
    if status == 403:
        fail(
            "rejected the token. Either SURVEY_INGEST_TOKEN is not set on the "
            "server, or it does not match --token. Note the server fails "
            "closed: unset means every request is refused.",
            body,
        )
    if status != 200:
        fail(f"expected 200, got {status}", body)
    print(f"    OK — stored {body.get('answer_count')} answers")

    # ── 2. Start the conversation, as the participant's browser would ────────
    step(2, "POST /api/initialize_conversation/  (what the chatbot page does)")
    status, body = post(
        f"{base}/api/initialize_conversation/",
        {
            "bot_name": args.bot_name,
            "conversation_id": conversation_id,
            "participant_id": args.participant_id,
            "survey_id": args.survey_id,
            "study_name": "survey_context_smoke_test",
        },
    )
    if status == 404:
        fail(f"no bot named '{args.bot_name}' exists on this server", body)
    if status != 200:
        fail(f"expected 200, got {status}", body)
    print("    OK — conversation created")

    # ── 3. Ask the bot something only the survey answers can tell it ─────────
    step(3, "POST /api/chatbot/  (asking the bot to recall a survey answer)")
    status, body = post(
        f"{base}/api/chatbot/",
        {
            "message": "What did I say my goldfish was named?",
            "bot_name": args.bot_name,
            "conversation_id": conversation_id,
            "participant_id": args.participant_id,
        },
    )
    if status != 200:
        fail(f"expected 200, got {status}", body)

    reply = json.dumps(body)
    print(f"    Bot replied: {reply[:300]}")

    if "mock response for load testing" in reply.lower():
        fail(
            "this server is running with MOCK_LLM=true, so it returns a canned "
            "reply instead of calling a model — step 3 cannot pass. Set "
            "MOCK_LLM=false and retry. Steps 1 and 2 above did pass, so the "
            "survey endpoint and the conversation link are working.",
        )

    if SECRET_PET.lower() not in reply.lower():
        fail(
            f"the bot did not mention '{SECRET_PET}', so the survey context did "
            f"not reach the prompt. Most likely the bot '{args.bot_name}' has an "
            "empty 'Survey context preamble' (which means opted out), or the "
            "survey_id/participant_id pair did not match between steps 1 and 2.",
        )

    print(f"\nPASS — the bot recalled '{SECRET_PET}' from the survey answers.")
    print("Survey context is working end to end on this deployment.")
    print(
        f"\nFor the full audit trail, open the admin panel and check conversation "
        f"'{conversation_id}':\n"
        "  Conversations -> Metadata -> survey context\n"
        "  Utterances    -> instruction prompt"
    )


if __name__ == "__main__":
    main()
