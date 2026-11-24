#!/usr/bin/env python3
"""dbcopy: copy a MariaDB/MySQL database to another server and prove the copy.

Steps (each runs on its own):

    verify      prove source and target hold the same schema and the same data

Connections come from the environment, never from the command line (arguments
are visible in the process list):

    MIG_SOURCE_HOST  MIG_SOURCE_PORT  MIG_SOURCE_USER  MIG_SOURCE_PASSWORD
    MIG_SOURCE_DATABASE  [MIG_SOURCE_DEFAULTS_FILE = path to a client option file]
    MIG_TARGET_...   the same six for the target

Only the standard library and the ``mariadb`` client binary are used.

Exit status: 0 success, 1 the check found a problem, 2 could not run.
"""

import argparse
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


class Refuse(Exception):
    """A precondition failed or the step could not run. Exit status 2."""


def say(message):
    sys.stdout.write(message + "\n")


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


def _defaults(conn):
    """Option-file arguments; must come first on any client command line.

    Only the file the operator named is read, never ~/.my.cnf or /etc/my.cnf:
    an inherited ``safe-updates``, ``select_limit`` or ``force`` there would
    silently cap or excuse what this tool reads.
    """
    if conn.defaults_file:
        return [f"--defaults-file={conn.defaults_file}"]
    return ["--no-defaults"]


def _spawn(conn, sql, writable=False):
    """The only place a database client is started.

    Every session is READ ONLY unless ``writable`` is passed, and a writable
    session on the source is refused outright: no step of this tool may change
    the source, which is what makes "point the app back at the old database" a
    complete rollback. The server enforces it (error 1792), not this script.
    """
    if not CLIENT:
        raise Refuse("no `mariadb` or `mysql` client binary on PATH")
    if writable and conn.role != "target":
        raise Refuse("refusing to open a writable session on the source")
    args = [CLIENT, *_defaults(conn)]
    for flag, value in (("host", conn.host), ("port", conn.port), ("user", conn.user)):
        if value:
            args.append(f"--{flag}={value}")
    args += [
        "--protocol=tcp",
        "--default-character-set=utf8mb4",
        "--batch",
        "--skip-column-names",
        "--quick",  # stream rows; never buffer a whole table in the client
        "--skip-force",  # stop at the first SQL error, even if a file says force
        conn.database,
    ]
    # No row cap, whatever an option file's init-command or the server's
    # global settings say (2^64-1 is "no limit").
    preamble = (
        "SET time_zone = '+00:00';"
        "SET SESSION sql_select_limit = 18446744073709551615;"
        "SET SESSION max_join_size = 18446744073709551615;"
    )
    if not writable:
        preamble += "SET SESSION TRANSACTION READ ONLY;"
    if conn.version and conn.flavour == "mysql":
        # MySQL 8 caches information_schema statistics (AUTO_INCREMENT) for a day.
        preamble += "SET SESSION information_schema_stats_expiry = 0;"
    env = dict(os.environ)
    if conn.password:
        env["MYSQL_PWD"] = conn.password  # environment, not argv
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


ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def connect(role):
    conn = Conn.from_env(role)
    conn.version, digest = query(conn, "SELECT VERSION(), SHA2('abc', 256)")[0]
    # Everything verify proves rests on the server's SHA2 (NULL on a server
    # built without it).
    if digest != ABC_SHA256:
        raise Refuse(
            f"{role}: SHA2('abc', 256) returned {digest!r}, not the known digest"
        )
    return conn


def identity(conn):
    """Which server and database this session really reached, by asking it.

    Host names in the environment prove nothing: a tunnel, an alias or a
    renamed endpoint can lead anywhere.
    """
    names = ["hostname", "port", "datadir", "database"]
    sql = "SELECT @@hostname, @@port, @@datadir, DATABASE()"
    if conn.flavour == "mysql":
        names.append("server_uuid")
        sql += ", @@server_uuid"
    return dict(zip(names, query(conn, sql)[0], strict=True))


def check_different(src, dst):
    if identity(src) == identity(dst):
        raise Refuse("source and target are the same database on the same server")


# Without these, information_schema silently hides triggers (TRIGGER), events
# (EVENT), routines (EXECUTE) and view bodies (SHOW VIEW): both sides would
# show nothing and agree.
DEFINITION_PRIVILEGES = ("SELECT", "SHOW VIEW", "TRIGGER", "EVENT", "EXECUTE")


