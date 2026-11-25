"""``dbcopy.py verify`` on a target whose tables the server has not opened yet.

A target restored from a snapshot is a freshly started server. Until MySQL 8
opens a table, information_schema.TABLES.AUTO_INCREMENT shows the counter it
persisted (the last value used, i.e. one below the next) or NULL for a table
that never used one, even with information_schema_stats_expiry = 0. The next
INSERT gets the real value. A faithful copy must still verify IDENTICAL, and a
target written to after the copy must still be DIFFERENT, whichever side was
restarted. (MariaDB opens the table to answer, so these pass there either way.)

The restart tests need commands that restart each server with its data kept:
MIGTEST_SOURCE_RESTART and MIGTEST_TARGET_RESTART (CI: ``docker restart`` of
the service). They skip without them, except in CI, where that fails.
"""

import os
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest

from .conftest import (
    DST,
    DST_DB,
    SRC,
    SRC_DB,
    admin,
    load_tool,
    plain_copy,
    read_report,
    tool,
    tool_env,
)

RESTART = {
    SRC: os.environ.get("MIGTEST_SOURCE_RESTART", ""),
    DST: os.environ.get("MIGTEST_TARGET_RESTART", ""),
}
KEYSTROKE = (
    "INSERT INTO chatbot_keystroke (conversation_id, total_time_on_page, "
    "total_time_away_from_page, keystroke_count, timestamp) "
    "VALUES ('after-copy', 1, 0, 1, '2026-01-01')"
)


def t(sql):
    return admin(DST, sql, DST_DB)


def verify(tmp_path):
    result = tool("--run-dir", str(tmp_path), "verify")
    path = tmp_path / "verify-report.json"
    return result, read_report(tmp_path) if path.exists() else None


def restart(server):
    """Restart a server process (its data stays) and wait for the new one."""
    started = time.monotonic()
    subprocess.run(shlex.split(RESTART[server]), check=True)
    deadline = started + 180
    uptime = None
    while True:
        try:
            uptime = int(admin(server, "SHOW GLOBAL STATUS LIKE 'Uptime'")[0][1])
            # Only a new server process has been up for less than this.
            if uptime <= time.monotonic() - started + 1:
                return
        except RuntimeError:
            uptime = None
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{server.host}:{server.port} did not come back"
                if uptime is None
                else f"{server.host}:{server.port} did not restart "
                f"(Uptime never reset: {uptime}s)"
            )
        time.sleep(1)


def test_restart_commands_are_set_in_ci(same_flavour):
    # Otherwise the restart tests below would skip and CI would stay green.
    if not os.environ.get("CI"):
        pytest.skip("only enforced in CI")
    assert RESTART[SRC], "MIGTEST_SOURCE_RESTART is not set"
    assert RESTART[DST], "MIGTEST_TARGET_RESTART is not set"


