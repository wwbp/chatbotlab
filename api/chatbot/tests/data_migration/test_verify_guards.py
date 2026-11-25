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
    LIMITED_GRANTS,
    SCRIPT,
    SRC,
    SRC_DB,
    SRC_PASSWORD,
    admin,
    create_limited_account,
    drop_limited_account,
    flavour,
    load_tool,
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
    # Defined by root: mig_lim may run it but not read its body (NULL).
    routine = "CREATE FUNCTION mig_f() RETURNS INT DETERMINISTIC NO SQL RETURN {}"
    env = create_limited_account()
    try:
        s(routine.format(1))
        t(routine.format(2))
        result = verify(tmp_path, env)
    finally:
        s("DROP FUNCTION IF EXISTS mig_f")
        t("DROP FUNCTION IF EXISTS mig_f")
        drop_limited_account()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "mig_f" in result.stderr


# Privileges held through a role, as the RDS master user holds its own
# (rds_superuser_role): they count only while the role is active.
ROLE, ROLE_USER = "mig_role", "mig_rl"


@pytest.fixture
def role_account(copied):
    """Make mig_rl on both servers, with no direct grants and only a role
    holding ``privileges`` on the database; returns its tool env."""

    def make(privileges, active=True):
        for server, db, password in (
            (SRC, SRC_DB, SRC_PASSWORD),
            (DST, DST_DB, DST_PASSWORD),
        ):
            mysql = flavour(server) == "mysql"
            # What reads the body of a routine one did not define (see
            # test_unreadable_routine_bodies_refused), held globally.
            routine_bodies = (
                f"GRANT SHOW_ROUTINE ON *.* TO {ROLE};"
                if mysql
                else f"GRANT SELECT ON mysql.proc TO {ROLE};"
            )
            default = "TO" if mysql else "FOR"
            admin(
                server,
                f"DROP USER IF EXISTS '{ROLE_USER}'@'%'; DROP ROLE IF EXISTS {ROLE};"
                f"CREATE ROLE {ROLE};"
                f"GRANT {privileges} ON {db}.* TO {ROLE};"
                f"{routine_bodies}"
                f"CREATE USER '{ROLE_USER}'@'%' IDENTIFIED BY '{password}';"
                f"GRANT {ROLE} TO '{ROLE_USER}'@'%';"
                + (
                    f"SET DEFAULT ROLE {ROLE} {default} '{ROLE_USER}'@'%';"
                    if active
                    else ""
                ),
            )
        return tool_env(MIG_SOURCE_USER=ROLE_USER, MIG_TARGET_USER=ROLE_USER)

    yield make
    for server in (SRC, DST):
        admin(
            server, f"DROP USER IF EXISTS '{ROLE_USER}'@'%'; DROP ROLE IF EXISTS {ROLE}"
        )


def test_privileges_held_through_an_active_role_count(role_account, tmp_path):
    result = verify(tmp_path, role_account(LIMITED_GRANTS))
    assert result.returncode == 0, result.stdout + result.stderr


