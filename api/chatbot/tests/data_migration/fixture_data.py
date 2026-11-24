"""Realistic, deliberately awkward data for the data-migration tests.

Run inside Django against the *source* database, after ``migrate``:

    python -c "import django; django.setup(); \
        from chatbot.tests.data_migration.fixture_data import build; build()"

Everything here is deterministic (no random, no now()) so a test can name a
row and know what is in it.
"""

import json
from datetime import UTC, datetime, timedelta, timezone

ADMIN_USER = "mig_admin"
ADMIN_PASSWORD = "fixture-only-admin-password"
AWKWARD_CONVERSATION = "conv_awkward"
EMOJI_TEXT = "Hello 😀 — 你好, مرحبا, naïve café, 👩‍👩‍👧‍👦"
LONG_TEXT_MARK = "LONG:"
N_CONVERSATIONS = 300
N_UTTERANCES = 30_000
N_KEYSTROKES = 20_000
# Rows created then deleted, so the table's AUTO_INCREMENT sits well above
# MAX(id)+1. A copy that loses the counter would hand out ids that once
# belonged to deleted rows.
N_DELETED_KEYSTROKES = 50

# India is UTC+05:30: the app stores UTC, so this must land as 21:00:15.123456
# on the previous day.
AWARE = datetime(
    2025, 3, 9, 2, 30, 15, 123456, tzinfo=timezone(timedelta(hours=5, minutes=30))
)
UTC0 = datetime(2025, 1, 1, tzinfo=UTC)

SURVEY_CONTEXT = {"b": 1, "a": [3, 1, 2], "nested": {"z": None, "y": "é😀"}}

EXTRA_TABLES = """
CREATE TABLE mig_types (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    dec_col DECIMAL(30,10) NULL,
    dbl_col DOUBLE NULL,
    flt_col FLOAT NULL,
    big_col BIGINT UNSIGNED NULL,
    bin_col VARBINARY(32) NULL,
    blob_col BLOB NULL,
    ts_col TIMESTAMP(6) NULL,
    date_col DATE NULL,
    time_col TIME(6) NULL,
    enum_col ENUM('a','b','c') NULL,
    latin_col VARCHAR(50) CHARACTER SET latin1 NULL
);
INSERT INTO mig_types
    (dec_col, dbl_col, flt_col, big_col, bin_col, blob_col, ts_col, date_col,
     time_col, enum_col, latin_col)
VALUES
    (12345678901234567890.1234567890, 0.1, 0.1, 18446744073709551615,
     X'00FF0A0D09', X'DEADBEEF00', '2025-03-08 21:00:15.123456', '2025-03-09',
     '23:59:59.999999', 'b', 'café'),
    (-0.0000000001, 1e-7, 3.4e38, 0, X'', X'', '2001-01-01 00:00:00.000001',
     '1000-01-01', '00:00:00.000001', 'a', ''),
    (0.3333333333, 0.30000000000000004, 1.5, 1, NULL, NULL, NULL, NULL, NULL,
     NULL, NULL),
    (NULL, 1.7976931348623157e308, -1.17549e-38, NULL, X'20', X'2020', NULL,
     NULL, NULL, 'c', 'trailing ');
CREATE TABLE mig_child (
    id INT AUTO_INCREMENT PRIMARY KEY,
    parent_id BIGINT NOT NULL,
    CONSTRAINT fk_mig_child_parent FOREIGN KEY (parent_id)
        REFERENCES mig_types (id) ON DELETE CASCADE
);
INSERT INTO mig_child (parent_id) VALUES (1), (1), (2);
CREATE TABLE mig_nopk (a INT NULL, b VARCHAR(20) NULL);
INSERT INTO mig_nopk VALUES (1, 'same'), (1, 'same'), (1, 'same'), (2, NULL),
    (NULL, '');
CREATE TABLE mig_composite (
    a INT NOT NULL,
    b VARCHAR(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    v TEXT NULL,
    PRIMARY KEY (a, b)
);
INSERT INTO mig_composite VALUES (1, 'a', 'x'), (1, 'A', 'y'), (2, 'a', NULL);
CREATE TABLE mig_empty (id INT PRIMARY KEY, note VARCHAR(10) NULL);
CREATE TRIGGER mig_child_bi BEFORE INSERT ON mig_child
    FOR EACH ROW SET NEW.parent_id = NEW.parent_id;
CREATE PROCEDURE mig_proc() SELECT COUNT(*) FROM mig_types;
CREATE VIEW mig_view AS SELECT id, dec_col FROM mig_types;
CREATE EVENT mig_event ON SCHEDULE EVERY 1 DAY DISABLE DO SET @mig = 1;
"""


