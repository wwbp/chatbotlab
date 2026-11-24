"""``dbcopy.py verify``: prove two databases hold the same schema and data.

A faithful copy must pass; every way a copy can be subtly wrong must fail,
naming the table and the first differing key. Each mutation is applied to the
target after a known-good copy and undone afterwards; the last test checks the
target verifies clean again, so a leaked mutation cannot hide.
"""

import json

import pytest

from .conftest import (
    DST,
    DST_DB,
    DST_PASSWORD,
    SRC,
    SRC_DB,
    admin,
    django,
    plain_copy,
    read_report,
    tool,
)
from .fixture_data import (
    AWKWARD_CONVERSATION,
    EMOJI_TEXT,
    LONG_TEXT_MARK,
    N_DELETED_KEYSTROKES,
    N_KEYSTROKES,
    N_UTTERANCES,
    SURVEY_CONTEXT,
)


def t(sql):
    """Run SQL on the target as root."""
    return admin(DST, sql, DST_DB)


def one(sql):
    return t(sql)[0][0]


def verify(tmp_path):
    result = tool("--run-dir", str(tmp_path), "verify")
    report = (
        read_report(tmp_path) if (tmp_path / "verify-report.json").exists() else None
    )
    return result, report


@pytest.fixture(scope="module")
def copied(same_flavour):
    plain_copy()
    return same_flavour


@pytest.fixture(scope="module")
def clean_report(copied, tmp_path_factory):
    result, report = verify(tmp_path_factory.mktemp("clean"))
    assert result.returncode == 0, result.stdout + result.stderr
    return report


# ── a faithful copy ──────────────────────────────────────────────────────────


def test_identical_copy_passes(clean_report):
    report = clean_report
    assert report["ok"] is True
    assert report["differences"] == []
    source_tables = {row[0] for row in admin(SRC, "SHOW TABLES", SRC_DB)}
    assert set(report["tables"]) == source_tables
    assert {"django_migrations", "chatbot_utterance", "mig_nopk"} <= source_tables

    utterances = report["tables"]["chatbot_utterance"]
    assert utterances["rows_source"] == utterances["rows_target"] == N_UTTERANCES + 9
    assert utterances["checksum_source"] == utterances["checksum_target"]
    assert len(utterances["checksum_source"]) == 64
    assert report["tables"]["mig_empty"]["rows_source"] == 0
    assert report["tables"]["mig_nopk"]["rows_target"] == 5

    keystrokes = report["tables"]["chatbot_keystroke"]
    assert keystrokes["rows_source"] == N_KEYSTROKES
    # The fixture deleted the newest rows: the counter is above MAX(id)+1.
    assert keystrokes["auto_increment_source"] > N_KEYSTROKES + N_DELETED_KEYSTROKES
    assert keystrokes["auto_increment_target"] == keystrokes["auto_increment_source"]

    # Different tables must not share a checksum by accident of construction.
    checksums = [
        v["checksum_source"] for v in report["tables"].values() if v["rows_source"]
    ]
    assert len(set(checksums)) == len(checksums)


def test_django_migrations_reported_identical(clean_report):
    migrations = clean_report["django_migrations"]
    assert migrations["identical"] is True
    assert migrations["rows_source"] == migrations["rows_target"] > 40


def test_human_summary_and_exit_code(copied, tmp_path):
    result, _ = verify(tmp_path)
    assert result.returncode == 0
    assert "IDENTICAL" in result.stdout
    assert "chatbot_utterance" in result.stdout


def test_rerun_gives_the_same_report(copied, clean_report, tmp_path):
    _, again = verify(tmp_path)
    volatile = {"generated_at", "duration_seconds"}

    def stable(report):
        return {k: v for k, v in report.items() if k not in volatile}

    assert stable(again) == stable(clean_report)


