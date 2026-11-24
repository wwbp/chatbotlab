"""``dbcopy.py verify`` must never say IDENTICAL when it did not really look.

Each test sets up a way the comparison could be blinded, partial or pointed
at the wrong thing, and checks verify refuses (2) or reports a difference (1)
instead of passing. Objects a test creates are removed again.
"""

import signal
import subprocess
import sys
import time

import pytest

from .conftest import (
    DST,
    DST_DB,
    DST_PASSWORD,
    SCRIPT,
    SRC,
    SRC_DB,
    SRC_PASSWORD,
    admin,
    plain_copy,
    shim,
    tool,
    tool_env,
)


def s(sql):
    return admin(SRC, sql, SRC_DB)


def t(sql):
    return admin(DST, sql, DST_DB)


@pytest.fixture(scope="module")
def copied(same_flavour):
    plain_copy()
    return same_flavour


def verify(tmp_path, env=None):
    return tool("--run-dir", str(tmp_path), "verify", env=env)


@pytest.fixture
def keystroke_deleted_beyond_row_1000(copied):
    t(
        "CREATE DATABASE mig_scratch;"
        "CREATE TABLE mig_scratch.k AS SELECT * FROM chatbot_keystroke WHERE id = 1234;"
        "DELETE FROM chatbot_keystroke WHERE id = 1234"
    )
    yield
    t(
        "INSERT INTO chatbot_keystroke SELECT * FROM mig_scratch.k; DROP DATABASE mig_scratch"
    )


# ── V1: client option files must not cap what verify reads ──────────────────


