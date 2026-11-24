#!/usr/bin/env python3
"""dbcopy: copy a MariaDB/MySQL database to another server and prove the copy.

Steps (each runs on its own):

    preflight   check both servers; fail on anything that would spoil the copy
    snapshot-check  record which snapshot protects the source (dump needs it)
    dump        consistent logical dump of the source to <run-dir>/dump.sql
    restore     load that dump into the (empty) target
    verify      prove source and target hold the same schema and the same data
    reset-target  empty a target this run loaded, after a failed restore

Every step takes --dry-run, and appends one line to <run-dir>/run.log saying
what ran, against which host and database, how long it took and what it found.

Connections come from the environment, never from the command line (arguments
are visible in the process list):

    MIG_SOURCE_HOST  MIG_SOURCE_PORT  MIG_SOURCE_USER  MIG_SOURCE_PASSWORD
    MIG_SOURCE_DATABASE  [MIG_SOURCE_DEFAULTS_FILE = path to a client option file]
    MIG_TARGET_...   the same six for the target

Only the standard library and the ``mariadb`` and ``mariadb-dump`` client
binaries are used.

Exit status: 0 success, 1 the check found a problem, 2 could not run.
"""

import argparse
import contextlib
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CLIENT = shutil.which("mariadb") or shutil.which("mysql")
DUMP = shutil.which("mariadb-dump") or shutil.which("mysqldump")


class Refuse(Exception):
    """A precondition failed or the step could not run. Exit status 2."""


def scrub(text):
    """Belt and braces: no configured password may reach output or the log."""
    for role in ("SOURCE", "TARGET"):
        secret = os.environ.get(f"MIG_{role}_PASSWORD")
        if secret:
            text = text.replace(secret, "***")
    return text


def say(message):
    sys.stdout.write(scrub(message) + "\n")


# ── connections ──────────────────────────────────────────────────────────────


@dataclass
class Conn:
    role: str  # "source" or "target"
    host: str
    port: str
    user: str
    password: str
    database: str
    defaults_file: str
    version: str = ""

    @classmethod
    def from_env(cls, role):
        def get(name):
            return os.environ.get(f"MIG_{role.upper()}_{name}", "")

        conn = cls(
            role,
            get("HOST"),
            get("PORT"),
            get("USER"),
            get("PASSWORD"),
            get("DATABASE"),
            get("DEFAULTS_FILE"),
        )
        if not conn.database or not (conn.host or conn.defaults_file):
            raise Refuse(
                f"{role}: set MIG_{role.upper()}_DATABASE and MIG_{role.upper()}_HOST "
                f"(or MIG_{role.upper()}_DEFAULTS_FILE). See --help.",
            )
        return conn

    @property
    def flavour(self):
        return "mariadb" if "mariadb" in self.version.lower() else "mysql"

    def describe(self):
        """For logs and reports: where, never who or with what password."""
        return {
            "host": self.host or f"(from {self.defaults_file})",
            "port": self.port or "default",
            "database": self.database,
            "flavour": self.flavour,
            "version": self.version,
        }


def _command(binary, conn, *options):
    """(argv, environment) for a client binary. Credentials are never in argv."""
    if not binary:
        raise Refuse("the mariadb / mariadb-dump client binaries are not on PATH")
    args = [binary]
    if conn.defaults_file:
        args.append(f"--defaults-extra-file={conn.defaults_file}")  # must be first
    for flag, value in (("host", conn.host), ("port", conn.port), ("user", conn.user)):
        if value:
            args.append(f"--{flag}={value}")
    args += ["--protocol=tcp", "--default-character-set=utf8mb4", *options]
    env = dict(os.environ)
    if conn.password:
        env["MYSQL_PWD"] = conn.password  # environment, not argv
    return [*args, conn.database], env


def _spawn(conn, sql, writable=False):
    """Run SQL in a client session. Every query of every step goes through here.

    The session is READ ONLY unless ``writable`` is passed, and a writable
    session on the source is refused outright: no step of this tool may change
    the source, which is what makes "point the app back at the old database" a
    complete rollback. The server enforces it (error 1792), not this script.

    The two other places a client is started are ``step_dump`` (mariadb-dump,
    which only reads) and ``load_dump`` (which refuses anything but the target).
    """
    if writable and conn.role != "target":
        raise Refuse("refusing to open a writable session on the source")
    # --quick: stream rows; never buffer a whole table in the client.
    args, env = _command(CLIENT, conn, "--batch", "--skip-column-names", "--quick")
    preamble = "SET time_zone = '+00:00';"
    if not writable:
        preamble += "SET SESSION TRANSACTION READ ONLY;"
    if conn.version and conn.flavour == "mysql":
        # MySQL 8 caches information_schema statistics (AUTO_INCREMENT) for a day.
        preamble += "SET SESSION information_schema_stats_expiry = 0;"
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    # SQL goes in on stdin. Statements here are small; the pipe never fills.
    proc.stdin.write((preamble + sql).encode())
    proc.stdin.close()
    return proc


def _finish(conn, proc):
    stderr = proc.stderr.read().decode(errors="replace").strip()
    if proc.wait() != 0:
        raise Refuse(f"{conn.role} database: {stderr or 'client failed'}")


_UNESCAPE = {"0": "\0", "n": "\n", "t": "\t", "\\": "\\"}


def _field(raw):
    # The client prints SQL NULL as the bare word NULL in batch mode. Queries
    # in this file never select a string that could itself be "NULL".
    if raw == "NULL":
        return None
    return re.sub(r"\\([0nt\\])", lambda m: _UNESCAPE[m.group(1)], raw)


def query(conn, sql, writable=False):
    """Run SQL, return rows as lists of str/None."""
    proc = _spawn(conn, sql, writable)
    out = proc.stdout.read().decode()
    _finish(conn, proc)
    return [[_field(f) for f in line.split("\t")] for line in out.splitlines()]


def connect(role):
    conn = Conn.from_env(role)
    conn.version = query(conn, "SELECT VERSION()")[0][0]
    return conn


