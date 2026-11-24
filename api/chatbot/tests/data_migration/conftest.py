"""Harness for the data-migration tests: two real database servers, no mocks.

These tests need a *second* database server, so they are skipped unless
MIGTEST_SOURCE_HOST, MIGTEST_TARGET_HOST and MIGTEST_ROOT_PASSWORD are set
(the CI job "Data migration test" sets them; the default backend suite does
not). The servers are throwaway: the harness creates and drops the databases
``mig_src`` and ``mig_dst`` and a ``mig_user`` account on each, as root.

The source gets the app's real schema (``manage.py migrate``) plus the data in
``fixture_data.py``.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "migration" / "dbcopy.py"
API_DIR = Path(__file__).resolve().parents[3]


class Server(NamedTuple):
    host: str
    port: str


SRC = Server(
    os.environ.get("MIGTEST_SOURCE_HOST", ""),
    os.environ.get("MIGTEST_SOURCE_PORT", "3306"),
)
DST = Server(
    os.environ.get("MIGTEST_TARGET_HOST", ""),
    os.environ.get("MIGTEST_TARGET_PORT", "3306"),
)
ROOT_PASSWORD = os.environ.get("MIGTEST_ROOT_PASSWORD", "")
SRC_DB, DST_DB = "mig_src", "mig_dst"
USER = "mig_user"
# Distinctive, so a test can search logs and process lists for them. Throwaway
# accounts on throwaway servers; built from parts so they are plainly not real.
SRC_PASSWORD = "-".join(["source", "side", "test", "pw"])
DST_PASSWORD = "-".join(["target", "side", "test", "pw"])

CLIENT = shutil.which("mariadb") or shutil.which("mysql") or "mysql"
DUMP = shutil.which("mariadb-dump") or shutil.which("mysqldump") or "mysqldump"


def _root_args(server):
    return [
        f"--host={server.host}",
        f"--port={server.port}",
        "--protocol=tcp",
        "--user=root",
    ]


def admin(server, sql, db=None, check=True):
    """Run SQL as root; rows come back as lists of strings."""
    result = subprocess.run(
        [
            CLIENT,
            *_root_args(server),
            "--default-character-set=utf8mb4",
            "--batch",
            "--skip-column-names",
            *([db] if db else []),
        ],
        input=sql.encode(),
        env={**os.environ, "MYSQL_PWD": ROOT_PASSWORD},
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"admin SQL failed on {server.host}:{server.port}: {result.stderr.decode()}"
        )
    return [line.split("\t") for line in result.stdout.decode().splitlines()]


def flavour(server):
    return (
        "mariadb"
        if "mariadb" in admin(server, "SELECT VERSION()")[0][0].lower()
        else "mysql"
    )


def tool_env(**overrides):
    env = {
        **os.environ,
        "MIG_SOURCE_HOST": SRC.host,
        "MIG_SOURCE_PORT": SRC.port,
        "MIG_SOURCE_USER": USER,
        "MIG_SOURCE_PASSWORD": SRC_PASSWORD,
        "MIG_SOURCE_DATABASE": SRC_DB,
        "MIG_TARGET_HOST": DST.host,
        "MIG_TARGET_PORT": DST.port,
        "MIG_TARGET_USER": USER,
        "MIG_TARGET_PASSWORD": DST_PASSWORD,
        "MIG_TARGET_DATABASE": DST_DB,
    }
    env.update(overrides)
    return env


def tool(*args, env=None):
    """Run the migration tool as an operator would: a subprocess, env credentials."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env or tool_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def django(code, server, db, password):
    """Run a snippet in a fresh Django process pointed at one of the test DBs."""
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "generic_chatbot.settings",
        "DATABASE_HOST": server.host,
        "DATABASE_PORT": server.port,
        "DATABASE_NAME": db,
        "DATABASE_USER": USER,
        "DATABASE_PASSWORD": password,
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result


def _wait_for(server):
    deadline = time.monotonic() + 120
    while True:
        try:
            admin(server, "SELECT 1")
            return
        except RuntimeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def _create_db_and_user(server, db, password, collation=None):
    collate = f" COLLATE {collation}" if collation else ""
    admin(
        server,
        f"DROP DATABASE IF EXISTS {db};"
        f"CREATE DATABASE {db} CHARACTER SET utf8mb4{collate};"
        f"CREATE USER IF NOT EXISTS '{USER}'@'%' IDENTIFIED BY '{password}';"
        f"ALTER USER '{USER}'@'%' IDENTIFIED BY '{password}';"
        # ALL, on purpose: the tool must be unable to write to the source
        # even when the account it is given could.
        f"GRANT ALL PRIVILEGES ON {db}.* TO '{USER}'@'%';"
        f"GRANT PROCESS ON *.* TO '{USER}'@'%';"
        # Lets a non-SUPER account create the fixture's trigger and procedure
        # on MySQL, where binary logging is on by default.
        "SET GLOBAL log_bin_trust_function_creators = 1;",
    )