def test_verify_writes_nothing_on_dry_run(copied, tmp_path):
    result = tool("--run-dir", str(tmp_path), "verify", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout.lower()
    assert not (tmp_path / "verify-report.json").exists()


# ── every way a copy can be wrong ────────────────────────────────────────────

CONV = f"conversation_id = '{AWKWARD_CONVERSATION}'"


def _cell(table, column, where, new_expr):
    """Change one cell; return (id, undo SQL) restoring the exact original bytes."""
    row_id = one(f"SELECT id FROM {table} WHERE {where} ORDER BY id LIMIT 1")
    original = one(f"SELECT HEX({column}) FROM {table} WHERE id = {row_id}")
    restore = (
        "NULL" if original == "NULL" else f"CONVERT(UNHEX('{original}') USING utf8mb4)"
    )
    t(f"UPDATE {table} SET {column} = {new_expr} WHERE id = {row_id}")
    return row_id, f"UPDATE {table} SET {column} = {restore} WHERE id = {row_id}"


def row_deleted(_):
    t(
        "CREATE DATABASE mig_scratch;"
        "CREATE TABLE mig_scratch.k AS SELECT * FROM chatbot_keystroke WHERE id = 1234;"
        "DELETE FROM chatbot_keystroke WHERE id = 1234"
    )
    undo = "INSERT INTO chatbot_keystroke SELECT * FROM mig_scratch.k; DROP DATABASE mig_scratch"
    return undo, ("chatbot_keystroke", "row_missing_on_target", "1234")


def row_added(_):
    t(
        "INSERT INTO chatbot_keystroke (conversation_id, total_time_on_page,"
        " total_time_away_from_page, keystroke_count, timestamp)"
        " VALUES ('conv_00001', 1, 1, 1, '2025-01-01 00:00:00')"
    )
    new_id = one("SELECT MAX(id) FROM chatbot_keystroke")
    undo = f"DELETE FROM chatbot_keystroke WHERE id = {new_id}"
    return undo, ("chatbot_keystroke", "row_extra_on_target", new_id)


def row_added_in_a_gap(_):
    # Not at the end: an extra row whose key sorts before existing ones.
    t("INSERT INTO mig_composite VALUES (1, 'B', 'sneaked in')")
    undo = "DELETE FROM mig_composite WHERE a = 1 AND b = 'B'"
    return undo, ("mig_composite", "row_extra_on_target", "1,B")


def one_character_changed(_):
    # Deep inside a megabyte of text.
    row_id, undo = _cell(
        "chatbot_utterance",
        "text",
        f"text LIKE '{LONG_TEXT_MARK}%'",
        "INSERT(text, 500000, 1, 'X')",
    )
    return undo, ("chatbot_utterance", "row_differs", row_id)


def trailing_space_added(_):
    # participant_id uses a PAD SPACE collation: 'P-001' = 'P-001 ' is TRUE in
    # SQL, so a comparison done with = would miss this.
    row_id, undo = _cell(
        "chatbot_conversation", "participant_id", CONV, "CONCAT(participant_id, ' ')"
    )
    return undo, ("chatbot_conversation", "row_differs", row_id)


def emoji_replaced_by_lookalike(_):
    row_id, undo = _cell(
        "chatbot_utterance", "text", "text LIKE 'Hello %'", "REPLACE(text, '😀', '😃')"
    )
    assert (
        one(f"SELECT text LIKE '%😃%' FROM chatbot_utterance WHERE id = {row_id}")
        == "1"
    )
    return undo, ("chatbot_utterance", "row_differs", row_id)


def case_changed(_):
    # Equal under a case-insensitive collation; still not the same data.
    row_id, undo = _cell("chatbot_utterance", "text", "text = 'blocked'", "'Blocked'")
    return undo, ("chatbot_utterance", "row_differs", row_id)


def null_became_empty_string(_):
    row_id, undo = _cell("chatbot_conversation", "study_name", CONV, "''")
    return undo, ("chatbot_conversation", "row_differs", row_id)


def datetime_shifted_one_microsecond(_):
    row_id = one("SELECT id FROM chatbot_utterance WHERE text LIKE 'Hello %' LIMIT 1")
    t(
        "UPDATE chatbot_utterance SET created_time = created_time + INTERVAL 1 MICROSECOND"
        f" WHERE id = {row_id}"
    )
    undo = (
        "UPDATE chatbot_utterance SET created_time = created_time - INTERVAL 1 MICROSECOND"
        f" WHERE id = {row_id}"
    )
    return undo, ("chatbot_utterance", "row_differs", row_id)


def double_changed_in_last_digit(_):
    # 0.30000000000000004 (what 0.1 + 0.2 gives) against 0.3.
    t("UPDATE mig_types SET dbl_col = 0.3E0 WHERE id = 3")
    undo = "UPDATE mig_types SET dbl_col = 3.0000000000000004E-1 WHERE id = 3"
    return undo, ("mig_types", "row_differs", "3")


def float_changed_in_seventh_digit(_):
    # FLOAT renders with 6 significant digits: 1.500001 shows as 1.5.
    t("UPDATE mig_types SET flt_col = 1.500001 WHERE id = 3")
    return "UPDATE mig_types SET flt_col = 1.5 WHERE id = 3", (
        "mig_types",
        "row_differs",
        "3",
    )


def json_array_reordered(_):
    row_id, undo = _cell(
        "chatbot_conversation",
        "survey_context",
        CONV,
        "REPLACE(survey_context, '[3, 1, 2]', '[1, 2, 3]')",
    )
    return undo, ("chatbot_conversation", "row_differs", row_id)


def json_keys_reordered(flavour):
    # Decision: verify compares what the server stores. MariaDB keeps JSON as
    # the text the app wrote, so reordered keys are a byte-level change and
    # count as a difference. MySQL's native JSON type normalises key order on
    # write, so the "reordered" value is stored identically: nothing differs
    # and nothing should be reported.
    reordered = json.dumps(dict(sorted(SURVEY_CONTEXT.items())), ensure_ascii=False)
    assert reordered != json.dumps(SURVEY_CONTEXT, ensure_ascii=False)
    row_id, undo = _cell(
        "chatbot_conversation", "survey_context", CONV, f"'{reordered}'"
    )
    expected = (
        ("chatbot_conversation", "row_differs", row_id)
        if flavour == "mariadb"
        else None
    )
    return undo, expected


def duplicate_row_lost_in_table_without_key(_):
    # Three identical rows, no primary key: losing one changes no distinct value.
    t("DELETE FROM mig_nopk WHERE a = 1 LIMIT 1")
    return "INSERT INTO mig_nopk VALUES (1, 'same')", ("mig_nopk", "row_count", None)


def column_collation_changed(_):
    column = "TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'chatbot_conversation' AND COLUMN_NAME = 'participant_id'"
    original = one(
        f"SELECT COLLATION_NAME FROM information_schema.COLUMNS WHERE {column}"
    )
    modify = "ALTER TABLE chatbot_conversation MODIFY participant_id varchar(255) CHARACTER SET utf8mb4 COLLATE {} NOT NULL"
    t(modify.format("utf8mb4_bin"))
    return modify.format(original), (
        "chatbot_conversation",
        "schema",
        "column participant_id",
    )


def column_type_widened(_):
    t("ALTER TABLE mig_empty MODIFY note VARCHAR(11) NULL")
    return "ALTER TABLE mig_empty MODIFY note VARCHAR(10) NULL", (
        "mig_empty",
        "schema",
        "column note",
    )


def index_dropped(_):
    name = one(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE()"
        " AND TABLE_NAME = 'chatbot_accesslog' AND COLUMN_NAME = 'timestamp'"
    )
    t(f"DROP INDEX {name} ON chatbot_accesslog")
    undo = f"CREATE INDEX {name} ON chatbot_accesslog (timestamp)"
    return undo, ("chatbot_accesslog", "schema", f"index {name}")


def foreign_key_dropped(_):
    t("ALTER TABLE mig_child DROP FOREIGN KEY fk_mig_child_parent")
    undo = (
        "ALTER TABLE mig_child ADD CONSTRAINT fk_mig_child_parent FOREIGN KEY (parent_id)"
        " REFERENCES mig_types (id) ON DELETE CASCADE"
    )
    return undo, ("mig_child", "schema", "foreign key fk_mig_child_parent")


def auto_increment_lowered(_):
    # InnoDB will not go below MAX(id)+1, which is exactly what a copy that
    # drops the counter ends up with.
    original = (
        t("SHOW CREATE TABLE chatbot_keystroke")[0][1]
        .split("AUTO_INCREMENT=")[1]
        .split()[0]
    )
    t("ALTER TABLE chatbot_keystroke AUTO_INCREMENT = 1")
    undo = f"ALTER TABLE chatbot_keystroke AUTO_INCREMENT = {original}"
    return undo, ("chatbot_keystroke", "auto_increment", None)


def auto_increment_raised(_):
    # A faithful copy carries the counter exactly; ahead means the target was
    # written to (or the copy came from a later state of the source).
    original = (
        t("SHOW CREATE TABLE chatbot_keystroke")[0][1]
        .split("AUTO_INCREMENT=")[1]
        .split()[0]
    )
    t(f"ALTER TABLE chatbot_keystroke AUTO_INCREMENT = {int(original) + 10}")
    undo = f"ALTER TABLE chatbot_keystroke AUTO_INCREMENT = {original}"
    return undo, ("chatbot_keystroke", "auto_increment", None)


def table_missing(_):
    t("DROP TABLE mig_empty")
    undo = "CREATE TABLE mig_empty (id INT PRIMARY KEY, note VARCHAR(10) NULL)"
    return undo, ("mig_empty", "table_missing_on_target", None)


def table_extra(_):
    t("CREATE TABLE mig_stowaway (id INT PRIMARY KEY)")
    return "DROP TABLE mig_stowaway", ("mig_stowaway", "table_extra_on_target", None)


def migration_record_missing(_):
    row_id = one("SELECT MAX(id) FROM django_migrations")
    t(
        "CREATE DATABASE mig_scratch;"
        f"CREATE TABLE mig_scratch.m AS SELECT * FROM django_migrations WHERE id = {row_id};"
        f"DELETE FROM django_migrations WHERE id = {row_id}"
    )
    undo = "INSERT INTO django_migrations SELECT * FROM mig_scratch.m; DROP DATABASE mig_scratch"
    return undo, ("django_migrations", "row_missing_on_target", row_id)


def database_default_collation_changed(_):
    original = one("SELECT @@collation_database")
    other = "utf8mb4_bin" if original != "utf8mb4_bin" else "utf8mb4_general_ci"
    t(f"ALTER DATABASE {DST_DB} COLLATE {other}")
    return f"ALTER DATABASE {DST_DB} COLLATE {original}", (
        None,
        "database",
        "default collation",
    )


MUTATIONS = [
    row_deleted,
    row_added,
    row_added_in_a_gap,
    one_character_changed,
    trailing_space_added,
    emoji_replaced_by_lookalike,
    case_changed,
    null_became_empty_string,
    datetime_shifted_one_microsecond,
    double_changed_in_last_digit,
    float_changed_in_seventh_digit,
    json_array_reordered,
    json_keys_reordered,
    duplicate_row_lost_in_table_without_key,
    column_collation_changed,
    column_type_widened,
    index_dropped,
    foreign_key_dropped,
    auto_increment_lowered,
    auto_increment_raised,
    table_missing,
    table_extra,
    migration_record_missing,
    database_default_collation_changed,
]


@pytest.mark.parametrize("mutate", MUTATIONS, ids=lambda m: m.__name__)
def test_verify_detects(mutate, copied, clean_report, tmp_path):
    undo, expected = mutate(copied)
    try:
        result, report = verify(tmp_path)
    finally:
        t(undo)

    if expected is None:  # documented non-difference (MySQL native JSON key order)
        assert result.returncode == 0, result.stdout
        return

    table, kind, key = expected
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["ok"] is False
    found = [(d["table"], d["kind"], d["key"]) for d in report["differences"]]
    assert (table, kind, key) in found, found
    # Nothing unrelated is blamed.
    assert {d["table"] for d in report["differences"]} == {table}
    assert "DIFFERENT" in result.stdout
    for part in (table, key):
        if part:
            assert part in result.stdout
    if mutate is migration_record_missing:
        assert report["django_migrations"]["identical"] is False


def test_target_is_clean_after_all_mutations(copied, clean_report, tmp_path):
    result, report = verify(tmp_path)
    assert result.returncode == 0, result.stdout
    for name, table in report["tables"].items():
        assert (
            table["checksum_target"] == clean_report["tables"][name]["checksum_source"]
        )


def test_app_reads_the_conversation_from_the_target(copied):
    # Last on purpose: logging in writes (last_login, a session row, an access
    # log row), so after this the target no longer matches the source. The
    # same holds in a real cutover: verify first, then let the app in.
    out = django(
        "import django; django.setup();"
        "from chatbot.tests.data_migration.fixture_data import read_back; read_back()",
        DST,
        DST_DB,
        DST_PASSWORD,
    ).stdout
    seen = json.loads(out.strip().splitlines()[-1])
    assert seen["logged_in"] is True
    assert seen["page_status"] == 200
    assert seen["page_has_emoji_text"] is True
    assert seen["survey_context"] == SURVEY_CONTEXT
    assert seen["study_name"] is None
    assert seen["initial_utterance"] == ""
    assert seen["texts"][0] == EMOJI_TEXT[:40]
    assert seen["long_len"] > 900_000
    # 02:30:15.123456+05:30 as the app stores it: UTC, microseconds intact.
    assert seen["created_time"] == "2025-03-08T21:00:15.123456+00:00"
    assert seen["total_utterances"] == N_UTTERANCES + 9


def test_verify_refuses_mysql_against_mariadb(source_db, tmp_path):
    if source_db["source"] == source_db["target"]:
        pytest.skip("needs a MySQL server on one side and MariaDB on the other")
    result = tool("--run-dir", str(tmp_path), "verify")
    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert "not supported" in result.stderr
    assert not (tmp_path / "verify-report.json").exists()