def test_privileges_of_an_inactive_role_do_not_count(role_account, tmp_path):
    env = role_account(LIMITED_GRANTS, active=False)
    # A direct SELECT, or the server alone would turn the session away.
    for server, db in ((SRC, SRC_DB), (DST, DST_DB)):
        admin(server, f"GRANT SELECT ON {db}.* TO '{ROLE_USER}'@'%'")
    result = verify(tmp_path, env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "lacks SHOW VIEW, TRIGGER, EVENT, EXECUTE on" in result.stderr


def test_role_missing_a_privilege_is_refused(role_account, tmp_path):
    result = verify(tmp_path, role_account("SELECT, SHOW VIEW, EVENT, EXECUTE"))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "lacks TRIGGER on" in result.stderr


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


# ── V9: routines and events are compared by signature and schedule ───────────

AS_MIG_USER = "CREATE DEFINER = 'mig_user'@'%' "  # so mig_user can read the body
ROUTINE_PAIRS = {
    "parameter_type": (
        "FUNCTION mig_f(x INT) RETURNS INT DETERMINISTIC NO SQL RETURN 1",
        "FUNCTION mig_f(x BIGINT) RETURNS INT DETERMINISTIC NO SQL RETURN 1",
        "DROP FUNCTION IF EXISTS mig_f",
    ),
    "return_type": (
        "FUNCTION mig_f() RETURNS INT DETERMINISTIC NO SQL RETURN 1",
        "FUNCTION mig_f() RETURNS BIGINT DETERMINISTIC NO SQL RETURN 1",
        "DROP FUNCTION IF EXISTS mig_f",
    ),
    # Same name, both types: keyed by name only, one hides the other.
    "function_and_procedure": (
        "FUNCTION mig_f() RETURNS INT DETERMINISTIC NO SQL RETURN 1;"
        + AS_MIG_USER
        + "PROCEDURE mig_f() DETERMINISTIC NO SQL DO 1",
        "FUNCTION mig_f() RETURNS INT DETERMINISTIC NO SQL RETURN 1;"
        + AS_MIG_USER
        + "PROCEDURE mig_f() DETERMINISTIC NO SQL DO 2",
        "DROP FUNCTION IF EXISTS mig_f; DROP PROCEDURE IF EXISTS mig_f",
    ),
    "event_schedule": (
        "EVENT mig_e ON SCHEDULE EVERY 1 DAY DISABLE DO DO 1",
        "EVENT mig_e ON SCHEDULE EVERY 2 DAY DISABLE DO DO 1",
        "DROP EVENT IF EXISTS mig_e",
    ),
}


@pytest.mark.parametrize("case", ROUTINE_PAIRS)
def test_routine_and_event_signatures_compared(case, copied, tmp_path):
    on_source, on_target, drop = ROUTINE_PAIRS[case]
    try:
        s(AS_MIG_USER + on_source)
        t(AS_MIG_USER + on_target)
        result = verify(tmp_path)
    finally:
        s(drop)
        t(drop)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DIFFERENT" in result.stdout


# ── key values are data: never printed or recorded unless they are integers ──

TOKEN = "livesessiontokenabc123xyz"  # like django_session.session_key


@pytest.mark.parametrize("difference", ["row_differs", "row_missing_on_target"])
def test_non_integer_keys_never_reach_output_or_files(difference, copied, tmp_path):
    create = "CREATE TABLE mig_sess (session_key VARCHAR(40) PRIMARY KEY, data TEXT)"
    s(create + f"; INSERT INTO mig_sess VALUES ('{TOKEN}', 'a')")
    t(
        create
        + (
            f"; INSERT INTO mig_sess VALUES ('{TOKEN}', 'b')"
            if difference == "row_differs"
            else ""
        )
    )
    run_dir = tmp_path / "run"
    try:
        result = verify(run_dir)
    finally:
        s("DROP TABLE mig_sess")
        t("DROP TABLE mig_sess")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "mig_sess" in result.stdout
    leaked = TOKEN in result.stdout + result.stderr or any(
        TOKEN in p.read_text() for p in run_dir.iterdir()
    )
    assert not leaked, "a non-integer key value was printed or recorded"


def test_run_files_are_private(copied, tmp_path):
    run_dir = tmp_path / "run"
    assert verify(run_dir).returncode == 0
    modes = {p.name: oct(p.stat().st_mode & 0o777) for p in run_dir.iterdir()}
    assert modes
    assert set(modes.values()) == {"0o600"}, modes


def test_line_separator_in_a_definition_is_not_a_row_break(copied, tmp_path):
    # U+2028 is not escaped by the client's batch output; only "\n" ends a row.
    view = "CREATE VIEW mig_v AS SELECT id, 'a\u2028b' AS sep FROM mig_empty"
    try:
        s(view)
        t(view)
        result = verify(tmp_path)
    finally:
        s("DROP VIEW IF EXISTS mig_v")
        t("DROP VIEW IF EXISTS mig_v")
    assert result.returncode == 0, result.stdout + result.stderr


def test_grant_lines_are_read_fail_closed(monkeypatch):
    dbcopy = load_tool()
    conn = dbcopy.Conn("source", "h", "1", "u", "", "mig_src", "")

    def held(*lines, needed=("SELECT", "TRIGGER", "PROCESS")):
        monkeypatch.setattr(dbcopy, "query", lambda _c, _s: [[x] for x in lines])
        return [p for p in needed if p not in dbcopy.missing_privileges(conn, needed)]

    assert held("GRANT PROCESS ON *.* TO `u`@`%`") == ["PROCESS"]
    # ALL on a database does not include the global-only PROCESS.
    assert held("GRANT ALL PRIVILEGES ON `mig_src`.* TO `u`@`%`") == [
        "SELECT",
        "TRIGGER",
    ]
    assert held("GRANT ALL PRIVILEGES ON *.* TO `u`@`%`") == [
        "SELECT",
        "TRIGGER",
        "PROCESS",
    ]
    # Database names are LIKE patterns; an escaped _ is literal.
    assert held("GRANT SELECT ON `mig%`.* TO `r`") == ["SELECT"]
    assert held("GRANT SELECT ON `mig\\_src`.* TO `r`") == ["SELECT"]
    assert held("GRANT SELECT ON `mig\\_sr`.* TO `r`") == []
    assert held("GRANT SELECT ON `other`.* TO `r`") == []
    # Table, column and routine grants are not database-wide.
    assert held("GRANT SELECT ON `mig_src`.`t` TO `u`@`%`") == []
    assert (
        held("GRANT EXECUTE ON PROCEDURE `mig_src`.`p` TO `u`@`%`", needed=("EXECUTE",))
        == []
    )
    assert (
        held(
            "GRANT SELECT ON *.* TO `u`@`%`",
            "REVOKE SELECT ON `mig_src`.* FROM `u`@`%`",
        )
        == []
    )
