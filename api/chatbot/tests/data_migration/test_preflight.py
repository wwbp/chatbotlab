"""``dbcopy.py preflight``: refuse, before anything is copied, whatever would
make a faithful copy impossible."""

import json
import subprocess
import time

import pytest

from .conftest import (
    CLIENT,
    DST,
    DST_DB,
    ROOT_PASSWORD,
    SRC,
    SRC_DB,
    SRC_PASSWORD,
    _root_args,
    admin,
    load_tool,
    read_log,
    reset_target,
    tool,
    tool_env,
)
from .fixture_data import N_UTTERANCES


def preflight(tmp_path, *flags, env=None):
    result = tool("--run-dir", str(tmp_path), "preflight", *flags, env=env)
    path = tmp_path / "preflight-report.json"
    return result, json.loads(path.read_text()) if path.exists() else None


def check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


def version_tuple(server):
    return tuple(
        int(x)
        for x in admin(server, "SELECT VERSION()")[0][0].split("-")[0].split(".")[:2]
    )


@pytest.fixture
def empty_target(same_flavour):
    reset_target()
    return same_flavour


@pytest.fixture
def same_or_newer_target(empty_target):
    if version_tuple(DST) < version_tuple(SRC):
        pytest.skip("target is an older version than the source")


def test_passes_and_reports_both_servers(same_or_newer_target, tmp_path):
    result, report = preflight(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PREFLIGHT OK" in result.stdout
    assert report["ok"] is True
    assert not [c for c in report["checks"] if c["status"] == "fail"]

    for side, server in (("source", SRC), ("target", DST)):
        info = report[side]
        assert info["version"] == admin(server, "SELECT VERSION()")[0][0]
        assert info["flavour"] in ("mariadb", "mysql")
        assert info["server_character_set"].startswith("utf8")
        assert info["server_collation"]
        assert info["database_character_set"] == "utf8mb4"
        assert info["database_collation"].startswith("utf8mb4_")
    source = report["source"]
    assert source["size_bytes"] > 1_000_000
    # Exact counts, not the engine's estimate.
    assert source["tables"]["chatbot_utterance"] == N_UTTERANCES + 9
    assert source["tables"]["mig_empty"] == 0
    assert "django_migrations" in source["tables"]
    assert report["target"]["tables"] == {}
    assert "chatbot_utterance" in result.stdout


def test_leaves_the_target_empty(same_or_newer_target, tmp_path):
    preflight(tmp_path)
    assert admin(DST, "SHOW TABLES", DST_DB) == []


def test_refuses_a_target_that_is_not_empty(same_or_newer_target, tmp_path):
    admin(DST, "CREATE TABLE leftover (id INT PRIMARY KEY)", DST_DB)
    result, report = preflight(tmp_path)
    assert result.returncode == 1
    failed = check(report, "target_empty")
    assert failed["status"] == "fail"
    assert "leftover" in failed["detail"]
    assert "PREFLIGHT FAILED" in result.stdout
    assert "leftover" in result.stdout

    result, report = preflight(tmp_path, "--allow-non-empty-target")
    assert result.returncode == 0, result.stdout
    assert check(report, "target_empty")["status"] == "warn"


def test_refuses_when_target_account_cannot_create_tables(
    same_or_newer_target, tmp_path
):
    admin(
        DST,
        "CREATE USER IF NOT EXISTS 'mig_weak'@'%' IDENTIFIED BY 'weak-test-pw';"
        f"GRANT SELECT ON {DST_DB}.* TO 'mig_weak'@'%'",
    )
    env = tool_env(MIG_TARGET_USER="mig_weak", MIG_TARGET_PASSWORD="weak-test-pw")
    result, report = preflight(tmp_path, env=env)
    assert result.returncode == 1
    failed = check(report, "target_privileges")
    assert failed["status"] == "fail"
    assert "denied" in failed["detail"].lower()


def test_refuses_when_source_account_cannot_read_every_table(
    same_or_newer_target, tmp_path
):
    admin(
        SRC,
        "CREATE USER IF NOT EXISTS 'mig_partial'@'%' IDENTIFIED BY 'partial-test-pw';"
        f"GRANT SELECT ON {SRC_DB}.chatbot_bot TO 'mig_partial'@'%'",
    )
    env = tool_env(MIG_SOURCE_USER="mig_partial", MIG_SOURCE_PASSWORD="partial-test-pw")
    result, report = preflight(tmp_path, env=env)
    # This account sees a one-table database. Nothing would error: a copy
    # made through it would silently be one table. So preflight demands
    # SELECT on the whole database, not just "the queries worked".
    assert result.returncode == 1
    failed = check(report, "source_privileges")
    assert failed["status"] == "fail"
    assert "SELECT" in failed["detail"]


def test_refuses_source_and_target_being_the_same_database(same_flavour, tmp_path):
    env = tool_env(
        MIG_TARGET_HOST=SRC.host,
        MIG_TARGET_PORT=SRC.port,
        MIG_TARGET_PASSWORD=SRC_PASSWORD,
        MIG_TARGET_DATABASE=SRC_DB,
    )
    result, report = preflight(tmp_path, "--allow-non-empty-target", env=env)
    assert result.returncode == 2
    assert "same database" in result.stderr
    assert report is None


def test_refuses_mysql_to_mariadb(source_db, tmp_path):
    if source_db["source"] == source_db["target"]:
        pytest.skip("needs a MySQL server on one side and MariaDB on the other")
    result, _ = preflight(tmp_path)
    assert result.returncode == 2
    assert "not supported" in result.stderr


def test_refuses_an_older_target_unless_allowed(empty_target, tmp_path):
    if version_tuple(DST) >= version_tuple(SRC):
        pytest.skip("needs a target older than the source")
    result, report = preflight(tmp_path)
    assert result.returncode == 1
    assert check(report, "versions")["status"] == "fail"
    assert "older" in check(report, "versions")["detail"]

    result, report = preflight(tmp_path, "--allow-version-downgrade")
    assert result.returncode == 0, result.stdout
    assert check(report, "versions")["status"] == "warn"


def test_collations_missing_on_the_target_are_named():
    # Two servers of one version always share their collations, so this path
    # cannot be reached with real containers of the same image; the check
    # itself is a pure function.
    dbcopy = load_tool()
    used = {"utf8mb4_uca1400_ai_ci", "utf8mb4_bin", None}
    available = {"utf8mb4_bin", "utf8mb4_unicode_ci"}
    assert dbcopy.missing_collations(used, available) == ["utf8mb4_uca1400_ai_ci"]
    assert dbcopy.missing_collations({"utf8mb4_bin", None}, available) == []
    # MariaDB 10.10+ lists UCA-14 collations without the character set.
    assert dbcopy.missing_collations(used, available | {"uca1400_ai_ci"}) == []


# ── --require-quiesced ───────────────────────────────────────────────────────


def _background_sql(sql, db=None):
    proc = subprocess.Popen(
        [CLIENT, *_root_args(SRC), *([db] if db else [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"MYSQL_PWD": ROOT_PASSWORD, "PATH": "/usr/bin:/bin"},
    )
    proc.stdin.write(sql.encode())
    proc.stdin.close()
    time.sleep(0.5)  # let it connect
    return proc


def test_quiesced_source_passes(same_or_newer_target, tmp_path):
    result, report = preflight(tmp_path, "--require-quiesced", "--quiesce-seconds", "1")
    assert result.returncode == 0, result.stdout
    assert check(report, "source_quiesced")["status"] == "ok"


def test_source_still_being_written_is_refused(same_or_newer_target, tmp_path):
    # No default database, so the writer does not show up as a session on
    # the source database: only the change itself can give it away.
    writes = (
        f"INSERT INTO {SRC_DB}.mig_empty VALUES (1, 'x'); DO SLEEP(0.3);"
        f"DELETE FROM {SRC_DB}.mig_empty; DO SLEEP(0.3);"
    ) * 15
    writer = _background_sql(writes)
    try:
        result, report = preflight(
            tmp_path, "--require-quiesced", "--quiesce-seconds", "2"
        )
    finally:
        writer.wait()
        admin(SRC, f"DELETE FROM {SRC_DB}.mig_empty")
    assert result.returncode == 1
    failed = check(report, "source_quiesced")
    assert failed["status"] == "fail"
    assert "mig_empty" in failed["detail"]


def test_source_with_an_open_app_session_is_refused(same_or_newer_target, tmp_path):
    idle = _background_sql("DO SLEEP(6);", SRC_DB)
    try:
        result, report = preflight(
            tmp_path, "--require-quiesced", "--quiesce-seconds", "1"
        )
    finally:
        idle.wait()
    assert result.returncode == 1
    failed = check(report, "source_quiesced")
    assert failed["status"] == "fail"
    assert "session" in failed["detail"]


def test_busy_source_is_only_checked_when_asked(same_or_newer_target, tmp_path):
    idle = _background_sql("DO SLEEP(3);", SRC_DB)
    try:
        result, report = preflight(tmp_path)
    finally:
        idle.wait()
    assert result.returncode == 0, result.stdout
    assert check(report, "source_quiesced")["status"] == "skipped"


# ── --dry-run ────────────────────────────────────────────────────────────────


def test_dry_run_connects_but_writes_nothing(same_or_newer_target, tmp_path):
    before = admin(DST, "SELECT COUNT(*) FROM information_schema.TABLES")
    result, report = preflight(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout.lower()
    assert report is None
    assert admin(DST, "SELECT COUNT(*) FROM information_schema.TABLES") == before
    entries = read_log(tmp_path)
    assert entries[-1]["step"] == "preflight"
    assert entries[-1]["dry_run"] is True


# ── P1: database text never becomes SQL ─────────────────────────────────────

AWKWARD_NAMES = ["it's", "back\\slash"]
# Sorts first; ends the string literal it is pasted into, re-enables writes
# and inserts.
INJECTING_NAME = "0';SET TRANSACTION READ WRITE;INSERT mig_empty VALUES(7,'');#"


def _make_table(name, rows):
    ident = "`" + name.replace("`", "``") + "`"
    admin(
        SRC,
        f"CREATE TABLE {ident} (id INT PRIMARY KEY);"
        + "".join(f"INSERT INTO {ident} VALUES ({i});" for i in range(rows)),
        SRC_DB,
    )
    return f"DROP TABLE IF EXISTS {ident}"


def test_awkward_table_names_are_counted_exactly(same_or_newer_target, tmp_path):
    drops = [_make_table(n, i + 2) for i, n in enumerate(AWKWARD_NAMES)]
    try:
        result, report = preflight(tmp_path)
    finally:
        admin(SRC, ";".join(drops), SRC_DB)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["source"]["tables"]["it's"] == 2
    assert report["source"]["tables"]["back\\slash"] == 3


def test_a_table_name_cannot_write_to_the_source(same_or_newer_target, tmp_path):
    drop = _make_table(INJECTING_NAME, 1)
    try:
        preflight(tmp_path)
        written = admin(SRC, "SELECT COUNT(*) FROM mig_empty", SRC_DB)[0][0]
    finally:
        admin(SRC, drop + "; DELETE FROM mig_empty", SRC_DB)
    assert written == "0"


def test_quiesced_check_needs_process_privilege(same_or_newer_target, tmp_path):
    # P2: without PROCESS the processlist shows only the account's own session.
    admin(
        SRC,
        "DROP USER IF EXISTS 'mig_noproc'@'%';"
        f"CREATE USER 'mig_noproc'@'%' IDENTIFIED BY '{SRC_PASSWORD}';"
        f"GRANT ALL PRIVILEGES ON {SRC_DB}.* TO 'mig_noproc'@'%'",
    )
    idle = _background_sql("DO SLEEP(6);", SRC_DB)
    try:
        result, report = preflight(
            tmp_path,
            "--require-quiesced",
            "--quiesce-seconds",
            "1",
            env=tool_env(MIG_SOURCE_USER="mig_noproc"),
        )
    finally:
        idle.wait()
        admin(SRC, "DROP USER IF EXISTS 'mig_noproc'@'%'")
    assert result.returncode == 1, result.stdout + result.stderr
    failed = check(report, "source_quiesced")
    assert failed["status"] == "fail"
    assert "grant PROCESS" in failed["detail"]


def test_utf8_and_utf8mb3_collation_names_are_the_same():
    # P7: MariaDB 10.6+ and MySQL 8.0.30+ report utf8mb3_*, older servers utf8_*.
    dbcopy = load_tool()
    assert dbcopy.missing_collations({"utf8_general_ci"}, {"utf8mb3_general_ci"}) == []
    assert dbcopy.missing_collations({"utf8mb3_bin"}, {"utf8_bin"}) == []
    assert dbcopy.missing_collations({"utf8mb4_bin"}, {"utf8_bin"}) == ["utf8mb4_bin"]
    assert dbcopy.norm_collation("utf8_unicode_ci") == "utf8mb3_unicode_ci"
    assert dbcopy.norm_collation("utf8mb4_unicode_ci") == "utf8mb4_unicode_ci"
