"""Guarantees that hold for every step: the source cannot be written, and
credentials never reach the command line, the output or the run log."""

import pytest

from .conftest import (
    DST,
    DST_DB,
    DST_PASSWORD,
    SRC,
    SRC_DB,
    SRC_PASSWORD,
    USER,
    admin,
    load_tool,
    plain_copy,
    read_log,
    reset_target,
    tool,
    tool_env,
    tool_watching_processes,
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
    for step in (["preflight", *PREFLIGHT_FLAGS], ["verify"]):
        result, command_lines = tool_watching_processes(
            "--run-dir", str(tmp_path), *step
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # The watcher did see the database client being run.
        assert any("--batch" in line and SRC.host in line for line in command_lines)
        _assert_no_secret("\n".join(command_lines), "the process list")
        _assert_no_secret(result.stdout + result.stderr, "the tool's output")
    for path in tmp_path.iterdir():
        _assert_no_secret(path.read_text(), path.name)
    assert USER not in (tmp_path / "run.log").read_text()


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
    result, command_lines = tool_watching_processes(
        "--run-dir", str(run_dir), "verify", env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    _assert_no_secret("\n".join(command_lines), "the process list")
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
