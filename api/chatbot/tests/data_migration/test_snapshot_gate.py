"""``dbcopy.py snapshot-check``: the "snapshot taken and restore proven" gate.

The tool cannot take or test a cloud snapshot. What it can do is refuse to
dump until the operator has stated, on the record, which snapshot protects
the source and that restoring it was proven.
"""

from .conftest import SNAPSHOT, read_log, tool, tool_env

# The first two run in the one-server "Backend test" job, where no MIG_*
# connection settings exist: the missing flag must be named first (S1).


def test_refuses_without_a_snapshot_id(tmp_path):
    result = tool("--run-dir", str(tmp_path), "snapshot-check", "--snapshot-verified")
    assert result.returncode == 2
    assert "--snapshot-id" in result.stderr
    assert read_log(tmp_path)[-1]["status"] == "refused"


def test_refuses_without_the_operators_assertion(tmp_path):
    result = tool(
        "--run-dir", str(tmp_path), "snapshot-check", "--snapshot-id", "snap-1"
    )
    assert result.returncode == 2
    assert "--snapshot-verified" in result.stderr
    assert "restore" in result.stderr  # says what is being asserted
    assert read_log(tmp_path)[-1]["status"] == "refused"


def test_records_the_snapshot_against_the_source(tmp_path):
    # No database connection is needed: this step only records. So it runs in
    # the one-server "Backend test" job too, with a source that need not exist.
    env = tool_env(MIG_SOURCE_HOST="old-db.internal", MIG_SOURCE_DATABASE="chatbot_db")
    result = tool("--run-dir", str(tmp_path), *SNAPSHOT, env=env)
    assert result.returncode == 0, result.stderr
    assert SNAPSHOT[2] in result.stdout
    entry = read_log(tmp_path)[-1]
    assert entry["step"] == "snapshot-check"
    assert entry["status"] == "ok"
    assert entry["snapshot_id"] == SNAPSHOT[2]
    assert entry["snapshot_verified"] is True
    assert entry["source"]["host"] == "old-db.internal"
    assert entry["source"]["database"] == "chatbot_db"
    assert entry["time"].endswith("+00:00")


def test_dump_refuses_until_a_snapshot_is_recorded(same_flavour, tmp_path):
    result = tool("--run-dir", str(tmp_path), "dump")
    assert result.returncode == 2
    assert "snapshot-check" in result.stderr
    assert not (tmp_path / "dump.sql").exists()

    # A dry run of the gate records nothing that counts.
    assert tool("--run-dir", str(tmp_path), *SNAPSHOT, "--dry-run").returncode == 0
    assert tool("--run-dir", str(tmp_path), "dump").returncode == 2
    # Nor does a dry run of dump get past the gate.
    assert tool("--run-dir", str(tmp_path), "dump", "--dry-run").returncode == 2

    assert tool("--run-dir", str(tmp_path), *SNAPSHOT).returncode == 0
    result = tool("--run-dir", str(tmp_path), "dump")
    assert result.returncode == 0, result.stderr
    assert read_log(tmp_path)[-1]["snapshot_id"] == SNAPSHOT[2]


def test_a_snapshot_recorded_for_another_database_does_not_count(
    same_flavour, tmp_path
):
    elsewhere = tool_env(MIG_SOURCE_DATABASE="some_other_db")
    assert tool("--run-dir", str(tmp_path), *SNAPSHOT, env=elsewhere).returncode == 0
    result = tool("--run-dir", str(tmp_path), "dump")
    assert result.returncode == 2
    assert "snapshot-check" in result.stderr
    assert not (tmp_path / "dump.sql").exists()