def test_empty_table_not_yet_opened_on_target_is_identical(same_flavour, tmp_path):
    # Recreated without being opened: MySQL shows NULL until it is, though
    # the next id is 1 on both sides (production: "source=1 target=None").
    plain_copy()
    admin(SRC, "SELECT 1 FROM django_admin_log LIMIT 1", SRC_DB)
    ddl = admin(SRC, "SHOW CREATE TABLE django_admin_log", SRC_DB)[0][1]
    t(f"DROP TABLE django_admin_log; {ddl.replace(chr(92) + 'n', ' ')}")

    result, report = verify(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    entry = report["tables"]["django_admin_log"]
    assert entry["auto_increment_source"] == entry["auto_increment_target"] == 1


def test_a_table_that_cannot_be_opened_refuses_rather_than_reading_it_cold(
    monkeypatch,
):
    # Unit test: no real account gets this far. verify first demands SELECT
    # on the whole database, and a discarded tablespace still opens for
    # LIMIT 0. The open must not be skippable: its error is the read's error.
    dbcopy = load_tool()

    def fake_query(conn, sql, writable=False):
        if "LIMIT 0" in sql:
            raise dbcopy.Refuse("target database: SELECT command denied for 't'")
        return [["t"]]

    monkeypatch.setattr(dbcopy, "query", fake_query)
    with pytest.raises(dbcopy.Refuse, match="denied"):
        dbcopy._tables(SimpleNamespace(role="source"), ["TABLE_NAME"])  # noqa: SLF001


def test_no_counter_and_counter_1_are_different():
    dbcopy = load_tool()

    def schema(auto_increment):
        table = {
            "options": {},
            "auto_increment": auto_increment,
            **{part: {} for part in ("column", "index", "foreign key", "check")},
        }
        return {
            "database": {},
            **{k: {} for k in ("views", "routines", "triggers", "events")},
            "tables": {"t": table},
        }

    diffs = dbcopy.diff_schema(schema(1), schema(None))
    assert [(d["table"], d["kind"]) for d in diffs] == [("t", "auto_increment")]


def needs_restart(*servers):
    names = {SRC: "MIGTEST_SOURCE_RESTART", DST: "MIGTEST_TARGET_RESTART"}
    missing = [names[s] for s in servers if not RESTART[s]]
    return pytest.mark.skipif(
        bool(missing), reason=f"needs {', '.join(missing)}: a server restart command"
    )


@needs_restart(DST)
def test_exact_copy_on_cold_started_target_is_identical(same_flavour, tmp_path):
    plain_copy()
    restart(DST)

    result, report = verify(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert report["differences"] == []
    keystrokes = report["tables"]["chatbot_keystroke"]
    assert keystrokes["auto_increment_target"] == keystrokes["auto_increment_source"]
    assert report["tables"]["django_admin_log"]["auto_increment_target"] == 1


@needs_restart(DST)
def test_insert_after_copy_on_cold_started_target_is_different(same_flavour, tmp_path):
    plain_copy()
    # The real counter is now one ahead of the source's.
    t(KEYSTROKE)
    restart(DST)

    result, report = verify(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    found = {(d["table"], d["kind"]) for d in report["differences"]}
    assert ("chatbot_keystroke", "auto_increment") in found, found
    keystrokes = report["tables"]["chatbot_keystroke"]
    assert keystrokes["rows_target"] == keystrokes["rows_source"] + 1
    assert keystrokes["auto_increment_target"] > keystrokes["auto_increment_source"]


def only_counter_differs(result, report):
    assert result.returncode == 1, result.stdout + result.stderr
    found = [(d["table"], d["kind"]) for d in report["differences"]]
    assert found == [("chatbot_keystroke", "auto_increment")], found
    keystrokes = report["tables"]["chatbot_keystroke"]
    assert keystrokes["checksum_target"] == keystrokes["checksum_source"]


@needs_restart(DST)
def test_counter_only_ahead_on_cold_started_target_is_different(same_flavour, tmp_path):
    # Rows identical, counter one ahead: before the fix this was IDENTICAL.
    plain_copy()
    t(
        f"{KEYSTROKE}; DELETE FROM chatbot_keystroke WHERE conversation_id = 'after-copy'"
    )
    restart(DST)

    only_counter_differs(*verify(tmp_path))


@needs_restart(SRC)
def test_counter_only_ahead_on_cold_started_source_is_different(same_flavour, tmp_path):
    plain_copy()
    admin(
        SRC,
        f"{KEYSTROKE}; DELETE FROM chatbot_keystroke WHERE conversation_id = 'after-copy'",
        SRC_DB,
    )
    restart(SRC)

    only_counter_differs(*verify(tmp_path))


@needs_restart(SRC)
def test_cold_started_source_is_not_mistaken_for_one_being_written(
    same_flavour, monkeypatch
):
    # Called directly: preflight itself counts every row first, which opens
    # the tables, so through the CLI the first sample is never cold.
    restart(SRC)
    for name, value in tool_env().items():
        monkeypatch.setenv(name, value)
    dbcopy = load_tool()

    assert dbcopy.quiesced_problem(dbcopy.connect("source"), 1) is None
