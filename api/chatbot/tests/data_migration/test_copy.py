"""``dbcopy.py dump``, ``restore`` and ``reset-target``: the copy itself.

The whole path an operator runs, against real servers: preflight, dump,
restore, verify. Then what happens when it goes wrong: a damaged dump file, a
target that is not empty, a restore killed halfway, and the clean-up.
"""

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from .conftest import (
    CLIENT,
    DST,
    DST_DB,
    DST_PASSWORD,
    DUMP,
    ROOT_PASSWORD,
    SCRIPT,
    SNAPSHOT,
    SRC,
    SRC_DB,
    SRC_PASSWORD,
    USER,
    _root_args,
    admin,
    db_collation,
    django,
    read_log,
    read_report,
    recorded_stdin,
    reset_target,
    shim,
    tool,
    tool_env,
    tool_recording_commands,
)
from .fixture_data import EMOJI_TEXT, N_UTTERANCES, SURVEY_CONTEXT

# Accepts an older target, so the same tests run on every same-flavour pair.
PREFLIGHT = ["preflight", "--allow-version-downgrade"]
STEPS = [PREFLIGHT, SNAPSHOT, ["dump"], ["restore"], ["verify"]]
WRITE_VERBS = (
    "INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|RENAME|GRANT|REVOKE"
    "|LOCK|FLUSH|CALL|LOAD|OPTIMIZE|ANALYZE|REPAIR"
)


CONFIRM = f"{DST.host}/{DST_DB}"


def snapshot(run_dir):
    """Record the snapshot gate, which dump requires, in ``run_dir``."""
    assert tool("--run-dir", str(run_dir), *SNAPSHOT).returncode == 0


def target_tables():
    return [row[0] for row in admin(DST, "SHOW TABLES", DST_DB)]


def other_collation():
    """A database default the source does not use."""
    return (
        "utf8mb4_bin"
        if db_collation(SRC, SRC_DB) != "utf8mb4_bin"
        else "utf8mb4_general_ci"
    )


def entry(run_dir, step, status="ok"):
    return [
        e for e in read_log(run_dir) if e["step"] == step and e["status"] == status
    ][-1]


@pytest.fixture(scope="module")
def copy(same_flavour, tmp_path_factory):
    """One full copy made with the tool. The target database starts with a
    different default collation, as a database created by Terraform will."""
    run_dir = tmp_path_factory.mktemp("run")
    reset_target(other_collation())
    results = {step[0]: tool("--run-dir", str(run_dir), *step) for step in STEPS}
    return run_dir, results


@pytest.fixture(scope="module")
def dumped(same_flavour, tmp_path_factory):
    """A run directory holding just a good dump."""
    run_dir = tmp_path_factory.mktemp("dumped")
    assert tool("--run-dir", str(run_dir), *SNAPSHOT).returncode == 0
    result = tool("--run-dir", str(run_dir), "dump")
    assert result.returncode == 0, result.stderr
    return run_dir


def fresh_run_with_dump(dumped, tmp_path):
    """A private copy of the dumped run directory for a test to damage."""
    for name in ("dump.sql", "dump.sql.sha256", "run.log"):
        (tmp_path / name).write_bytes((dumped / name).read_bytes())
    return tmp_path


# ── the happy path ───────────────────────────────────────────────────────────


def test_every_step_succeeds_and_the_copy_verifies(copy):
    run_dir, results = copy
    for step, result in results.items():
        assert result.returncode == 0, f"{step}: {result.stdout}{result.stderr}"
    report = read_report(run_dir)
    assert report["ok"] is True
    assert report["tables"]["chatbot_utterance"]["rows_target"] == N_UTTERANCES + 9
    assert [e["step"] for e in read_log(run_dir) if e["status"] == "ok"] == [
        "preflight",
        "snapshot-check",
        "dump",
        "restore",
        "verify",
    ]


def test_dump_file_checksum_is_recorded_and_true(copy):
    run_dir, _ = copy
    digest = hashlib.sha256((run_dir / "dump.sql").read_bytes()).hexdigest()
    assert (run_dir / "dump.sql.sha256").read_text() == f"{digest}  dump.sql\n"
    logged = entry(run_dir, "dump")
    assert logged["sha256"] == digest
    assert logged["bytes"] == (run_dir / "dump.sql").stat().st_size
    assert logged["source"]["host"] == SRC.host
    assert not list(run_dir.glob("*.partial"))
    # The file is private to the operator: it is the whole database.
    assert (run_dir / "dump.sql").stat().st_mode & 0o077 == 0