def missing_privileges(conn, needed):
    """Privileges in ``needed`` the account holds neither globally nor on the
    database. Grants made through roles are not seen here: this fails safe."""
    me = "REPLACE(GRANTEE, CHAR(39), '') = CURRENT_USER()"
    held = {
        row[0]
        for row in query(
            conn,
            f"SELECT PRIVILEGE_TYPE FROM information_schema.USER_PRIVILEGES WHERE {me}"
            " UNION SELECT PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES"
            f" WHERE {me} AND DATABASE() LIKE TABLE_SCHEMA",
        )
    }
    return [p for p in needed if p not in held]


def require_definition_privileges(conn):
    missing = missing_privileges(conn, DEFINITION_PRIVILEGES)
    if missing:
        raise Refuse(
            f"{conn.role}: the account lacks {', '.join(missing)} on {conn.database}; "
            "without them triggers, events, routines or view definitions are "
            "invisible and would compare as equal. Grant "
            f"{', '.join(DEFINITION_PRIVILEGES)} on the database.",
        )


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
        "views": {},
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
        if row["table_type"] == "VIEW":
            continue  # read below, from information_schema.VIEWS
        if row["table_type"] != "BASE TABLE":
            raise Refuse(
                f"{conn.role}: {row['table_name']} is a {row['table_type']}; "
                "verify can only compare base tables and views",
            )
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
            if row["definition_sha256"] is None:
                raise Refuse(
                    f"{conn.role}: cannot read the body of {kind[:-1]} {row[name.lower()]}; "
                    "the account must be its definer or hold SHOW_ROUTINE (MySQL) / "
                    "SHOW CREATE ROUTINE or SELECT on mysql.proc (MariaDB)",
                )
            schema[kind][row.pop(name.lower())] = row
    # Each side's definition names its own database: drop that qualifier.
    for row in _rows(
        conn,
        ["TABLE_NAME",
         "REPLACE(VIEW_DEFINITION, CONCAT('`', DATABASE(), '`.'), '') AS definition",
         "CHECK_OPTION", "SECURITY_TYPE"],
        "VIEWS",
    ):  # fmt: skip
        if not row["definition"]:
            raise Refuse(
                f"{conn.role}: cannot read the definition of view "
                f"{row['table_name']}; grant SHOW VIEW on the database",
            )
        schema["views"][row.pop("table_name")] = row
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
    for kind in ("views", "routines", "triggers", "events"):
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

    Numbers and datetimes are hashed as the server renders them as text
    (FLOAT widened to DOUBLE first, see ``cell``); the
    session time zone is pinned to UTC. That rendering is the same for two
    servers of the same flavour, which is all verify accepts.
    """

    def cell(name):
        # FLOAT renders with only 6 significant digits; widened to DOUBLE it
        # renders exactly (every float is exactly representable as a double).
        if columns[name]["data_type"] == "float":
            return f"{q(name)} + 0E0"
        return q(name)

    cells = ", ".join(f"IFNULL(SHA2({cell(c)}, 256), 'NULL')" for c in columns)
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
    sql = row_hash_sql(name, table["column"], table["primary_key"])
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
    # Independent of how the rows were read: every row the table has was hashed.
    for side, conn in enumerate((src, dst)):
        total = int(query(conn, f"SELECT COUNT(*) FROM {q(name)}")[0][0])
        if total != counts[side]:
            raise Refuse(
                f"{name}: {conn.role} has {total} rows by COUNT(*) but only "
                f"{counts[side]} row hashes were read; the comparison is incomplete"
            )

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
    src, dst = connect("source"), connect("target")
    check_same_flavour(src, dst)
    check_different(src, dst)
    for conn in (src, dst):
        require_definition_privileges(conn)
    if args.dry_run:
        say(
            "dry-run: would compare schema and every row of "
            f"{src.database} on {src.host} with {dst.database} on {dst.host} "
            "(read-only on both sides); nothing written.",
        )
        return 0
    started = time.monotonic()
    schema_src, schema_dst = read_schema(src), read_schema(dst)
    if not schema_src["tables"]:
        raise Refuse(
            f"source database {src.database} has no tables: nothing to compare"
        )
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
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

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
        help="directory for this run's report files (default: ./migration-run)",
    )
    steps = parser.add_subparsers(dest="step", required=True)
    verify = steps.add_parser("verify", help="prove source and target are identical")
    verify.add_argument("--dry-run", action="store_true", help="say what would run")
    verify.set_defaults(func=step_verify)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Refuse as error:
        sys.stderr.write(f"REFUSED: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
