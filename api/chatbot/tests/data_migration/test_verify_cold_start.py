"""``dbcopy.py verify`` on a target whose tables the server has not opened yet.

A target restored from a snapshot is a freshly started server. Until MySQL 8
opens a table, information_schema.TABLES.AUTO_INCREMENT shows the counter it
persisted (the last value used, i.e. one below the next) or NULL for a table
that never used one, even with information_schema_stats_expiry = 0. The next
INSERT gets the real value. A faithful copy must still verify IDENTICAL, and a
target written to after the copy must still be DIFFERENT. (MariaDB opens the
table to answer, so these pass there either way.)
"""

import os
import shlex
import subprocess
import time

import pytest

from .conftest import DST, DST_DB, SRC, SRC_DB, admin, plain_copy, read_report, tool

RESTART = os.environ.get("MIGTEST_TARGET_RESTART", "")


def t(sql):
    return admin(DST, sql, DST_DB)


def verify(tmp_path):
    result = tool("--run-dir", str(tmp_path), "verify")
    return result, read_report(tmp_path)


def restart_target():
    """Restart the target server process (its data stays) and wait for it."""
    started = time.monotonic()
    subprocess.run(shlex.split(RESTART), check=True)
    deadline = started + 180
    while True:
        try:
            uptime = int(admin(DST, "SHOW GLOBAL STATUS LIKE 'Uptime'")[0][1])
            # Only a new server process has been up for less than this.
            if uptime <= time.monotonic() - started + 1:
                return
        except RuntimeError:
            pass
        assert time.monotonic() < deadline, "target did not come back"
        time.sleep(1)


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


needs_restart = pytest.mark.skipif(
    not RESTART,
    reason="needs MIGTEST_TARGET_RESTART, a command that restarts the target server",
)


@needs_restart
def test_exact_copy_on_cold_started_target_is_identical(same_flavour, tmp_path):
    plain_copy()
    restart_target()

    result, report = verify(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert report["differences"] == []
    keystrokes = report["tables"]["chatbot_keystroke"]
    assert keystrokes["auto_increment_target"] == keystrokes["auto_increment_source"]
    assert report["tables"]["django_admin_log"]["auto_increment_target"] == 1


@needs_restart
def test_insert_after_copy_on_cold_started_target_is_different(same_flavour, tmp_path):
    plain_copy()
    # The real counter is now one ahead of the source's.
    t(
        "INSERT INTO chatbot_keystroke (conversation_id, total_time_on_page, "
        "total_time_away_from_page, keystroke_count, timestamp) "
        "VALUES ('after-copy', 1, 0, 1, '2026-01-01')"
    )
    restart_target()

    result, report = verify(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    found = {(d["table"], d["kind"]) for d in report["differences"]}
    assert ("chatbot_keystroke", "auto_increment") in found, found
    keystrokes = report["tables"]["chatbot_keystroke"]
    assert keystrokes["rows_target"] == keystrokes["rows_source"] + 1
    assert keystrokes["auto_increment_target"] > keystrokes["auto_increment_source"]