def connect_both(args):
    """Connect to source and target, note them for the run log, and refuse
    the pairings no step may run against."""
    src = connect("source")
    args.record["source"] = src.describe()
    dst = connect("target")
    args.record["target"] = dst.describe()
    check_same_flavour(src, dst)
    identity = "SELECT @@hostname, @@port, @@datadir, DATABASE()"
    if query(src, identity) == query(dst, identity):
        raise Refuse("source and target are the same database on the same server")
    return src, dst


# ── schema ───────────────────────────────────────────────────────────────────

_HERE = "TABLE_SCHEMA = DATABASE()"


def _rows(conn, columns, table, where=_HERE, order=""):
    names = [c.split(" AS ")[-1].split(".")[-1].lower() for c in columns]
    sql = f"SELECT {', '.join(columns)} FROM information_schema.{table} WHERE {where}"
    return [
        dict(zip(names, row, strict=True))
        for row in query(conn, sql + (f" ORDER BY {order}" if order else ""))
    ]


def read_schema(conn):
    """Everything that defines the database's structure, as plain dicts.

    Read from information_schema rather than SHOW CREATE TABLE so a difference
    can be named ("column x: collation a vs b") instead of diffing DDL text.
    """
    default = query(
        conn,
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
        "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()",
    )[0]
    schema = {
        "database": {
            "default character set": default[0],
            "default collation": default[1],
        },
        "tables": {},
        "views": [],
        "routines": {},
        "triggers": {},
        "events": {},
    }
    for row in _rows(
        conn,
        [
            "TABLE_NAME",
            "TABLE_TYPE",
            "ENGINE",
            "TABLE_COLLATION",
            "ROW_FORMAT",
            "AUTO_INCREMENT",
        ],
        "TABLES",
    ):
        if row["table_type"] != "BASE TABLE":
            # ponytail: views are compared by name only (their definitions embed
            # the database name). The app has none; compare definitions if it
            # ever does.
            schema["views"].append(row["table_name"])
            continue
        schema["tables"][row.pop("table_name")] = {
            "options": {
                "engine": row["engine"],
                "collation": row["table_collation"],
                "row_format": row["row_format"],
            },
            "auto_increment": int(row["auto_increment"])
            if row["auto_increment"]
            else None,
            "column": {},
            "index": {},
            "foreign key": {},
            "check": {},
            "primary_key": [],
        }
    tables = schema["tables"]
    for row in _rows(
        conn,
        [
            "TABLE_NAME",
            "COLUMN_NAME",
            "ORDINAL_POSITION",
            "COLUMN_TYPE",
            "DATA_TYPE",
            "IS_NULLABLE",
            "COLUMN_DEFAULT",
            "CHARACTER_SET_NAME",
            "COLLATION_NAME",
            "EXTRA",
            "GENERATION_EXPRESSION",
        ],
        "COLUMNS",
        order="TABLE_NAME, ORDINAL_POSITION",
    ):
        if row["table_name"] in tables:
            tables[row.pop("table_name")]["column"][row.pop("column_name")] = row
    for row in _rows(
        conn,
        [
            "TABLE_NAME",
            "INDEX_NAME",
            "COLUMN_NAME",
            "NON_UNIQUE",
            "SUB_PART",
            "COLLATION",
            "INDEX_TYPE",
        ],
        "STATISTICS",
        order="TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
    ):
        table = tables[row.pop("table_name")]
        table["index"].setdefault(row.pop("index_name"), []).append(row)
    for table in tables.values():
        table["primary_key"] = [
            p["column_name"] for p in table["index"].get("PRIMARY", [])
        ]
    for row in _rows(
        conn,
        ["k.TABLE_NAME", "k.CONSTRAINT_NAME", "k.COLUMN_NAME", "k.REFERENCED_TABLE_NAME",
         "k.REFERENCED_COLUMN_NAME", "r.UPDATE_RULE", "r.DELETE_RULE"],
        "KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
        "ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA AND r.TABLE_NAME = k.TABLE_NAME "
        "AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME",
        where="k.TABLE_SCHEMA = DATABASE() AND k.REFERENCED_TABLE_NAME IS NOT NULL",
        order="k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION",
    ):  # fmt: skip
        table = tables[row.pop("table_name")]
        table["foreign key"].setdefault(row.pop("constraint_name"), []).append(row)
    # CHECK constraints (MariaDB's JSON columns are LONGTEXT + CHECK json_valid).
    if conn.flavour == "mariadb":
        checks = _rows(
            conn,
            ["TABLE_NAME", "CONSTRAINT_NAME", "CHECK_CLAUSE"],
            "CHECK_CONSTRAINTS",
            where="CONSTRAINT_SCHEMA = DATABASE()",
        )
    else:
        checks = _rows(
            conn,
            ["t.TABLE_NAME", "t.CONSTRAINT_NAME", "c.CHECK_CLAUSE"],
            "TABLE_CONSTRAINTS t JOIN information_schema.CHECK_CONSTRAINTS c "
            "ON c.CONSTRAINT_SCHEMA = t.CONSTRAINT_SCHEMA "
            "AND c.CONSTRAINT_NAME = t.CONSTRAINT_NAME",
            where="t.TABLE_SCHEMA = DATABASE() AND t.CONSTRAINT_TYPE = 'CHECK'",
        )
    for row in checks:
        tables[row["table_name"]]["check"][row["constraint_name"]] = row["check_clause"]
    for kind, table, name, columns in (
        ("routines", "ROUTINES", "ROUTINE_NAME",
         ["ROUTINE_TYPE", "SHA2(ROUTINE_DEFINITION, 256) AS definition_sha256", "SQL_MODE"]),
        ("triggers", "TRIGGERS", "TRIGGER_NAME",
         ["EVENT_OBJECT_TABLE", "ACTION_TIMING", "EVENT_MANIPULATION",
          "SHA2(ACTION_STATEMENT, 256) AS definition_sha256"]),
        ("events", "EVENTS", "EVENT_NAME",
         ["STATUS", "SHA2(EVENT_DEFINITION, 256) AS definition_sha256"]),
    ):  # fmt: skip
        where = f"{table[:-1]}_SCHEMA = DATABASE()"
        for row in _rows(conn, [name, *columns], table, where=where):
            schema[kind][row.pop(name.lower())] = row
    schema["views"].sort()
    return schema