def build():
    from django.contrib.auth.models import User
    from django.db import connection

    from chatbot.models import (
        AccessLog,
        Bot,
        Conversation,
        Keystroke,
        Model,
        Persona,
        SurveyResponse,
        Utterance,
    )

    Model.get_or_create_default_models()
    ai_model = Model.objects.order_by("id").first()
    User.objects.create_superuser(ADMIN_USER, "mig@example.org", ADMIN_PASSWORD)
    persona = Persona.objects.create(name="Wärm 🙂", instructions="Be kind.\n\tAlways.")
    bot = Bot.objects.create(
        name="mig_bot",
        prompt="You are a test assistant. " * 50,
        ai_model=ai_model,
        reading_words_per_minute=0.1 + 0.2,  # 0.30000000000000004
        writing_words_per_minute=1e-7,
        idle_time_minutes=3,
    )
    bot.personas.add(persona)

    awkward = Conversation.objects.create(
        conversation_id=AWKWARD_CONVERSATION,
        bot_name=bot.name,
        bot_config=json.dumps({"prompt": "p" * 20_000, "temperature": 0.7}),
        participant_id="P-001",
        initial_utterance="",  # empty string, not NULL
        study_name=None,  # NULL, not empty string
        user_group="",
        survey_id="SV_1",
        survey_meta_data=None,
        selected_persona=persona,
        survey_context=SURVEY_CONTEXT,
    )
    scores = {"harassment": 0.9731, "hate": 1.2e-05, "violence/graphic": 0.0}
    specials = [
        {"text": EMOJI_TEXT, "speaker_id": "user"},
        {
            "text": LONG_TEXT_MARK + "lorem ipsüm 😀 " * 70_000,
            "speaker_id": "assistant",
        },
        {"text": "", "speaker_id": "user", "bot_name": None, "participant_id": None},
        {"text": "", "speaker_id": "user", "bot_name": "", "participant_id": ""},
        {"text": "identical twin", "speaker_id": "user"},
        {"text": "identical twin", "speaker_id": "user"},
        {
            "text": "tab\there \\ back\\slash 'q' \"dq\" %s \r\n trailing  ",
            "speaker_id": "user",
        },
        {
            "text": "blocked",
            "speaker_id": "user",
            "moderation_category": "harassment",
            "moderation_scores": scores,
            "compiled_prompt": {"history": [{"role": "user", "content": "hi 😀"}]},
            "chat_history_used": json.dumps([{"role": "user", "content": "hi"}]),
            "instruction_prompt": "prompt\nwith\nnewlines",
        },
        {
            "text": "voice",
            "speaker_id": "user",
            "is_voice": True,
            "audio_file": "utterance_audio/a b.webm",
        },
    ]
    for row in specials:
        Utterance.objects.create(conversation=awkward, **row)
    # created_time is auto_now_add, so it can only be set afterwards.
    Utterance.objects.filter(conversation=awkward).update(created_time=AWARE)

    conversations = Conversation.objects.bulk_create(
        [
            Conversation(
                conversation_id=f"conv_{i:05d}",
                bot_name=bot.name,
                participant_id=f"P-{i % 97:03d}",
                study_name="study_a" if i % 2 else None,
                survey_context=[{"question": f"q{i}", "answer": "ä" * (i % 7)}],
            )
            for i in range(N_CONVERSATIONS)
        ]
    )
    conv_ids = list(
        Conversation.objects.filter(conversation_id__startswith="conv_0")
        .order_by("id")
        .values_list("id", flat=True)
    )
    assert len(conv_ids) == len(conversations)
    Utterance.objects.bulk_create(
        [
            Utterance(
                conversation_id=conv_ids[i % N_CONVERSATIONS],
                speaker_id="user" if i % 2 else "assistant",
                bot_name=bot.name if i % 2 == 0 else None,
                participant_id=f"P-{i % 97:03d}",
                text=f"message {i} 😀 " + "wörd " * (i % 40),
                is_voice=i % 11 == 0,
            )
            for i in range(N_UTTERANCES)
        ],
        batch_size=2000,
    )
    with connection.cursor() as cursor:
        # Spread the timestamps deterministically, microseconds included.
        cursor.execute(
            "UPDATE chatbot_utterance SET created_time = "
            "TIMESTAMPADD(MICROSECOND, id * 1000003, '2025-01-01 00:00:00') "
            "WHERE conversation_id <> %s",
            [awkward.id],
        )
    Keystroke.objects.bulk_create(
        [
            Keystroke(
                conversation_id=f"conv_{i % N_CONVERSATIONS:05d}",
                total_time_on_page=i / 7,
                total_time_away_from_page=i * 0.1,
                keystroke_count=i,
                timestamp=UTC0 + timedelta(microseconds=i * 999_983),
            )
            for i in range(N_KEYSTROKES + N_DELETED_KEYSTROKES)
        ],
        batch_size=5000,
    )
    last_kept = Keystroke.objects.order_by("id")[N_KEYSTROKES - 1].id
    Keystroke.objects.filter(id__gt=last_kept).delete()
    SurveyResponse.objects.bulk_create(
        [
            SurveyResponse(
                survey_id="SV_1",
                participant_id=f"P-{i:03d}",
                answers=[{"question": "How are you? 😀", "answer": "fine" * i}],
            )
            for i in range(500)
        ]
    )
    AccessLog.objects.bulk_create(
        [
            AccessLog(
                username=ADMIN_USER,
                action=AccessLog.Action.choices[0][0],
                method="GET",
                path=f"/api/admin/chatbot/conversation/{i}/change/",
                status_code=200 if i % 5 else None,
                ip="2001:db8::1" if i % 2 else "10.0.0.1",
            )
            for i in range(1000)
        ]
    )
    with connection.cursor() as cursor:
        for statement in EXTRA_TABLES.split(";\n"):
            if statement.strip():
                cursor.execute(statement)


