"""Guarantees that hold for every step: the source cannot be written, and
credentials never reach the command line, the output or the run log."""

import re
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
    USER,
    admin,
    load_tool,
    plain_copy,
    read_log,
    recorded_stdin,
    reset_target,
    shim,
    tool,
    tool_env,
    tool_recording_commands,
)

# Enough for preflight to pass against a filled target on any same-flavour pair.
PREFLIGHT_FLAGS = ["--allow-non-empty-target", "--allow-version-downgrade"]

WRITES = [
    "INSERT INTO mig_empty VALUES (1, 'x')",
    "UPDATE chatbot_conversation SET study_name = 'tampered'",
    "DELETE FROM chatbot_utterance",
    "TRUNCATE TABLE chatbot_keystroke",
    "DROP TABLE mig_empty",
    "CREATE TABLE mig_intruder (id INT PRIMARY KEY)",
    "ALTER TABLE mig_empty ADD COLUMN extra INT",
]


@pytest.fixture
def dbcopy(source_db, monkeypatch):
    for name, value in tool_env().items():
        if name.startswith("MIG_"):
            monkeypatch.setenv(name, value)
    return load_tool()


def _source_fingerprint():
    return admin(
        SRC,
        "SELECT (SELECT COUNT(*) FROM chatbot_utterance), (SELECT COUNT(*) FROM chatbot_keystroke),"
        " (SELECT COUNT(*) FROM mig_empty), (SELECT COUNT(*) FROM chatbot_conversation"
        " WHERE study_name = 'tampered'), (SELECT COUNT(*) FROM information_schema.TABLES"
        " WHERE TABLE_SCHEMA = DATABASE()), (SELECT COUNT(*) FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE())",
        SRC_DB,
    )


@pytest.mark.parametrize("statement", WRITES, ids=lambda s: s.split()[0].lower())
def test_source_session_cannot_write(dbcopy, statement):
    # The account holds ALL PRIVILEGES on the source database (see the
    # harness): it is the session, not the grants, that stops this.
    before = _source_fingerprint()
    source = dbcopy.connect("source")
    with pytest.raises(dbcopy.Refuse, match=r"(?i)read.only"):
        dbcopy.query(source, statement)
    assert _source_fingerprint() == before


def test_a_writable_session_on_the_source_cannot_be_opened(dbcopy):
    source = dbcopy.connect("source")
    with pytest.raises(dbcopy.Refuse, match="writable session on the source"):
        dbcopy.query(source, "SELECT 1", writable=True)


def test_target_sessions_are_read_only_unless_asked(dbcopy):
    reset_target()
    target = dbcopy.connect("target")
    with pytest.raises(dbcopy.Refuse, match=r"(?i)read.only"):
        dbcopy.query(target, "CREATE TABLE nope (id INT PRIMARY KEY)")
    dbcopy.query(target, "CREATE TABLE yes (id INT PRIMARY KEY)", writable=True)
    dbcopy.query(target, "DROP TABLE yes", writable=True)


def _assert_no_secret(text, where):
    for secret in (SRC_PASSWORD, DST_PASSWORD):
        assert secret not in text, f"password found in {where}"