def _explain(a, b):
    """Short text for what differs between two schema fragments."""
    if a is None:
        return "missing on source"
    if b is None:
        return "missing on target"
    if isinstance(a, dict) and isinstance(b, dict):
        parts = [
            f"{k}: source={a.get(k)!r} target={b.get(k)!r}"
            for k in sorted(a.keys() | b.keys())
            if a.get(k) != b.get(k)
        ]
        return "; ".join(parts)
    return f"source={a!r} target={b!r}"


def diff_schema(src, dst):
    """Differences as dicts: table, kind, key, detail."""
    diffs = []

    def add(table, kind, key, detail):
        diffs.append({"table": table, "kind": kind, "key": key, "detail": detail})

    for key in src["database"]:
        if src["database"][key] != dst["database"][key]:
            add(
                None,
                "database",
                key,
                _explain(src["database"][key], dst["database"][key]),
            )
    if src["views"] != dst["views"]:
        add(None, "database", "views", _explain(src["views"], dst["views"]))
    for kind in ("routines", "triggers", "events"):
        for name in sorted(src[kind].keys() | dst[kind].keys()):
            a, b = src[kind].get(name), dst[kind].get(name)
            if a != b:
                add(None, "database", f"{kind[:-1]} {name}", _explain(a, b))
    for name in sorted(src["tables"].keys() | dst["tables"].keys()):
        a, b = src["tables"].get(name), dst["tables"].get(name)
        if b is None:
            add(
                name, "table_missing_on_target", None, "table exists only on the source"
            )
            continue
        if a is None:
            add(name, "table_extra_on_target", None, "table exists only on the target")
            continue
        if a["options"] != b["options"]:
            add(name, "schema", "table options", _explain(a["options"], b["options"]))
        for part in ("column", "index", "foreign key", "check"):
            for item in sorted(a[part].keys() | b[part].keys()):
                if a[part].get(item) != b[part].get(item):
                    detail = _explain(a[part].get(item), b[part].get(item))
                    add(name, "schema", f"{part} {item}", detail)
        # The target's counter may be ahead (rows added since), never behind:
        # behind means new rows would reuse ids that deleted rows once had.
        if (a["auto_increment"] or 0) > (b["auto_increment"] or 0):
            add(
                name,
                "auto_increment",
                None,
                f"source={a['auto_increment']} target={b['auto_increment']} "
                "(target must be at least the source's)",
            )
    return diffs


# ── content ──────────────────────────────────────────────────────────────────


def q(name):
    return "`" + name.replace("`", "``") + "`"


INTEGER_TYPES = {"tinyint", "smallint", "mediumint", "int", "bigint"}


def row_hash_sql(table, columns, primary_key):
    """One output line per row: ``<primary key>\\t<sha256 of the row>``.

    The server hashes each cell's own bytes with SHA-256, then hashes the
    cell hashes together. Why this shape:

    * bytes, not SQL equality: a trailing space, a case change or an emoji
      swapped for a look-alike are equal under many collations and all differ
      here;
    * NULL is hashed as a marker no cell hash can equal, so NULL and '' differ;
    * hashing per cell first keeps every intermediate value at 64 characters.
      Concatenating raw cells would exceed max_allowed_packet on long text, and
      MySQL/MariaDB then return NULL *with only a warning*: both sides would
      "agree". (The reader below also rejects any line without a 64-hex hash.)
    * rows are read ORDER BY primary key, so the result does not depend on how
      or in what order either server stores them.

    Numbers and datetimes are hashed as the server renders them as text; the
    session time zone is pinned to UTC. That rendering is the same for two
    servers of the same flavour, which is all verify accepts.
    """
    cells = ", ".join(f"IFNULL(SHA2({q(c)}, 256), 'NULL')" for c in columns)
    row = f"SHA2(CONCAT_WS('|', {cells}), 256)"
    if primary_key:
        keys = ", ".join(q(c) for c in primary_key)
        return f"SELECT CONCAT_WS(',', {keys}), {row} FROM {q(table)} ORDER BY {keys}"
    # ponytail: no primary key -> sort the row hashes themselves (a server-side
    # filesort of the whole table). Fine for small tables; every table the app
    # owns has a primary key.
    return f"SELECT '', h FROM (SELECT {row} AS h FROM {q(table)}) x ORDER BY h"


def _split(line):
    """(key, hash) from one output line, or raise if it is not a hash line."""
    key, sep, digest = line.rstrip(b"\n").rpartition(b"\t")
    if not sep or len(digest) != 64:
        raise Refuse(f"unexpected row-hash output: {line[:200]!r}")
    return key.decode(errors="replace"), digest