def read_back():
    """What the app sees for the awkward conversation; printed as JSON.

    Run against the *target* after a copy: logs in with the fixture password
    (so the auth tables survived), opens the admin transcript page the
    researchers use, and reads the rows through the ORM.
    """
    from django.test import Client
    from django.test.utils import setup_test_environment

    from chatbot.models import Conversation, Utterance

    setup_test_environment()  # allows the test client's host name
    client = Client()
    logged_in = client.login(username=ADMIN_USER, password=ADMIN_PASSWORD)
    conversation = Conversation.objects.get(conversation_id=AWKWARD_CONVERSATION)
    page = client.get(f"/api/admin/chatbot/conversation/{conversation.pk}/change/")
    utterances = list(
        Utterance.objects.filter(conversation=conversation).order_by("id")
    )
    print(  # noqa: T201 - the test reads this from stdout
        json.dumps(
            {
                "logged_in": logged_in,
                "page_status": page.status_code,
                "page_has_emoji_text": "Hello 😀" in page.content.decode(),
                "survey_context": conversation.survey_context,
                "study_name": conversation.study_name,
                "initial_utterance": conversation.initial_utterance,
                "texts": [u.text[:40] for u in utterances],
                "long_len": len(utterances[1].text),
                "created_time": utterances[0].created_time.isoformat(),
                "total_utterances": Utterance.objects.count(),
            }
        )
    )