@pytest.mark.parametrize("where", ["home_my_cnf", "supplied_defaults_file"])
def test_option_files_cannot_truncate_reads(
    where, keystroke_deleted_beyond_row_1000, tmp_path
):
    if where == "home_my_cnf":
        # safe-updates caps every SELECT at 1000 rows (select_limit).
        (tmp_path / ".my.cnf").write_text("[mysql]\nsafe-updates\n")
        env = tool_env(HOME=str(tmp_path))
    else:
        cnf = tmp_path / "client.cnf"
        cnf.write_text("[client]\ninit-command=SET SESSION sql_select_limit=1000\n")
        env = tool_env(
            MIG_SOURCE_DEFAULTS_FILE=str(cnf), MIG_TARGET_DEFAULTS_FILE=str(cnf)
        )
    result = verify(tmp_path / "run", env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "chatbot_keystroke" in result.stdout


def test_rows_streamed_must_match_count(copied, tmp_path):
    # A client that silently drops the 5th row hash of every table, both sides.
    drop = r"sed '5{/\t[0-9a-f]\{64\}$/d;}'"
    env = shim(tmp_path, "mariadb", f'"$REAL" "$@" | {drop}')
    result = verify(tmp_path / "run", env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "COUNT(*)" in result.stderr


# ── V3: only base tables and views; views by definition ──────────────────────

UNCOMPARABLE = {
    # A sequence's state is not rows verify can hash.
    "sequence": (
        "CREATE SEQUENCE mig_odd",
        "SELECT NEXTVAL(mig_odd)",
        "DROP SEQUENCE IF EXISTS mig_odd",
    ),
    # A system-versioned table was listed as a view and compared by name only.
    "system_versioned": (
        "CREATE TABLE mig_odd (id INT) WITH SYSTEM VERSIONING",
        "INSERT INTO mig_odd VALUES (1)",
        "DROP TABLE IF EXISTS mig_odd",
    ),
}


@pytest.mark.parametrize("kind", UNCOMPARABLE)
def test_refuses_table_types_it_cannot_compare(kind, copied, tmp_path):
    if copied != "mariadb":
        pytest.skip("MariaDB-only table types")
    create, change_source, drop = UNCOMPARABLE[kind]
    try:
        s(create + ";" + change_source)
        t(create)
        result = verify(tmp_path)
    finally:
        s(drop)
        t(drop)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "mig_odd" in result.stderr


def test_view_definitions_are_compared(copied, tmp_path):
    try:
        s("CREATE VIEW mig_v AS SELECT id FROM mig_empty")
        t("CREATE VIEW mig_v AS SELECT id FROM mig_empty WHERE id > 0")
        result = verify(tmp_path)
    finally:
        s("DROP VIEW IF EXISTS mig_v")
        t("DROP VIEW IF EXISTS mig_v")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "view mig_v" in result.stdout


def test_identical_views_pass(copied, tmp_path):
    # Each side's definition names its own database; that must not differ.
    try:
        s("CREATE VIEW mig_v AS SELECT id FROM mig_empty")
        t("CREATE VIEW mig_v AS SELECT id FROM mig_empty")
        result = verify(tmp_path)
    finally:
        s("DROP VIEW IF EXISTS mig_v")
        t("DROP VIEW IF EXISTS mig_v")
    assert result.returncode == 0, result.stdout + result.stderr


# ── V4: definitions the account cannot see must not compare equal ────────────


@pytest.fixture
def select_only_account(copied):
    for server, db, password in (
        (SRC, SRC_DB, SRC_PASSWORD),
        (DST, DST_DB, DST_PASSWORD),
    ):
        admin(
            server,
            "DROP USER IF EXISTS 'mig_ro'@'%';"
            f"CREATE USER 'mig_ro'@'%' IDENTIFIED BY '{password}';"
            f"GRANT SELECT ON {db}.* TO 'mig_ro'@'%';",
        )
    yield tool_env(MIG_SOURCE_USER="mig_ro", MIG_TARGET_USER="mig_ro")
    for server in (SRC, DST):
        admin(server, "DROP USER IF EXISTS 'mig_ro'@'%'")


def test_select_only_account_cannot_hide_a_trigger(select_only_account, tmp_path):
    try:
        s(
            "CREATE TRIGGER mig_tr BEFORE INSERT ON mig_empty "
            "FOR EACH ROW SET NEW.note = NEW.note"
        )
        result = verify(tmp_path, select_only_account)
    finally:
        s("DROP TRIGGER IF EXISTS mig_tr")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "TRIGGER" in result.stderr


def test_unreadable_routine_bodies_refused(copied, tmp_path):
    # Defined by root: mig_user may run it but not read its body (NULL).
    routine = "CREATE FUNCTION mig_f() RETURNS INT DETERMINISTIC NO SQL RETURN {}"
    try:
        s(routine.format(1))
        t(routine.format(2))
        result = verify(tmp_path)
    finally:
        s("DROP FUNCTION IF EXISTS mig_f")
        t("DROP FUNCTION IF EXISTS mig_f")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "mig_f" in result.stderr


# ── V5: two different databases, and something to compare ───────────────────


def test_target_pointing_at_the_source_is_refused(copied, tmp_path):
    env = tool_env(
        **{
            f"MIG_TARGET_{k}": tool_env()[f"MIG_SOURCE_{k}"]
            for k in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE")
        }
    )
    result = verify(tmp_path, env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "same database" in result.stderr


def test_empty_source_is_refused(copied, tmp_path):
    for server in (SRC, DST):
        admin(
            server,
            "CREATE DATABASE mig_void; GRANT ALL PRIVILEGES ON mig_void.* TO 'mig_user'@'%'",
        )
    try:
        env = tool_env(MIG_SOURCE_DATABASE="mig_void", MIG_TARGET_DATABASE="mig_void")
        result = verify(tmp_path, env)
    finally:
        for server in (SRC, DST):
            admin(server, "DROP DATABASE IF EXISTS mig_void")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no tables" in result.stderr


# ── V6: the hash function itself works ───────────────────────────────────────

ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_broken_sha2_is_refused(copied, tmp_path):
    env = shim(tmp_path, "mariadb", f'"$REAL" "$@" | sed s/{ABC_SHA256}/{"0" * 64}/')
    result = verify(tmp_path / "run", env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "SHA2" in result.stderr


# ── V7: no stale report survives a later run ─────────────────────────────────


def test_stale_ok_report_removed_by_a_refused_run(copied, tmp_path):
    stale = tmp_path / "verify-report.json"
    stale.write_text('{"ok": true}\n')
    result = verify(tmp_path, tool_env(MIG_TARGET_DATABASE=""))
    assert result.returncode == 2, result.stdout + result.stderr
    assert not stale.exists()


# ── V8: a crash or an interrupt is exit 2, never 1 and never a traceback ─────


def test_unexpected_error_exits_2_without_traceback(copied, tmp_path):
    # A client that prints nothing and succeeds: the tool cannot parse that.
    env = shim(tmp_path, "mariadb", "cat >/dev/null; exit 0")
    result = verify(tmp_path / "run", env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "CRASHED" in result.stderr
    assert "Traceback" not in result.stderr


def test_interrupt_exits_2(copied, tmp_path):
    env = shim(tmp_path, "mariadb", "cat >/dev/null; exec sleep 5")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--run-dir", str(tmp_path / "run"), "verify"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1)
    proc.send_signal(signal.SIGINT)
    _, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 2, stderr
    assert "INTERRUPTED" in stderr
    assert "Traceback" not in stderr
