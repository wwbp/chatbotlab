"""``dbcopy.py verify`` must never say IDENTICAL when it did not really look.

Each test sets up a way the comparison could be blinded, partial or pointed
at the wrong thing, and checks verify refuses (2) or reports a difference (1)
instead of passing. Objects a test creates are removed again.
"""

import pytest

from .conftest import DST, DST_DB, SRC, SRC_DB, admin, plain_copy, shim, tool, tool_env


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