def test_every_file_in_the_run_directory_is_private(copy):
    # Review 1: reports, run.log and the checksum file as well as the dump.
    run_dir, _ = copy
    modes = {p.name: oct(p.stat().st_mode & 0o777) for p in run_dir.iterdir()}
    assert {"dump.sql", "dump.sql.sha256", "run.log", "verify-report.json"} <= set(
        modes
    )
    assert set(modes.values()) == {"0o600"}, modes


def test_dump_is_deterministic(copy, dumped):
    first = entry(copy[0], "dump")["sha256"]
    assert entry(dumped, "dump")["sha256"] == first


def test_dump_carries_trigger_routine_event_and_view(copy):
    def names(table, column, schema_column):
        sql = f"SELECT {column} FROM information_schema.{table} WHERE {schema_column} = DATABASE()"
        return [row[0] for row in admin(DST, sql, DST_DB)]

    assert names("TRIGGERS", "TRIGGER_NAME", "TRIGGER_SCHEMA") == ["mig_child_bi"]
    assert names("ROUTINES", "ROUTINE_NAME", "ROUTINE_SCHEMA") == ["mig_proc"]
    assert names("EVENTS", "EVENT_NAME", "EVENT_SCHEMA") == ["mig_event"]
    assert names("VIEWS", "TABLE_NAME", "TABLE_SCHEMA") == ["mig_view"]


def test_restore_gives_the_target_the_sources_default_collation(copy):
    assert other_collation() != db_collation(SRC, SRC_DB)
    assert db_collation(DST, DST_DB) == db_collation(SRC, SRC_DB)


def test_timings_are_recorded(copy):
    run_dir, _ = copy
    seconds = {
        step: entry(run_dir, step)["duration_seconds"]
        for step in ("dump", "restore", "verify")
    }
    megabytes = entry(run_dir, "dump")["bytes"] / 1e6
    sys.stdout.write(
        f"\nTIMINGS for a {megabytes:.1f} MB dump: {json.dumps(seconds)}\n"
    )
    # Sanity only: 52,000 rows must not take minutes.
    assert all(0 < value < 120 for value in seconds.values())


def test_no_writing_statement_reaches_the_source(same_flavour, tmp_path):
    """Read the server's own log of everything the tool's account ran."""
    reset_target()
    admin(
        SRC,
        "SET GLOBAL log_output = 'TABLE'; SET GLOBAL general_log = 1; TRUNCATE mysql.general_log",
    )
    try:
        for step in STEPS:
            assert tool("--run-dir", str(tmp_path), *step).returncode == 0
    finally:
        admin(SRC, "SET GLOBAL general_log = 0")
    statements = admin(
        SRC,
        "SELECT REPLACE(REPLACE(CONVERT(argument USING utf8mb4), '\\n', ' '), '\\t', ' ')"
        " FROM mysql.general_log WHERE command_type = 'Query' AND user_host LIKE 'mig_user%'",
    )
    statements = [row[0] for row in statements]
    assert len(statements) > 50  # the log really was capturing the tool
    assert any("START TRANSACTION" in s for s in statements)  # the dump is in there
    writes = [
        s
        for s in statements
        if re.match(rf"\s*(/\*.*?\*/\s*)*({WRITE_VERBS})\b", s, flags=re.IGNORECASE)
    ]
    assert writes == []


def test_no_credential_in_dump_or_restore_command_lines(same_flavour, tmp_path):
    reset_target()
    run_dir = tmp_path / "run"
    assert tool("--run-dir", str(run_dir), *SNAPSHOT).returncode == 0
    for step in ("dump", "restore"):
        result, commands = tool_recording_commands(
            tmp_path, "--run-dir", str(run_dir), step
        )
        assert result.returncode == 0, result.stderr
        assert any("--max-allowed-packet" in c for c in commands)  # the big one ran
        for secret in (SRC_PASSWORD, DST_PASSWORD):
            assert secret not in "\n".join(commands)
            assert secret not in result.stdout + result.stderr
            assert secret not in (run_dir / "run.log").read_text()


# ── refusing to start ────────────────────────────────────────────────────────


