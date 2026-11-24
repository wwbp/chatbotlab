"""Guarantees that hold for every step: the source cannot be written, and
credentials never reach the command line, the output or the run log."""

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
    return [c for c in commands if f"--host={SRC.host} " in c and f"{SRC_DB}" in c]


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
