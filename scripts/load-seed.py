#!/usr/bin/env python3
"""Load examples/seed-demo.sql against the configured Oracle database.

Handles what a naive `split(';')` cannot:
  - PL/SQL anonymous blocks terminated by `/` on its own line
  - Semicolons inside string literals
  - SQL*Plus directives (SET, PROMPT, WHENEVER) — silently skipped
  - Final SELECT for row-count summary — fetched and printed

Usage:
    python scripts/load-seed.py [path/to/seed.sql]

Defaults to examples/seed-demo.sql relative to repo root.

Requires the same ORACLE_USER / ORACLE_PASSWORD / ORACLE_DSN /
ORACLE_WALLET_DIR / ORACLE_WALLET_PASSWORD env vars the OracleOps
plugin uses. Run from the repo root with the venv active:

    cd /Users/ranjithkondoju/oracleops
    source .venv/bin/activate
    python scripts/load-seed.py
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

# Make the plugins/ directory importable
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugins"))

from oracle.connection import get_pool  # noqa: E402


# SQL*Plus directives we should silently skip — they're not SQL.
SQLPLUS_DIRECTIVE = re.compile(
    r"^\s*(SET|PROMPT|WHENEVER|SPOOL|CONNECT|EXIT|QUIT|CLEAR|COL|"
    r"COLUMN|DEFINE|UNDEFINE|TIMING|LINESIZE|PAGESIZE|FEEDBACK|"
    r"ECHO|HEADING|TRIMSPOOL|REM|REMARK)\b",
    re.IGNORECASE,
)

# Errors we silently tolerate during a re-run of an idempotent seed.
# ORA-00942: table or view does not exist (DROP on first run)
# ORA-04043: object does not exist (DROP VIEW on first run if 19c)
# ORA-00955: name already used by an existing object (CREATE on re-run if
#            something failed mid-flight)
TOLERATED_ORACODES = {"ORA-00942", "ORA-04043", "ORA-00955"}


def split_statements(sql_text: str) -> list[tuple[str, str]]:
    """Split a SQL*Plus-style script into (kind, statement) tuples.

    `kind` is one of:
      - "sql"   : a regular SQL statement (DDL, DML, SELECT)
      - "plsql" : an anonymous PL/SQL block (terminated by '/')

    Comments are stripped. SQL*Plus directives are filtered out at the
    statement level, not here. Strings literals are respected — a `;`
    inside a quoted string does not split.
    """
    statements: list[tuple[str, str]] = []
    buf: list[str] = []
    in_plsql_block = False
    in_string = False
    string_delim: str | None = None

    lines = sql_text.splitlines()
    for raw_line in lines:
        line = raw_line.rstrip()

        # Strip full-line comments
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        if not stripped and not buf:
            continue

        # Detect start of a PL/SQL block. A line starting with BEGIN or
        # DECLARE (case-insensitive) flips the parser into PL/SQL mode
        # until a `/` line.
        upper_stripped = stripped.upper()
        if not in_plsql_block and not buf and (
            upper_stripped.startswith("BEGIN")
            or upper_stripped.startswith("DECLARE")
            or upper_stripped.startswith("CREATE OR REPLACE PROCEDURE")
            or upper_stripped.startswith("CREATE OR REPLACE FUNCTION")
            or upper_stripped.startswith("CREATE OR REPLACE PACKAGE")
            or upper_stripped.startswith("CREATE OR REPLACE TRIGGER")
        ):
            in_plsql_block = True

        # PL/SQL block terminator: a line consisting only of '/'
        if in_plsql_block and stripped == "/":
            stmt = "\n".join(buf).strip()
            if stmt:
                statements.append(("plsql", stmt))
            buf = []
            in_plsql_block = False
            continue

        if in_plsql_block:
            buf.append(line)
            continue

        # Plain-SQL mode. Walk the line character by character to honor
        # string literals when looking for the terminating ';'.
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                if ch == string_delim:
                    # Check for doubled quote (escape inside string)
                    if i + 1 < len(line) and line[i + 1] == string_delim:
                        buf.append(ch)
                        buf.append(line[i + 1])
                        i += 2
                        continue
                    in_string = False
                    string_delim = None
                buf.append(ch)
                i += 1
                continue

            if ch in ("'", '"'):
                in_string = True
                string_delim = ch
                buf.append(ch)
                i += 1
                continue

            if ch == ";":
                # End of plain-SQL statement
                buf.append(ch)
                stmt = "".join(buf).strip().rstrip(";").strip()
                if stmt:
                    statements.append(("sql", stmt))
                buf = []
                i += 1
                continue

            buf.append(ch)
            i += 1
        buf.append("\n")

    # Anything left in buf at EOF
    leftover = "".join(buf).strip()
    if leftover:
        if in_plsql_block:
            statements.append(("plsql", leftover))
        else:
            statements.append(("sql", leftover.rstrip(";")))

    return statements


def execute_statement(cursor, kind: str, sql: str) -> tuple[bool, str]:
    """Execute one statement. Return (ok, message). Tolerates known errors."""
    try:
        if kind == "plsql":
            cursor.execute(sql)
        else:
            cursor.execute(sql)
            # If it was a SELECT, fetch the results for the user
            if cursor.description is not None:
                cols = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
                pretty = "\n".join(
                    [
                        " | ".join(str(v) for v in r)
                        for r in [tuple(cols)] + rows
                    ]
                )
                return True, f"SELECT returned {len(rows)} rows:\n{pretty}"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        for code in TOLERATED_ORACODES:
            if code in msg:
                return True, f"tolerated {code}"
        return False, msg


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Load seed SQL against Oracle.")
    parser.add_argument(
        "script",
        nargs="?",
        default=str(REPO_ROOT / "examples" / "seed-demo.sql"),
        help="path to .sql file to load (default: examples/seed-demo.sql)",
    )
    args = parser.parse_args(argv)

    script_path = Path(args.script)
    if not script_path.exists():
        print(f"ERROR: {script_path} not found", file=sys.stderr)
        return 2

    print(f"Loading {script_path} ...")
    text = script_path.read_text()
    statements = split_statements(text)
    print(f"Parsed {len(statements)} statements.\n")

    start = time.time()
    pool = get_pool()
    ok_count = 0
    err_count = 0

    with pool.connection() as conn:
        cursor = conn.cursor()

        for i, (kind, stmt) in enumerate(statements, start=1):
            first_line = stmt.splitlines()[0][:70].strip()

            # Skip SQL*Plus directives at the statement level
            if kind == "sql" and SQLPLUS_DIRECTIVE.match(first_line):
                print(f"[{i}/{len(statements)}] SKIP (directive): {first_line}")
                continue

            ok, msg = execute_statement(cursor, kind, stmt)
            status = "OK  " if ok else "ERR "
            tag = "plsql" if kind == "plsql" else "sql  "
            extra = f"  ({msg})" if msg else ""
            print(f"[{i:>3}/{len(statements)}] {status} {tag} : {first_line}{extra}")
            if ok:
                ok_count += 1
            else:
                err_count += 1

        conn.commit()

    elapsed = time.time() - start
    print()
    print(f"Done in {elapsed:.1f}s — {ok_count} ok, {err_count} errors.")
    return 0 if err_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