def compare_table(src, dst, name, table):
    """Stream both sides' row hashes in key order and compare line by line.

    Memory use is constant: nothing is collected. Returns (result, diffs).
    """
    sql = row_hash_sql(name, list(table["column"]), table["primary_key"])
    integer_key = table["primary_key"] and all(
        table["column"][c]["data_type"] in INTEGER_TYPES for c in table["primary_key"]
    )
    procs = [_spawn(src, sql), _spawn(dst, sql)]
    sums = [hashlib.sha256(), hashlib.sha256()]
    counts = [0, 0]
    first = None
    for lines in itertools.zip_longest(procs[0].stdout, procs[1].stdout):
        for side, line in enumerate(lines):
            if line is not None:
                if len(line) < 66 or line[-66] != 9:  # "<key>\t<64 hex>\n"
                    raise Refuse(f"{name}: unexpected row-hash output {line[:200]!r}")
                sums[side].update(line)
                counts[side] += 1
        if first is None and lines[0] != lines[1]:
            first = lines
    _finish(src, procs[0])
    _finish(dst, procs[1])

    result = {
        "rows_source": counts[0],
        "rows_target": counts[1],
        "checksum_source": sums[0].hexdigest(),
        "checksum_target": sums[1].hexdigest(),
    }
    diffs = []
    if counts[0] != counts[1]:
        detail = f"source={counts[0]} target={counts[1]}"
        diffs.append(
            {"table": name, "kind": "row_count", "key": None, "detail": detail}
        )
    if first is not None:
        a = _split(first[0]) if first[0] is not None else None
        b = _split(first[1]) if first[1] is not None else None
        if not table["primary_key"]:
            kind, key = "rows_differ", None
            detail = "table has no primary key; the sorted row hashes differ"
        elif b is None:
            kind, key, detail = "row_missing_on_target", a[0], "target ended first"
        elif a is None:
            kind, key, detail = "row_extra_on_target", b[0], "source ended first"
        elif a[0] == b[0]:
            kind, key, detail = "row_differs", a[0], "same key, different content"
        elif integer_key:
            ka = [int(x) for x in a[0].split(",")]
            kb = [int(x) for x in b[0].split(",")]
            if ka < kb:
                kind, key = "row_missing_on_target", a[0]
            else:
                kind, key = "row_extra_on_target", b[0]
            detail = f"first divergence: source key {a[0]}, target key {b[0]}"
        else:
            # A non-integer key sorts by collation, which this script cannot
            # reproduce; the key present on only one side is one of these two.
            kind, key = "row_extra_on_target", b[0]
            if not _key_exists(dst, name, table["primary_key"], a[0]):
                kind, key = "row_missing_on_target", a[0]
            detail = f"first divergence: source key {a[0]}, target key {b[0]}"
        diffs.append({"table": name, "kind": kind, "key": key, "detail": detail})
    return result, diffs


def _key_exists(conn, table, primary_key, key):
    keys = ", ".join(q(c) for c in primary_key)
    literal = "0x" + key.encode().hex() if key else "''"
    sql = (
        f"SELECT COUNT(*) FROM {q(table)} WHERE CAST(CONCAT_WS(',', {keys}) AS BINARY) "
        f"= CAST({literal} AS BINARY)"
    )
    return query(conn, sql)[0][0] != "0"


# ── preflight ────────────────────────────────────────────────────────────────


def missing_collations(used, available):
    """Collations the source uses that the target server does not have.

    MariaDB 10.10+ lists its UCA-14 collations without a character set
    (``uca1400_ai_ci``) while columns report the full name
    (``utf8mb4_uca1400_ai_ci``), so both spellings are accepted.
    """
    return sorted(
        c
        for c in used
        if c and c not in available and c.partition("_")[2] not in available
    )


def _version(conn):
    return tuple(int(x) for x in re.findall(r"\d+", conn.version)[:2])


def server_info(conn):
    row = query(
        conn,
        "SELECT @@character_set_server, @@collation_server, "
        "@@lower_case_table_names, @@max_allowed_packet, @@sql_mode, "
        "DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
        "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()",
    )
    if not row:
        raise Refuse(f"{conn.role}: database {conn.database} not found")
    row = row[0]
    return {
        **conn.describe(),
        "server_character_set": row[0],
        "server_collation": row[1],
        "lower_case_table_names": row[2],
        "max_allowed_packet": int(row[3]),
        "sql_mode": row[4],
        "database_character_set": row[5],
        "database_collation": row[6],
    }


def table_activity(conn):
    """(everything listed, base tables) with exact row counts on the latter."""
    listed = _rows(
        conn,
        ["TABLE_NAME", "TABLE_TYPE", "ENGINE", "AUTO_INCREMENT", "UPDATE_TIME",
         "DATA_LENGTH + INDEX_LENGTH AS bytes"],
        "TABLES",
        order="TABLE_NAME",
    )  # fmt: skip
    base = [r for r in listed if r["table_type"] == "BASE TABLE"]
    counts = {}
    if base:
        # COUNT(*), because information_schema.TABLE_ROWS is only an estimate.
        union = " UNION ALL ".join(
            f"SELECT '{r['table_name']}', COUNT(*) FROM {q(r['table_name'])}"
            for r in base
        )
        counts = {name: int(n) for name, n in query(conn, union)}
    for row in base:
        row["rows"] = counts[row["table_name"]]
    return listed, base


def has_select_on_database(conn):
    # An account granted only some tables sees a smaller database and nothing
    # errors. Grants made through roles are not seen here: this fails safe.
    me = "REPLACE(GRANTEE, CHAR(39), '') = CURRENT_USER() AND PRIVILEGE_TYPE = 'SELECT'"
    sql = (
        f"SELECT (SELECT COUNT(*) FROM information_schema.USER_PRIVILEGES WHERE {me})"
        f" + (SELECT COUNT(*) FROM information_schema.SCHEMA_PRIVILEGES WHERE {me}"
        " AND DATABASE() LIKE TABLE_SCHEMA)"
    )
    return query(conn, sql)[0][0] != "0"


SCRATCH = "_dbcopy_preflight"


def target_can_load(dst):
    """Do, on a scratch table, each kind of thing a restore does."""
    try:
        query(
            dst,
            f"CREATE TABLE {SCRATCH} (id INT AUTO_INCREMENT PRIMARY KEY, v INT, KEY (v));"
            f"INSERT INTO {SCRATCH} (v) VALUES (1);"
            f"ALTER TABLE {SCRATCH} AUTO_INCREMENT = 100;"
            f"DROP TABLE {SCRATCH};",
            writable=True,
        )
    except Refuse as error:
        with contextlib.suppress(Refuse):
            query(dst, f"DROP TABLE IF EXISTS {SCRATCH}", writable=True)
        return str(error)
    return None


def quiesced_problem(src, seconds):
    """Why the source does not look idle, or None.

    Two signs are checked: another session connected to the database, and any
    table whose row count, AUTO_INCREMENT or last-write time moves while we
    watch. This is a sample, not a proof: stop the app first. The proof is
    verify, which fails if the source changed after the dump.
    """
    sessions = int(
        query(
            src,
            "SELECT COUNT(*) FROM information_schema.PROCESSLIST "
            "WHERE DB = DATABASE() AND ID <> CONNECTION_ID()",
        )[0][0]
    )
    if sessions:
        return (
            f"{sessions} other session(s) connected to the source database "
            "(the app, or someone's client, is still attached)"
        )

    def sample():
        return {
            r["table_name"]: (r["rows"], r["auto_increment"], r["update_time"])
            for r in table_activity(src)[1]
        }

    before = sample()
    time.sleep(seconds)
    after = sample()
    moved = sorted(
        t for t in before.keys() | after.keys() if before.get(t) != after.get(t)
    )
    if moved:
        return f"written to during a {seconds}s watch: {', '.join(moved)}"
    return None