def test_dump_will_not_overwrite_an_existing_dump(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    before = (run_dir / "dump.sql").read_bytes()
    result = tool("--run-dir", str(run_dir), "dump")
    assert result.returncode == 2
    assert "already exists" in result.stderr
    assert (run_dir / "dump.sql").read_bytes() == before


def test_restore_refuses_a_target_that_is_not_empty(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    admin(
        DST,
        "CREATE TABLE leftover (id INT PRIMARY KEY); INSERT INTO leftover VALUES (7)",
        DST_DB,
    )
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 2
    assert "not empty" in result.stderr
    assert "leftover" in result.stderr
    assert target_tables() == ["leftover"]

    result = tool("--run-dir", str(run_dir), "restore", "--allow-non-empty-target")
    assert result.returncode == 0, result.stderr
    # Restore only ever adds: what was there is still there, untouched.
    assert admin(DST, "SELECT id FROM leftover", DST_DB) == [["7"]]
    assert "chatbot_utterance" in target_tables()


def test_restore_never_replaces_an_existing_table(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    admin(
        DST,
        "CREATE TABLE auth_user (id INT PRIMARY KEY); INSERT INTO auth_user VALUES (7)",
        DST_DB,
    )
    result = tool("--run-dir", str(run_dir), "restore", "--allow-non-empty-target")
    assert result.returncode == 1
    assert "already exists" in result.stdout + result.stderr
    assert admin(DST, "SELECT id FROM auth_user", DST_DB) == [["7"]]


@pytest.mark.parametrize("damage", ["flipped byte", "truncated", "appended"])
def test_restore_refuses_a_dump_that_does_not_match_its_checksum(
    dumped, tmp_path, damage
):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    path = run_dir / "dump.sql"
    data = bytearray(path.read_bytes())
    if damage == "flipped byte":
        data[len(data) // 2] ^= 1
    elif damage == "truncated":
        del data[len(data) // 2 :]
    else:
        data += b"DROP TABLE auth_user;\n"
    path.write_bytes(bytes(data))
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 2
    assert "checksum" in result.stderr
    assert target_tables() == []


def test_restore_refuses_a_file_this_run_did_not_dump(dumped, tmp_path):
    reset_target()
    (tmp_path / "dump.sql").write_bytes((dumped / "dump.sql").read_bytes())
    result = tool("--run-dir", str(tmp_path), "restore")
    assert result.returncode == 2
    assert "no dump is recorded" in result.stderr
    assert target_tables() == []


# ── a dump that fails (D2) ───────────────────────────────────────────────────

BROKEN_DUMPS = {
    "half_then_error": f'"{DUMP}" "$@" | head -c 200000; exit 2',
    "no_trailer_exit_0": f'"{DUMP}" "$@" | grep -v "Dump completed"; exit 0',
}


@pytest.mark.parametrize("kind", BROKEN_DUMPS)
def test_a_failed_dump_leaves_nothing_restore_could_use(kind, same_flavour, tmp_path):
    env = shim(tmp_path, "mariadb-dump", BROKEN_DUMPS[kind])
    run_dir = tmp_path / "run"
    snapshot(run_dir)
    result = tool("--run-dir", str(run_dir), "dump", env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "dump failed or is incomplete" in result.stderr
    assert sorted(p.name for p in run_dir.iterdir()) == ["run.log"]
    assert [e["status"] for e in read_log(run_dir) if e["step"] == "dump"] == [
        "refused"
    ]
    reset_target()
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 2
    assert "no dump is recorded" in result.stderr
    assert target_tables() == []


def test_dump_with_a_lot_on_stderr_does_not_deadlock(same_flavour, tmp_path):
    # D5: 200 KB of stderr before any output fills the pipe if nobody reads it.
    noisy = f'head -c 200000 /dev/zero | tr "\\0" x >&2; exec "{DUMP}" "$@"'
    env = shim(tmp_path, "mariadb-dump", noisy)
    run_dir = tmp_path / "run"
    snapshot(run_dir)
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "dump"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("dump deadlocked on a full stderr pipe")
    assert result.returncode == 0, result.stdout + result.stderr[-2000:]
    assert (run_dir / "dump.sql").exists()


def test_float_the_dump_would_round_is_refused(same_flavour, tmp_path):
    # D7: mariadb-dump writes FLOAT with 6 significant digits; 1.500001 would
    # come back as 1.5. Values that survive that (the fixture's) are copied.
    admin(
        SRC,
        "CREATE TABLE mig_flt (id INT PRIMARY KEY, x FLOAT);"
        "INSERT INTO mig_flt VALUES (1, 1.5), (2, 1.500001)",
        SRC_DB,
    )
    snapshot(tmp_path)
    try:
        result = tool("--run-dir", str(tmp_path), "dump")
    finally:
        admin(SRC, "DROP TABLE mig_flt", SRC_DB)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "mig_flt.x" in result.stderr
    assert not (tmp_path / "dump.sql").exists()


# ── D6: the smaller ways a dump or restore can go wrong ─────────────────────


def test_missing_dump_binary_is_a_clean_refusal(same_flavour, tmp_path):
    bin_dir = tmp_path / "only-client"
    bin_dir.mkdir()
    (bin_dir / "mariadb").symlink_to(CLIENT)
    env = tool_env(PATH=str(bin_dir))
    snapshot(tmp_path / "run")
    result = tool("--run-dir", str(tmp_path / "run"), "dump", env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REFUSED" in result.stderr
    assert "mariadb-dump" in result.stderr


def test_dump_failure_names_the_exit_code(same_flavour, tmp_path):
    env = shim(tmp_path, "mariadb-dump", BROKEN_DUMPS["half_then_error"])
    snapshot(tmp_path / "run")
    result = tool("--run-dir", str(tmp_path / "run"), "dump", env=env)
    assert result.returncode == 2
    assert "exit code 2" in result.stderr


def test_disk_full_during_dump_leaves_no_partial_file(same_flavour, tmp_path):
    # A file-size limit stands in for a full disk: writes fail with EFBIG.
    run_dir = tmp_path / "run"
    snapshot(run_dir)
    result = subprocess.run(
        ["sh", "-c", 'ulimit -f 2000; exec "$@"', "sh", sys.executable, str(SCRIPT),
         "--run-dir", str(run_dir), "dump"],
        env=tool_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )  # fmt: skip
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REFUSED" in result.stderr
    assert sorted(p.name for p in run_dir.iterdir()) == ["run.log"]


def test_a_torn_last_line_in_run_log_does_not_break_the_run(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    with (run_dir / "run.log").open("a") as log:
        log.write('{"step": "restore", "sta')  # the machine died mid-write
    reset_target()
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = (run_dir / "run.log").read_text().splitlines()
    assert lines[-3] == '{"step": "restore", "sta'
    assert json.loads(lines[-1])["status"] == "ok"


@pytest.mark.parametrize(
    ("create", "drop"),
    [
        ("CREATE SEQUENCE mig_seq", "DROP SEQUENCE IF EXISTS mig_seq"),
        (
            "CREATE EVENT mig_live ON SCHEDULE EVERY 1 DAY ENABLE DO DO 1",
            "DROP EVENT IF EXISTS mig_live",
        ),
    ],
    ids=["sequence", "enabled_event"],
)
def test_dump_refuses_objects_it_cannot_copy_safely(
    create, drop, same_flavour, tmp_path
):
    # A sequence is not a view; an enabled event would start writing to the
    # target the moment it is restored.
    if "SEQUENCE" in create and same_flavour != "mariadb":
        pytest.skip("MariaDB-only object")
    admin(SRC, create, SRC_DB)
    snapshot(tmp_path)
    try:
        result = tool("--run-dir", str(tmp_path), "dump")
    finally:
        admin(SRC, drop, SRC_DB)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "mig_" in result.stderr
    assert not (tmp_path / "dump.sql").exists()


def test_dump_reports_definers(copy):
    run_dir, results = copy
    assert "DEFINER" in results["dump"].stdout
    assert entry(run_dir, "dump")["definers"]


def test_restore_feeds_the_bytes_it_checked(same_flavour, tmp_path):
    run_dir = tmp_path / "run"
    reset_target()
    snapshot(run_dir)
    for step in ("dump", "restore"):
        result, _ = tool_recording_commands(tmp_path, "--run-dir", str(run_dir), step)
        assert result.returncode == 0, result.stderr
    (sent,) = [
        s for argv, s in recorded_stdin(tmp_path) if "--max-allowed-packet" in argv
    ]
    assert hashlib.sha256(sent.encode()).hexdigest() == entry(run_dir, "dump")["sha256"]
    assert entry(run_dir, "restore")["fed_sha256"] == entry(run_dir, "dump")["sha256"]


def test_recorded_collation_is_never_pasted_into_sql(dumped, tmp_path):
    # P1 audit: restore builds ALTER DATABASE ... COLLATE <recorded name>.
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    log = run_dir / "run.log"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    for e in entries:
        if e["step"] == "dump":
            e["database_collation"] = "utf8mb4_bin; CREATE TABLE mig_injected (i INT)"
    log.write_text("".join(json.dumps(e) + "\n" for e in entries))
    reset_target()
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 2, result.stdout + result.stderr
    assert target_tables() == []


# ── restore must not swallow warnings (D4) ─────────────────────────────────

# Like a real dump, it turns strict mode off: 'abc' into an INT is then a
# warning, and the row is stored as 0.
LOSSY_DUMP = b"""/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
CREATE TABLE mig_w (id INT PRIMARY KEY, n INT);
INSERT INTO mig_w VALUES (1, 'abc');
-- Dump completed
"""


def run_with_handmade_dump(dumped, run_dir, data):
    """A run directory whose recorded dump is ``data`` (checksum and all)."""
    (entry_,) = [e for e in read_log(dumped) if e["step"] == "dump"]
    (run_dir / "dump.sql").write_bytes(data)
    entry_.update(
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        tables=1,
        object_names=["TABLE mig_w"],
    )
    (run_dir / "run.log").write_text(json.dumps(entry_) + "\n")
    return run_dir


def test_restore_fails_on_any_warning(dumped, tmp_path):
    run_dir = run_with_handmade_dump(dumped, tmp_path, LOSSY_DUMP)
    reset_target()
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1366" in result.stdout + result.stderr  # Incorrect integer value
    assert entry(run_dir, "restore", "failed")


# ── --dry-run ────────────────────────────────────────────────────────────────


def test_dump_dry_run_writes_no_file(same_flavour, tmp_path):
    assert tool("--run-dir", str(tmp_path), *SNAPSHOT).returncode == 0
    result = tool("--run-dir", str(tmp_path), "dump", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout.lower()
    assert "--single-transaction" in result.stdout
    assert SRC_PASSWORD not in result.stdout
    assert [p.name for p in tmp_path.iterdir()] == ["run.log"]


def test_restore_dry_run_loads_nothing(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target(other_collation())
    result = tool("--run-dir", str(run_dir), "restore", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout.lower()
    assert target_tables() == []
    assert db_collation(DST, DST_DB) == other_collation()


# ── interruption and clean-up ────────────────────────────────────────────────


def _kill_restore_midway(run_dir):
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "restore"],
        env=tool_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # so the kill takes the database client with it
    )
    deadline = time.monotonic() + 60
    while len(target_tables()) < 3:
        assert proc.poll() is None, "restore finished before it could be interrupted"
        assert time.monotonic() < deadline
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _target_sessions():
    return admin(
        DST,
        "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER = 'mig_user'",
    )[0][0]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP], ids=["TERM", "HUP"])
def test_a_signalled_restore_stops_its_client_and_logs_the_outcome(
    sig, dumped, tmp_path
):
    # D3: only the Python process is signalled, as a closed terminal or a
    # plain `kill` would. Its database client must not carry on loading.
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), "restore"],
        env=tool_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while len(target_tables()) < 3:
        assert proc.poll() is None, "restore finished before it could be signalled"
        assert time.monotonic() < deadline
    proc.send_signal(sig)
    assert proc.wait(timeout=30) == 2
    time.sleep(2)
    assert _target_sessions() == "0", "the restore's client is still loading"
    statuses = [e["status"] for e in read_log(run_dir) if e["step"] == "restore"]
    assert statuses == ["started", "interrupted"]


def test_interrupted_restore_is_detected_cleaned_up_and_redone(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    _kill_restore_midway(run_dir)
    source_count = len(admin(SRC, "SHOW TABLES", SRC_DB))
    time.sleep(1)  # let the server notice its client is gone
    partial = target_tables()
    assert 0 < len(partial) < source_count

    # Everything now refuses the half-loaded target...
    result = tool("--run-dir", str(run_dir), *PREFLIGHT)
    assert result.returncode == 1
    assert "target_empty" in result.stdout
    result = tool("--run-dir", str(run_dir), "restore")
    assert result.returncode == 2
    assert "not empty" in result.stderr
    assert "reset-target" in result.stderr
    assert tool("--run-dir", str(run_dir), "verify").returncode == 1

    # ...until the documented clean-up, which demands the database's name.
    result = tool("--run-dir", str(run_dir), "reset-target")
    assert result.returncode == 2
    result = tool(
        "--run-dir", str(run_dir), "reset-target", "--confirm", f"{DST.host}/wrong_name"
    )
    assert result.returncode == 2
    assert target_tables() == partial

    result = tool(
        "--run-dir",
        str(run_dir),
        "reset-target",
        "--confirm",
        CONFIRM,
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert target_tables() == partial

    result = tool("--run-dir", str(run_dir), "reset-target", "--confirm", CONFIRM)
    assert result.returncode == 0, result.stderr
    assert target_tables() == []

    for step in (PREFLIGHT, ["restore"], ["verify"]):
        result = tool("--run-dir", str(run_dir), *step)
        assert result.returncode == 0, f"{step}: {result.stdout}{result.stderr}"
    statuses = [(e["step"], e["status"]) for e in read_log(run_dir)]
    assert ("restore", "started") in statuses
    assert statuses[-1] == ("verify", "ok")


def test_reset_target_only_empties_a_database_this_run_loaded(dumped, tmp_path):
    """The operator's worst slip: MIG_TARGET_* pointing at the real database."""
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    as_if_source_were_target = tool_env(
        MIG_TARGET_HOST=SRC.host,
        MIG_TARGET_PORT=SRC.port,
        MIG_TARGET_PASSWORD=SRC_PASSWORD,
        MIG_TARGET_DATABASE=SRC_DB,
    )
    before = admin(SRC, "SELECT COUNT(*) FROM chatbot_utterance", SRC_DB)
    result = tool(
        "--run-dir",
        str(run_dir),
        "reset-target",
        "--confirm",
        f"{SRC.host}/{SRC_DB}",
        env=as_if_source_were_target,
    )
    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert admin(SRC, "SELECT COUNT(*) FROM chatbot_utterance", SRC_DB) == before

    # Same with only the target configured and a database this run never loaded.
    del as_if_source_were_target["MIG_SOURCE_HOST"]
    result = tool(
        "--run-dir",
        str(run_dir),
        "reset-target",
        "--confirm",
        f"{SRC.host}/{SRC_DB}",
        env=as_if_source_were_target,
    )
    assert result.returncode == 2
    assert "records no restore" in result.stderr
    assert admin(SRC, "SELECT COUNT(*) FROM chatbot_utterance", SRC_DB) == before


def test_reset_target_refuses_a_database_that_was_not_empty_before(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    admin(DST, "CREATE TABLE precious (id INT PRIMARY KEY)", DST_DB)
    assert (
        tool(
            "--run-dir", str(run_dir), "restore", "--allow-non-empty-target"
        ).returncode
        == 0
    )
    result = tool("--run-dir", str(run_dir), "reset-target", "--confirm", CONFIRM)
    assert result.returncode == 2
    assert "was not empty" in result.stderr
    assert "precious" in target_tables()


# ── reset-target must never empty the wrong database (D1) ──────────────────


def reset(run_dir, confirm=CONFIRM, env=None):
    return tool(
        "--run-dir", str(run_dir), "reset-target", "--confirm", confirm, env=env
    )


def test_reset_target_refuses_after_a_successful_verify(copy):
    run_dir, results = copy
    assert results["verify"].returncode == 0
    before = target_tables()
    result = reset(run_dir)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "verif" in result.stderr
    assert target_tables() == before


@pytest.fixture
def half_restored(dumped, tmp_path):
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    _kill_restore_midway(run_dir)
    time.sleep(1)  # let the server notice its client is gone
    return run_dir


def test_reset_target_refuses_objects_the_dump_did_not_name(half_restored):
    admin(DST, "CREATE TABLE mig_stowaway (id INT PRIMARY KEY)", DST_DB)
    result = reset(half_restored)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "mig_stowaway" in result.stderr
    assert "mig_stowaway" in target_tables()


def test_reset_target_checks_the_server_identity_by_query(half_restored):
    # As if the recorded restore had reached some other server.
    log = half_restored / "run.log"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    for e in entries:
        if e["step"] == "restore" and e["status"] == "started":
            e["target_identity"] = {
                **e.get("target_identity", {}),
                "hostname": "elsewhere",
            }
    log.write_text("".join(json.dumps(e) + "\n" for e in entries))
    before = target_tables()
    result = reset(half_restored)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "identity" in result.stderr
    assert target_tables() == before


def test_reset_target_refuses_while_others_are_connected(half_restored):
    idle = subprocess.Popen(
        [CLIENT, *_root_args(DST), DST_DB],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"MYSQL_PWD": ROOT_PASSWORD, "PATH": "/usr/bin:/bin"},
    )
    idle.stdin.write(b"DO SLEEP(6);")
    idle.stdin.close()
    time.sleep(0.5)
    before = target_tables()
    try:
        result = reset(half_restored)
    finally:
        idle.wait()
    assert result.returncode == 2, result.stdout + result.stderr
    assert "session" in result.stderr
    assert target_tables() == before


def test_reset_target_confirmation_names_host_and_database(half_restored):
    before = target_tables()
    result = reset(half_restored, confirm=DST_DB)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "host/database" in result.stderr
    assert target_tables() == before


def test_reset_target_refuses_once_a_later_restore_went_elsewhere(dumped, tmp_path):
    # Review 2: restore into T1 and verify it; start a restore into T2 from the
    # same run directory; reset-target on T1 must not empty the proven copy.
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    assert tool("--run-dir", str(run_dir), "restore").returncode == 0
    assert tool("--run-dir", str(run_dir), "verify").returncode == 0
    admin(
        DST,
        "DROP DATABASE IF EXISTS mig_dst2; CREATE DATABASE mig_dst2;"
        "GRANT ALL PRIVILEGES ON mig_dst2.* TO 'mig_user'@'%'",
    )
    try:
        t2 = tool_env(MIG_TARGET_DATABASE="mig_dst2")
        assert tool("--run-dir", str(run_dir), "restore", env=t2).returncode == 0
        before = target_tables()
        result = reset(run_dir)
    finally:
        admin(DST, "DROP DATABASE IF EXISTS mig_dst2")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "another server" in result.stderr
    assert target_tables() == before


def test_restore_client_runs_sandboxed(dumped, tmp_path):
    # Review 3: like every other client run, where the client supports it.
    run_dir = fresh_run_with_dump(dumped, tmp_path)
    reset_target()
    result, commands = tool_recording_commands(
        tmp_path, "--run-dir", str(run_dir), "restore"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (load,) = [c for c in commands if "--show-warnings" in c]
    supported = (
        "--sandbox"
        in subprocess.run(
            [CLIENT, "--no-defaults", "--help"], capture_output=True, text=True
        ).stdout
    )
    assert ("--sandbox" in load) == supported, load


def test_option_file_cannot_make_the_dump_flush_or_lock(same_flavour, tmp_path):
    # Review 3: a [mysqldump] group in the operator's option file must not add
    # FLUSH, global locks or binlog changes. mig_user lacks RELOAD and SUPER,
    # so any of these would make the dump fail.
    option_file = tmp_path / "source.cnf"
    option_file.write_text(
        f"[client]\nhost={SRC.host}\nport={SRC.port}\nuser={USER}\n"
        f"password={SRC_PASSWORD}\nssl-verify-server-cert=0\n"
        "[mysqldump]\nflush-logs\nmaster-data=2\nlock-all-tables\ndelete-master-logs\n"
    )
    option_file.chmod(0o600)
    env = tool_env(MIG_SOURCE_DEFAULTS_FILE=str(option_file))
    for name in ("HOST", "PORT", "USER", "PASSWORD"):
        del env[f"MIG_SOURCE_{name}"]
    run_dir = tmp_path / "run"
    result = tool("--run-dir", str(run_dir), "dump", env=env)
    assert result.returncode == 0, result.stdout + result.stderr


# ── and the app, last (it writes on login) ───────────────────────────────────


def test_app_reads_the_conversation_from_a_tool_made_copy(same_flavour, tmp_path):
    reset_target(other_collation())
    for step in STEPS:
        assert tool("--run-dir", str(tmp_path), *step).returncode == 0
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
    assert seen["texts"][0] == EMOJI_TEXT[:40]
    assert seen["created_time"] == "2025-03-08T21:00:15.123456+00:00"
    assert seen["total_utterances"] == N_UTTERANCES + 9