def db_collation(server, db):
    return admin(
        server,
        "SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
        f"WHERE SCHEMA_NAME = '{db}'",
    )[0][0]


def reset_target(collation=None):
    """Drop and recreate the target database, empty, with the source's default
    collation where the target server has it (it may not, across flavours)."""
    try:
        _create_db_and_user(
            DST, DST_DB, DST_PASSWORD, collation or db_collation(SRC, SRC_DB)
        )
    except RuntimeError:
        _create_db_and_user(DST, DST_DB, DST_PASSWORD)


def plain_copy():
    """A known-good copy made without the tool: dump piped into the client.

    The verify tests need a faithful copy to mutate; using the tool's own
    dump/restore here would make them circular.
    """
    reset_target()
    dump = subprocess.Popen(
        [
            DUMP,
            *_root_args(SRC),
            "--default-character-set=utf8mb4",
            "--single-transaction",
            "--hex-blob",
            "--routines",
            "--events",
            SRC_DB,
        ],
        env={**os.environ, "MYSQL_PWD": ROOT_PASSWORD},
        stdout=subprocess.PIPE,
    )
    load = subprocess.run(
        [CLIENT, *_root_args(DST), "--default-character-set=utf8mb4", DST_DB],
        stdin=dump.stdout,
        env={**os.environ, "MYSQL_PWD": ROOT_PASSWORD},
        capture_output=True,
        check=False,
    )
    assert dump.wait() == 0
    assert load.returncode == 0, load.stderr.decode()


@pytest.fixture(autouse=True)
def _needs_the_script():
    # The dev container mounts only api/; CI checks out the whole repository.
    if not SCRIPT.exists():
        pytest.skip("needs scripts/migration/dbcopy.py, which is outside api/")


@pytest.fixture(scope="session")
def servers():
    if not (SRC.host and DST.host and ROOT_PASSWORD):
        pytest.skip(
            "needs two database servers: set MIGTEST_SOURCE_HOST, MIGTEST_TARGET_HOST, MIGTEST_ROOT_PASSWORD"
        )
    _wait_for(SRC)
    _wait_for(DST)
    return {"source": flavour(SRC), "target": flavour(DST)}


@pytest.fixture(scope="session")
def source_db(servers):
    """The source: real app schema plus the awkward fixture. Built once."""
    reuse = os.environ.get("MIGTEST_REUSE_SOURCE") and admin(
        SRC, f"SHOW TABLES IN {SRC_DB} LIKE 'mig_empty'", check=False
    )
    if not reuse:
        _create_db_and_user(SRC, SRC_DB, SRC_PASSWORD)
        django(
            "import django; django.setup();"
            "from django.core.management import call_command;"
            "call_command('migrate', verbosity=0);"
            "from chatbot.tests.data_migration.fixture_data import build; build()",
            SRC,
            SRC_DB,
            SRC_PASSWORD,
        )
    # An empty target database and account, whatever the test then does.
    _create_db_and_user(DST, DST_DB, DST_PASSWORD)
    return servers


@pytest.fixture(scope="session")
def same_flavour(source_db):
    if source_db["source"] != source_db["target"]:
        pytest.skip("source and target are different database flavours")
    return source_db["source"]


def read_report(run_dir):
    return json.loads((Path(run_dir) / "verify-report.json").read_text())


def load_tool():
    """Import scripts/migration/dbcopy.py as a module (it is not a package)."""
    spec = importlib.util.spec_from_file_location("dbcopy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dbcopy"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def read_log(run_dir):
    lines = (Path(run_dir) / "run.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


def tool_recording_commands(tmp_path, *args, env=None):
    """Run the tool and record every database client command line it issues.

    Wrappers named like the clients go first on PATH; each appends its full
    argument list (exactly what `ps` would show) to a file, then becomes the
    real client. Complete and deterministic, unlike sampling the process list.
    Returns (result, list of command lines).
    """
    bin_dir = tmp_path / "recording-bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "recorded-commands.txt"
    record.write_text("")
    for name, real in (("mariadb", CLIENT), ("mariadb-dump", DUMP)):
        wrapper = bin_dir / name
        wrapper.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$0 $*\" >> '{record}'\nexec '{real}' \"$@\"\n"
        )
        wrapper.chmod(0o755)
    env = dict(env or tool_env())
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    result = tool(*args, env=env)
    return result, record.read_text().splitlines()