def test_no_credential_in_process_list_output_or_log(same_flavour, tmp_path):
    plain_copy()
    run_dir = tmp_path / "run"
    for step in (["preflight", *PREFLIGHT_FLAGS], ["verify"]):
        result, commands = tool_recording_commands(
            tmp_path, "--run-dir", str(run_dir), *step
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # Every query the step made is in the record: tens of client runs.
        assert sum(f"--host={SRC.host}" in c for c in commands) > 5
        assert sum(f"--host={DST.host}" in c for c in commands) > 5
        _assert_no_secret("\n".join(commands), "a client command line")
        _assert_no_secret(result.stdout + result.stderr, "the tool's output")
    for path in run_dir.iterdir():
        _assert_no_secret(path.read_text(), path.name)
    assert USER not in (run_dir / "run.log").read_text()


def test_wrong_password_is_refused_without_echoing_it(source_db, tmp_path):
    bad = "wrong-" + SRC_PASSWORD
    result = tool(
        "--run-dir", str(tmp_path), "verify", env=tool_env(MIG_SOURCE_PASSWORD=bad)
    )
    assert result.returncode == 2
    assert "Access denied" in result.stderr
    assert bad not in result.stdout + result.stderr
    assert SRC_PASSWORD not in (tmp_path / "run.log").read_text()


def test_credentials_can_come_from_a_client_option_file(same_flavour, tmp_path):
    plain_copy()
    option_file = tmp_path / "source.cnf"
    option_file.write_text(
        f"[client]\nhost={SRC.host}\nport={SRC.port}\nuser={USER}\npassword={SRC_PASSWORD}\n"
        # MariaDB 11.4+ clients verify the server's TLS certificate by default
        # and the test servers have none worth trusting. A real option file
        # would carry ssl-ca=<the provider's CA bundle> instead.
        "ssl-verify-server-cert=0\n"
    )
    option_file.chmod(0o600)
    env = tool_env(MIG_SOURCE_DEFAULTS_FILE=str(option_file))
    for name in ("HOST", "PORT", "USER", "PASSWORD"):
        del env[f"MIG_SOURCE_{name}"]
    run_dir = tmp_path / "run"
    result, commands = tool_recording_commands(
        tmp_path, "--run-dir", str(run_dir), "verify", env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(str(option_file) in c for c in commands)
    _assert_no_secret("\n".join(commands), "a client command line")
    _assert_no_secret((run_dir / "run.log").read_text(), "run.log")


def test_missing_configuration_is_explained(tmp_path):
    env = tool_env()
    del env["MIG_SOURCE_HOST"]
    result = tool("--run-dir", str(tmp_path), "verify", env=env)
    assert result.returncode == 2
    assert "MIG_SOURCE_HOST" in result.stderr


def test_passwords_are_not_accepted_as_arguments(tmp_path):
    result = tool("--run-dir", str(tmp_path), "verify", "--source-password", "x")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_run_log_records_each_step(same_flavour, tmp_path):
    plain_copy()
    assert (
        tool("--run-dir", str(tmp_path), "preflight", *PREFLIGHT_FLAGS).returncode == 0
    )
    assert tool("--run-dir", str(tmp_path), "verify").returncode == 0
    assert tool("--run-dir", str(tmp_path), "verify", "--dry-run").returncode == 0
    entries = read_log(tmp_path)
    assert [(e["step"], e["dry_run"]) for e in entries] == [
        ("preflight", False),
        ("verify", False),
        ("verify", True),
    ]
    for entry in entries:
        assert entry["time"].endswith("+00:00")
        assert entry["status"] == "ok"
        assert entry["exit_code"] == 0
        assert entry["duration_seconds"] >= 0
        assert entry["source"]["host"] == SRC.host
        assert entry["source"]["database"] == SRC_DB
        assert entry["target"]["version"]
    verify = entries[1]
    assert verify["rows_compared"] > 50_000
    assert verify["tables_compared"] >= 30
    assert len(verify["report_sha256"]) == 64
    assert entries[0]["source_rows"] > 50_000


def test_run_log_records_failures_and_refusals(same_flavour, tmp_path):
    plain_copy()
    admin(DST, "DELETE FROM mig_composite WHERE a = 2", DST_DB)
    assert tool("--run-dir", str(tmp_path), "verify").returncode == 1
    env = tool_env()
    del env["MIG_TARGET_DATABASE"]
    assert tool("--run-dir", str(tmp_path), "verify", env=env).returncode == 2
    failed, refused = read_log(tmp_path)
    assert failed["status"] == "failed"
    assert failed["differences"] == 2  # the row count, and the missing row
    assert refused["status"] == "refused"
    assert "MIG_TARGET_DATABASE" in refused["message"]


def test_run_log_says_crashed_not_refused(source_db, tmp_path):
    # V8: a client that prints nothing and succeeds makes the tool fail
    # unexpectedly; the log must not call that a refusal.
    env = shim(tmp_path, "mariadb", "cat >/dev/null; exit 0")
    result = tool("--run-dir", str(tmp_path / "run"), "verify", env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    (entry,) = read_log(tmp_path / "run")
    assert entry["status"] == "crashed"
    assert entry["exit_code"] == 2


def test_run_log_says_interrupted_not_refused(source_db, tmp_path):
    env = shim(tmp_path, "mariadb", "cat >/dev/null; exec sleep 5")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--run-dir", str(tmp_path / "run"), "verify"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1)
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=30) == 2
    (entry,) = read_log(tmp_path / "run")
    assert entry["status"] == "interrupted"


def test_help_states_the_exit_code_contract(tmp_path):
    # P3: code, tests, --help and the runbook use one rule.
    result = tool("--help")
    assert result.returncode == 0
    text = " ".join(result.stdout.split())
    assert (
        "0 succeeded; 1 a check or comparison found a problem; 2 refused, crashed or interrupted"
        in text
    )
    assert "Anything but 0 means stop" in text


def _source_commands(commands):
    return [
        c
        for c in commands
        if f"--host={SRC.host} --port={SRC.port} " in c and f"{SRC_DB}" in c
    ]


def test_read_only_survives_a_reconnect(same_flavour, tmp_path):
    # P4: setup sent once on stdin is lost if the client silently reconnects;
    # --init-command is re-run on every connect, and reconnecting is off.
    plain_copy()
    result, commands = tool_recording_commands(
        tmp_path, "--run-dir", str(tmp_path / "run"), "verify"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source = _source_commands(commands)
    assert len(source) > 5
    for command in source:
        assert "--skip-reconnect" in command, command
        assert "--init-command=" in command, command
        init = command.split("--init-command=", 1)[1]
        assert "SET SESSION TRANSACTION READ ONLY" in init, command


def test_each_client_gets_only_its_own_password(same_flavour, tmp_path):
    # P5: one environment file per client run: its argv, then its environment.
    plain_copy()
    runs = tmp_path / "runs"
    runs.mkdir()
    env = shim(
        tmp_path,
        "mariadb",
        f'f=$(mktemp -p {runs}); echo "$*" > "$f"; env >> "$f"; exec "$REAL" "$@"',
    )
    result = tool("--run-dir", str(tmp_path / "run"), "verify", env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = {"source": 0, "target": 0}
    # Booleans only: a failing assert must not print the environment.
    for path in runs.iterdir():
        argv, _, environment = path.read_text().partition("\n")
        leaked = (
            "MIG_SOURCE_PASSWORD" in environment or "MIG_TARGET_PASSWORD" in environment
        )
        assert not leaked, f"MIG_*_PASSWORD passed to: {argv}"
        if f"--host={SRC.host} --port={SRC.port} " in argv:
            seen["source"] += 1
            leaked = DST_PASSWORD in environment
        elif f"--host={DST.host} --port={DST.port} " in argv:
            seen["target"] += 1
            leaked = SRC_PASSWORD in environment
        assert not leaked, f"the other side's password passed to: {argv}"
    assert seen["source"] > 5
    assert seen["target"] > 5


def test_password_not_in_repr():
    dbcopy = load_tool()
    conn = dbcopy.Conn("source", "h", "1", "u", SRC_PASSWORD, "d", "")
    assert SRC_PASSWORD not in repr(conn)


def test_source_sessions_do_not_wait_a_year_for_a_lock(same_flavour, tmp_path):
    # P6: lock_wait_timeout defaults to a year; a held metadata lock on the
    # source must make a step fail in seconds, not hang (or queue the app).
    plain_copy()
    result, commands = tool_recording_commands(
        tmp_path, "--run-dir", str(tmp_path / "run"), "verify"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for command in _source_commands(commands):
        assert "SET SESSION lock_wait_timeout = 10" in command, command


def test_awkward_password_is_scrubbed_from_the_run_log(source_db, tmp_path):
    # P10: JSON escapes ", \\ and non-ASCII, so scrubbing the JSON text misses.
    password = 'q"u\\o-té-test-pw'
    env = shim(tmp_path, "mariadb", 'echo "denied for $MYSQL_PWD" >&2; exit 1')
    env["MIG_SOURCE_PASSWORD"] = password
    result = tool("--run-dir", str(tmp_path / "run"), "verify", env=env)
    assert result.returncode == 2
    leaked = password in result.stdout + result.stderr
    assert not leaked, "password in output"
    text = (tmp_path / "run" / "run.log").read_text()
    entries = read_log(tmp_path / "run")
    values = [v for e in entries for v in e.values() if isinstance(v, str)]
    leaked = password in text or any(password in v for v in values)
    assert not leaked, "password in run.log"


READ_WRITE = re.compile(r"(?i)read\s+write|(transaction|tx)_read_only")


def test_every_source_session_is_read_only_by_argv_and_stdin(same_flavour, tmp_path):
    # P11: every source client run gets the read-only setup in its arguments,
    # and nothing it is sent on stdin undoes it.
    plain_copy()
    run_dir = tmp_path / "run"
    for step in (["preflight", *PREFLIGHT_FLAGS], ["verify"]):
        result, _ = tool_recording_commands(tmp_path, "--run-dir", str(run_dir), *step)
        assert result.returncode == 0, result.stdout + result.stderr
    source = [
        (argv, sent)
        for argv, sent in recorded_stdin(tmp_path)
        if f"--host={SRC.host} --port={SRC.port} " in argv
    ]
    assert len(source) > 10
    for argv, sent in source:
        assert "SET SESSION TRANSACTION READ ONLY" in argv, argv
        assert not READ_WRITE.search(sent), sent[:500]