def step_preflight(args):
    src, dst = connect_both(args)
    if args.dry_run:
        say(
            f"dry-run: connected to source {src.database} on {src.host} "
            f"({src.version}) and target {dst.database} on {dst.host} "
            f"({dst.version}). Would check versions, character sets, settings, "
            "privileges (creating and dropping one scratch table on the "
            "target), that the target is empty, and count every source row.",
        )
        return 0
    checks = []

    def check(name, problem, detail="", fatal=True):
        status = "ok" if not problem else "fail" if fatal else "warn"
        checks.append({"name": name, "status": status, "detail": problem or detail})

    client = subprocess.run(
        [CLIENT, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    check(
        "client",
        ""
        if "MariaDB" in client
        else f"only tested with MariaDB's client, found: {client}",
        client,
    )

    problem = ""
    if _version(dst) < _version(src):
        problem = f"target {dst.version} is older than source {src.version}"
        if not args.allow_version_downgrade:
            problem += "; pass --allow-version-downgrade to accept"
    check(
        "versions",
        problem,
        f"{src.flavour} {src.version} -> {dst.flavour} {dst.version}",
        fatal=not args.allow_version_downgrade,
    )

    a, b = server_info(src), server_info(dst)
    problem = ""
    if a["database_collation"] != b["database_collation"]:
        problem = (
            f"target database default is {b['database_character_set']}/"
            f"{b['database_collation']}, source is {a['database_character_set']}/"
            f"{a['database_collation']}: the target's default must be changed to "
            "match before loading (restore does this), or tables created later "
            "would use a different collation"
        )
    check(
        "database_defaults",
        problem,
        f"{a['database_character_set']}/{a['database_collation']} on both",
        fatal=False,
    )
    used = {
        r[0]
        for r in query(
            src,
            "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() UNION SELECT TABLE_COLLATION "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()",
        )
    } | {a["database_collation"]}
    missing = missing_collations(used, {r[0] for r in query(dst, "SHOW COLLATION")})
    check(
        "collations",
        f"not available on the target: {', '.join(missing)}" if missing else "",
        f"{len(used - {None})} collation(s) in use, all available on the target",
    )
    problems = []
    if a["lower_case_table_names"] != b["lower_case_table_names"]:
        problems.append(
            "lower_case_table_names differs (table names would change case)"
        )
    if b["max_allowed_packet"] < a["max_allowed_packet"]:
        problems.append(
            f"target max_allowed_packet {b['max_allowed_packet']} is below the "
            f"source's {a['max_allowed_packet']} (long rows may not load)",
        )
    check(
        "settings",
        "; ".join(problems),
        "lower_case_table_names and max_allowed_packet compatible",
    )
    problem = ""
    if a["sql_mode"] != b["sql_mode"]:
        problem = f"differs: source={a['sql_mode']} target={b['sql_mode']}"
    check("sql_mode", problem, "same on both", fatal=False)

    _, base = table_activity(src)
    a["tables"] = {r["table_name"]: r["rows"] for r in base}
    a["size_bytes"] = sum(int(r["bytes"] or 0) for r in base)
    other = sorted(r["table_name"] for r in base if r["engine"] != "InnoDB")
    problem = ""
    if other:
        problem = (
            "not InnoDB, so a single-transaction dump is only consistent if "
            f"the source is quiesced: {', '.join(other)}"
        )
    # A quiesced source needs no snapshot isolation.
    check(
        "source_engines",
        problem,
        "all tables are InnoDB",
        fatal=not args.require_quiesced,
    )
    problem = ""
    if not has_select_on_database(src):
        problem = (
            "the source account does not hold the SELECT privilege on the "
            "whole database; tables it cannot see would silently be left out"
        )
    check(
        "source_privileges",
        problem,
        f"SELECT on the whole database; {len(base)} tables readable",
    )

    target_listed, target_base = table_activity(dst)
    b["tables"] = {r["table_name"]: r["rows"] for r in target_base}
    present = [r["table_name"] for r in target_listed]
    problem = ""
    if present:
        problem = (
            f"target already has {len(present)} table(s): {', '.join(present[:10])}"
        )
        if not args.allow_non_empty_target:
            problem += "; pass --allow-non-empty-target to accept"
    check("target_empty", problem, "no tables", fatal=not args.allow_non_empty_target)
    check(
        "target_privileges",
        target_can_load(dst) or "",
        "create, index, insert, alter and drop all work",
    )

    if args.require_quiesced:
        check(
            "source_quiesced",
            quiesced_problem(src, args.quiesce_seconds) or "",
            f"no other sessions; no writes during a {args.quiesce_seconds}s watch",
        )
    else:
        checks.append(
            {
                "name": "source_quiesced",
                "status": "skipped",
                "detail": "--require-quiesced not given",
            }
        )

    failed = [c["name"] for c in checks if c["status"] == "fail"]
    report = {
        "ok": not failed,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checks": checks,
        "source": a,
        "target": b,
    }
    args.run_dir.mkdir(parents=True, exist_ok=True)
    path = args.run_dir / "preflight-report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.record.update(
        source_tables=len(base),
        source_rows=sum(a["tables"].values()),
        source_bytes=a["size_bytes"],
        failed_checks=failed,
        report=str(path),
    )

    for side in (a, b):
        say(
            f"{side['flavour']} {side['version']} on {side['host']}, database "
            f"{side['database']}: server {side['server_character_set']}/"
            f"{side['server_collation']}, database {side['database_character_set']}/"
            f"{side['database_collation']}"
        )
    for name, rows in a["tables"].items():
        say(f"  {name:<45} {rows:>10} rows")
    say(
        f"  source: {len(base)} tables, {sum(a['tables'].values())} rows, about "
        f"{a['size_bytes'] / 1e6:.1f} MB (data + indexes, the engine's estimate)"
    )
    for c in checks:
        say(f"  {c['status']:<7} {c['name']}: {c['detail']}")
    verdict = f"PREFLIGHT FAILED: {', '.join(failed)}" if failed else "PREFLIGHT OK"
    say(f"{verdict}. Report: {path}")
    return 1 if failed else 0


# ── dump, restore, reset-target ──────────────────────────────────────────────

# Read-only by construction: none of these makes mariadb-dump lock, flush or
# write anything on the source (a test reads the server's general log to
# confirm). Deterministic: the same data gives the same file, byte for byte.
DUMP_OPTIONS = [
    "--single-transaction",  # one consistent InnoDB snapshot, no locks
    "--skip-lock-tables",
    "--quick",
    "--order-by-primary",
    "--hex-blob",
    "--skip-dump-date",
    "--triggers",
    "--no-tablespaces",
    # Restore must only ever add. With the default DROP TABLE IF EXISTS lines a
    # dump loaded into the wrong database would destroy what is there.
    "--skip-add-drop-table",
    "--max-allowed-packet=1G",
]
TRAILER = b"-- Dump completed"


def run_log(args):
    path = args.run_dir / "run.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def append_log(args, **entry):
    args.run_dir.mkdir(parents=True, exist_ok=True)
    entry = {"time": datetime.now(UTC).isoformat(timespec="seconds"), **entry}
    with (args.run_dir / "run.log").open("a") as log:
        log.write(scrub(json.dumps(entry, sort_keys=True)) + "\n")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _objects(conn):
    """Everything in the database that a dump creates: {kind: [names]}."""
    found = {"TABLE": [], "VIEW": [], "PROCEDURE": [], "FUNCTION": [], "EVENT": []}
    for row in _rows(conn, ["TABLE_NAME", "TABLE_TYPE"], "TABLES", order="TABLE_NAME"):
        kind = "TABLE" if row["table_type"] == "BASE TABLE" else "VIEW"
        found[kind].append(row["table_name"])
    for row in _rows(
        conn,
        ["ROUTINE_NAME", "ROUTINE_TYPE"],
        "ROUTINES",
        "ROUTINE_SCHEMA = DATABASE()",
    ):
        found[row["routine_type"]].append(row["routine_name"])
    for row in _rows(conn, ["EVENT_NAME"], "EVENTS", "EVENT_SCHEMA = DATABASE()"):
        found["EVENT"].append(row["event_name"])
    return found


def step_dump(args):
    src = connect("source")
    args.record["source"] = src.describe()
    path = args.run_dir / "dump.sql"
    if path.exists():
        raise Refuse(f"{path} already exists; use a new --run-dir for a new dump")
    args.record["snapshot_id"] = _recorded_snapshot(args, src)
    version = subprocess.run(
        [DUMP or "mariadb-dump", "--version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    if "MariaDB" not in version:
        raise Refuse(
            f"only tested with MariaDB's mariadb-dump, found: {version.strip()}"
        )
    info = server_info(src)
    objects = _objects(src)
    other = [
        r["table_name"]
        for r in _rows(src, ["TABLE_NAME", "ENGINE", "TABLE_TYPE"], "TABLES")
        if r["table_type"] == "BASE TABLE" and r["engine"] != "InnoDB"
    ]
    if other and not args.source_is_quiesced:
        raise Refuse(
            f"not InnoDB: {', '.join(other)}. A single-transaction dump is only "
            "consistent for these if nothing is writing; stop the app and pass "
            "--source-is-quiesced.",
        )
    options = list(DUMP_OPTIONS)
    if objects["PROCEDURE"] or objects["FUNCTION"]:
        options.append("--routines")
    if objects["EVENT"]:
        options.append("--events")
    command, env = _command(DUMP, src, *options)
    if args.dry_run:
        say(f"dry-run: would run: {' '.join(command)} > {path}")
        say(f"dry-run: {len(objects['TABLE'])} tables; nothing written.")
        return 0

    args.run_dir.mkdir(parents=True, exist_ok=True)
    partial = args.run_dir / "dump.sql.partial"
    digest = hashlib.sha256()
    size = 0
    tail = b""
    # 0600 from creation: this file is the whole database in plain text.
    with os.fdopen(
        os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"
    ) as out:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        while chunk := proc.stdout.read(1 << 20):
            out.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            tail = (tail + chunk)[-200:]
        out.flush()
        os.fsync(out.fileno())
    stderr = proc.stderr.read().decode(errors="replace").strip()
    # mariadb-dump can die mid-table and still leave a plausible-looking file;
    # its last line is the only proof it reached the end.
    if proc.wait() != 0 or TRAILER not in tail:
        partial.unlink()
        raise Refuse(
            f"dump failed or is incomplete: {stderr or 'no completion marker'}"
        )
    partial.rename(path)
    (args.run_dir / "dump.sql.sha256").write_text(f"{digest.hexdigest()}  dump.sql\n")
    args.record.update(
        file=str(path),
        bytes=size,
        sha256=digest.hexdigest(),
        tables=len(objects["TABLE"]),
        database_character_set=info["database_character_set"],
        database_collation=info["database_collation"],
        options=options,
    )
    say(
        f"dump: {len(objects['TABLE'])} tables, {size / 1e6:.1f} MB written to {path}\n"
        f"dump: sha256 {digest.hexdigest()}"
    )
    return 0


def _recorded_dump(args):
    dumps = [
        e
        for e in run_log(args)
        if e["step"] == "dump" and e["status"] == "ok" and not e["dry_run"]
    ]
    if not dumps:
        raise Refuse(
            f"no dump is recorded in {args.run_dir / 'run.log'}; restore only "
            "loads a dump made by `dump` in the same run directory",
        )
    return dumps[-1]


def _identity(conn):
    """Which database, as written in the run log."""
    return {k: conn.describe()[k] for k in ("host", "port", "database")}


def step_snapshot_check(args):
    """Record the operator's statement about the source's snapshot.

    This tool does not call any cloud API. Taking the snapshot and proving it
    restores are manual (docs/deployment/data-migration.rst); this step makes
    that gate explicit: without it on record for this source, dump refuses.
    """
    source = _identity(Conn.from_env("source"))  # no connection is made
    args.record["source"] = source
    if not args.snapshot_id:
        raise Refuse("give the snapshot's identifier with --snapshot-id")
    if not args.snapshot_verified:
        raise Refuse(
            "pass --snapshot-verified to state that this snapshot is complete "
            "and that restoring it was proven (restored to a scratch instance "
            "and compared with `verify`). If that has not been done, do it first.",
        )
    if args.dry_run:
        say(f"dry-run: would record snapshot {args.snapshot_id}; nothing recorded.")
        return 0
    args.record.update(snapshot_id=args.snapshot_id, snapshot_verified=True)
    say(
        f"snapshot-check: recorded snapshot {args.snapshot_id} as taken and "
        f"restore-proven for {source['database']} on {source['host']}."
    )
    return 0


def _recorded_snapshot(args, src):
    recorded = [
        e
        for e in run_log(args)
        if e["step"] == "snapshot-check"
        and e["status"] == "ok"
        and not e["dry_run"]
        and e["source"] == _identity(src)
    ]
    if not recorded:
        raise Refuse(
            f"no snapshot is recorded for this source in {args.run_dir / 'run.log'}. "
            "Take a snapshot of the source, prove it restores, then run "
            "`snapshot-check --snapshot-id <id> --snapshot-verified`.",
        )
    return recorded[-1]["snapshot_id"]


def load_dump(dst, path):
    """Feed the dump file to the client. Target only, ever."""
    if dst.role != "target":
        raise Refuse("refusing to load a dump into the source")
    command, env = _command(CLIENT, dst, "--max-allowed-packet=1G")
    with path.open("rb") as handle:
        return subprocess.run(
            command, stdin=handle, capture_output=True, env=env, check=False
        )


def step_restore(args):
    dst = connect("target")
    args.record["target"] = dst.describe()
    dump = _recorded_dump(args)
    path = args.run_dir / "dump.sql"
    if not path.exists():
        raise Refuse(f"{path} not found")
    actual = _sha256(path)
    if actual != dump["sha256"]:
        raise Refuse(
            f"{path} does not match the checksum recorded when it was dumped "
            f"(recorded {dump['sha256']}, file is {actual}); it was changed, "
            "truncated or swapped. Dump again.",
        )
    if dump["source"]["flavour"] != dst.flavour:
        raise Refuse(
            f"the dump is from {dump['source']['flavour']} {dump['source']['version']} "
            f"but the target is {dst.flavour} {dst.version}; not supported",
        )
    existing = [name for names in _objects(dst).values() for name in names]
    if existing and not args.allow_non_empty_target:
        raise Refuse(
            f"target is not empty ({len(existing)} object(s): "
            f"{', '.join(existing[:10])}). If this is a failed restore from this "
            "run, clean up with `reset-target`; otherwise check MIG_TARGET_*.",
        )
    info = server_info(dst)
    align = ""
    if info["database_collation"] != dump["database_collation"]:
        # Tables in the dump carry their own collation; this is for tables the
        # app's future migrations create, which take the database default.
        align = (
            f"ALTER DATABASE {q(dst.database)} CHARACTER SET "
            f"{dump['database_character_set']} COLLATE {dump['database_collation']}"
        )
    if args.dry_run:
        say(
            f"dry-run: dump checksum matches; would load {dump['bytes'] / 1e6:.1f} MB "
            f"({dump['tables']} tables) into {dst.database} on {dst.host}"
            + (f" after: {align}" if align else "")
            + "; nothing written."
        )
        return 0
    # Written before the first change, so an interrupted restore leaves a
    # record that this run is what touched the target (reset-target needs it).
    append_log(
        args,
        step="restore",
        status="started",
        dry_run=False,
        target=dst.describe(),
        target_was_empty=not existing,
        sha256=actual,
    )
    if align:
        query(dst, align, writable=True)
    result = load_dump(dst, path)
    loaded = len(_objects(dst)["TABLE"])
    args.record.update(sha256=actual, bytes=dump["bytes"], tables=loaded)
    if result.returncode != 0 or loaded < dump["tables"]:
        say(
            "restore FAILED: "
            + (result.stderr.decode(errors="replace").strip() or "tables are missing")
            + f"\nThe target now holds a partial copy ({loaded} of {dump['tables']} "
            "tables). Nothing on the source was changed. Run `reset-target`, "
            "then `restore` again."
        )
        return 1
    say(
        f"restore: loaded {loaded} tables into {dst.database} on {dst.host}. Now run verify."
    )
    return 0


def step_reset_target(args):
    dst = connect("target")
    args.record["target"] = dst.describe()
    me = _identity(dst)
    if args.confirm_database != dst.database:
        raise Refuse(
            "reset-target drops every table in the target database. Confirm "
            f"with --confirm-database {dst.database}",
        )
    source = {k: os.environ.get(f"MIG_SOURCE_{k.upper()}", "") for k in me}
    if me == {**source, "port": source["port"] or "default"}:
        raise Refuse("MIG_TARGET_* points at the source database")
    started = [
        e
        for e in run_log(args)
        if e["step"] == "restore"
        and e["status"] == "started"
        and {k: e["target"][k] for k in me} == me
    ]
    # The guard against emptying the wrong database: only one that this run
    # found empty and then started loading can be reset.
    if not started:
        raise Refuse(
            f"{args.run_dir / 'run.log'} records no restore into {dst.database} "
            f"on {dst.host}; reset-target only empties a database this run loaded",
        )
    if not all(e["target_was_empty"] for e in started):
        raise Refuse(
            "the target was not empty before this run restored into it, so "
            "reset-target cannot tell the copy from what was there. Clean up by hand.",
        )
    objects = _objects(dst)
    count = sum(len(names) for names in objects.values())
    if args.dry_run:
        say(
            f"dry-run: would drop {count} object(s) from {dst.database} on "
            f"{dst.host} ({len(objects['TABLE'])} tables); nothing changed."
        )
        return 0
    drops = ["SET FOREIGN_KEY_CHECKS = 0"]
    for kind in ("VIEW", "TABLE", "PROCEDURE", "FUNCTION", "EVENT"):
        drops += [f"DROP {kind} {q(name)}" for name in objects[kind]]
    query(dst, ";".join(drops), writable=True)
    left = sum(len(names) for names in _objects(dst).values())
    args.record.update(dropped=count, remaining=left)
    say(f"reset-target: dropped {count} object(s) from {dst.database}; {left} remain.")
    return 1 if left else 0


# ── verify ───────────────────────────────────────────────────────────────────


def check_same_flavour(src, dst):
    if src.flavour != dst.flavour:
        raise Refuse(
            f"source is {src.flavour} {src.version} but target is {dst.flavour} "
            f"{dst.version}. Copying between MySQL and MariaDB is not supported: "
            "they store JSON differently (native type vs LONGTEXT) and do not "
            "share collations (utf8mb4_0900_* vs utf8mb4_uca1400_*), so the data "
            "cannot be proven unchanged. Create the target with the same engine "
            "as the source.",
        )


def step_verify(args):
    src, dst = connect_both(args)
    if args.dry_run:
        say(
            "dry-run: would compare schema and every row of "
            f"{src.database} on {src.host} with {dst.database} on {dst.host} "
            "(read-only on both sides); nothing written.",
        )
        return 0
    started = time.monotonic()
    schema_src, schema_dst = read_schema(src), read_schema(dst)
    diffs = diff_schema(schema_src, schema_dst)
    tables = {}
    for name, table in sorted(schema_src["tables"].items()):
        other = schema_dst["tables"].get(name)
        entry = {
            "auto_increment_source": table["auto_increment"],
            "auto_increment_target": other["auto_increment"] if other else None,
        }
        if other is None:
            entry["content"] = "not compared: table missing on target"
        elif list(table["column"]) != list(other["column"]):
            entry["content"] = "not compared: column lists differ"
        else:
            result, table_diffs = compare_table(src, dst, name, table)
            entry.update(result)
            diffs += table_diffs
        tables[name] = entry

    bad = {d["table"] for d in diffs}
    migrations = tables.get("django_migrations", {})
    report = {
        "ok": not diffs,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started, 2),
        "source": src.describe(),
        "target": dst.describe(),
        "tables_compared": len(tables),
        "rows_compared": sum(t.get("rows_source", 0) for t in tables.values()),
        "django_migrations": {
            "identical": "django_migrations" in tables
            and "django_migrations" not in bad,
            "rows_source": migrations.get("rows_source"),
            "rows_target": migrations.get("rows_target"),
        },
        "differences": diffs,
        "tables": tables,
    }
    args.run_dir.mkdir(parents=True, exist_ok=True)
    path = args.run_dir / "verify-report.json"
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    path.write_text(text)
    args.record.update(
        tables_compared=report["tables_compared"],
        rows_compared=report["rows_compared"],
        differences=len(diffs),
        report=str(path),
        report_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )

    for name, entry in tables.items():
        mark = "DIFF" if name in bad else "ok  "
        rows = entry.get("rows_source", "-")
        say(
            f"  {mark} {name:<45} {rows:>10} rows  {entry.get('checksum_source', '')[:16]}"
        )
    for d in diffs:
        where = d["table"] or "(database)"
        key = f" [{d['key']}]" if d["key"] else ""
        say(f"  DIFFERENCE {where}: {d['kind']}{key}: {d['detail']}")
    verdict = (
        f"DIFFERENT: {len(diffs)} difference(s)"
        if diffs
        else f"IDENTICAL: {report['tables_compared']} tables, {report['rows_compared']} rows"
    )
    say(f"verify: {verdict} in {report['duration_seconds']}s. Report: {path}")
    return 1 if diffs else 0


# ── command line ─────────────────────────────────────────────────────────────


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=Path("migration-run"),
        help="directory for this run's log and reports (default: ./migration-run)",
    )
    steps = parser.add_subparsers(dest="step", required=True)

    def step(name, func, text):
        sub = steps.add_parser(name, help=text)
        sub.add_argument("--dry-run", action="store_true", help="say what would run")
        sub.set_defaults(func=func)
        return sub

    preflight = step("preflight", step_preflight, "check both servers before copying")
    preflight.add_argument("--allow-non-empty-target", action="store_true")
    preflight.add_argument("--allow-version-downgrade", action="store_true")
    preflight.add_argument(
        "--require-quiesced",
        action="store_true",
        help="fail unless the source has no other sessions and no writes",
    )
    preflight.add_argument("--quiesce-seconds", type=int, default=5)
    snapshot = step(
        "snapshot-check", step_snapshot_check, "record the source's snapshot"
    )
    snapshot.add_argument("--snapshot-id", default="", metavar="ID")
    snapshot.add_argument(
        "--snapshot-verified",
        action="store_true",
        help="you assert the snapshot is complete and its restore was proven",
    )
    dump = step("dump", step_dump, "dump the source to <run-dir>/dump.sql")
    dump.add_argument(
        "--source-is-quiesced",
        action="store_true",
        help="accept non-InnoDB tables: the operator asserts nothing is writing",
    )
    restore = step("restore", step_restore, "load the dump into the empty target")
    restore.add_argument("--allow-non-empty-target", action="store_true")
    step("verify", step_verify, "prove source and target are identical")
    reset = step("reset-target", step_reset_target, "empty a target this run loaded")
    reset.add_argument("--confirm-database", default="", metavar="NAME")

    args = parser.parse_args(argv)
    args.record = {}
    started = time.monotonic()
    status, code = "refused", 2
    try:
        code = args.func(args)
        status = "ok" if code == 0 else "failed"
    except Refuse as error:
        args.record["message"] = str(error)
        sys.stderr.write(scrub(f"REFUSED: {error}\n"))
    finally:
        append_log(
            args,
            step=args.step,
            dry_run=args.dry_run,
            status=status,
            exit_code=code,
            duration_seconds=round(time.monotonic() - started, 2),
            **args.record,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
