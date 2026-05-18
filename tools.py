"""Tool definitions exposed to Hermes Agent via the OracleOps plugin.

Each tool follows Hermes' built-in pattern (see ``plugins/spotify/tools.py``
in the Hermes repo for the canonical template):

- A SCHEMA dict defining the JSON Schema parameters for the LLM
- A ``_handle_<tool>`` function that accepts ``(args: dict, **kwargs)``
  and returns a JSON-encoded string
- A ``_check_oracle_available`` function that returns True when the
  tool can be dispatched (env vars present)

Handlers NEVER raise — they catch and return ``json.dumps({"error": ...})``.

Read-only tools (run_select / describe_table / explain_plan / display_
cursor_plan) auto-run. The write tool ``oracle_write_with_confirmation``
requires the calling skill to populate ``user_confirmation_token`` with
the user's literal "yes" response BEFORE invocation — the skill is
responsible for collecting consent; this module audits what got run.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
from typing import Any

from .connection import credentials_available, get_pool


# --- Availability check -----------------------------------------------------


def _check_oracle_available() -> bool:
    """Lightweight env-var check. Doesn't connect — that would hang
    plugin loading on a network round-trip. Real connection failures
    surface inside the handlers with clear error messages.
    """
    return credentials_available()


# --- Read-only path ---------------------------------------------------------

# Anything matching this is rejected before the server sees it. The skills
# layer is the primary contract that this is read-only; the deny-list is a
# defense-in-depth safety net so the agent can't mutate state by accident
# via the read-only tool.
_FORBIDDEN_IN_READ = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|GRANT|"
    r"REVOKE|COMMIT|ROLLBACK|SAVEPOINT)\b",
    re.IGNORECASE,
)


def _to_jsonable(value: Any) -> Any:
    """Coerce Oracle row values into JSON-serializable shapes."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary len={len(value)}>"
    return value


# --- oracle_run_select ------------------------------------------------------

ORACLE_RUN_SELECT_SCHEMA = {
    "name": "oracle_run_select",
    "description": (
        "Execute a read-only SELECT statement against the configured Oracle "
        "database and return rows as JSON. Refuses any statement containing "
        "INSERT/UPDATE/DELETE/DDL keywords — use oracle_write_with_confirmation "
        "for mutations. Bind parameters via the optional `binds` object."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SELECT statement to execute. No trailing semicolon.",
            },
            "binds": {
                "type": "object",
                "description": "Optional bind variables as a name→value mapping.",
                "additionalProperties": True,
            },
            "fetch_limit": {
                "type": "integer",
                "description": "Maximum number of rows to return (default 100, max 1000).",
                "default": 100,
                "minimum": 1,
                "maximum": 1000,
            },
        },
        "required": ["sql"],
    },
}


def _handle_run_select(args: dict, **kwargs) -> str:
    sql = (args.get("sql") or "").strip().rstrip(";")
    binds = args.get("binds") or {}
    fetch_limit = min(int(args.get("fetch_limit") or 100), 1000)

    if not sql:
        return json.dumps({"error": "sql is required"})
    if _FORBIDDEN_IN_READ.search(sql):
        return json.dumps({
            "error": (
                "oracle_run_select refuses statements containing INSERT/UPDATE/"
                "DELETE/DDL keywords. Use oracle_write_with_confirmation for "
                "mutations after collecting explicit user 'yes' confirmation."
            )
        })

    try:
        pool = get_pool()
        with pool.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, binds)
            if cursor.description is None:
                return json.dumps({"error": "statement did not return rows"})
            columns = [c[0].lower() for c in cursor.description]
            rows: list[dict] = []
            for i, raw in enumerate(cursor):
                if i >= fetch_limit:
                    break
                rows.append({c: _to_jsonable(v) for c, v in zip(columns, raw)})
            return json.dumps({
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": len(rows) == fetch_limit,
            })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


# --- oracle_describe_table --------------------------------------------------

ORACLE_DESCRIBE_TABLE_SCHEMA = {
    "name": "oracle_describe_table",
    "description": (
        "Return the columns, indexes, and statistics for an Oracle table. "
        "Use to understand a table's structure before writing SQL against it, "
        "or to check whether stats are fresh."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {
                "type": "string",
                "description": "Schema owner (e.g. 'ADMIN', 'SCOTT'). Case-insensitive.",
            },
            "table_name": {
                "type": "string",
                "description": "Table name. Case-insensitive.",
            },
        },
        "required": ["owner", "table_name"],
    },
}


def _handle_describe_table(args: dict, **kwargs) -> str:
    owner = (args.get("owner") or "").upper()
    table_name = (args.get("table_name") or "").upper()
    if not owner or not table_name:
        return json.dumps({"error": "owner and table_name are required"})

    try:
        pool = get_pool()
        with pool.connection() as conn:
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT column_name, data_type, data_length, nullable, data_default
                  FROM all_tab_columns
                 WHERE owner = :o AND table_name = :t
                 ORDER BY column_id
                """,
                {"o": owner, "t": table_name},
            )
            columns = [
                {
                    "name": r[0],
                    "type": r[1],
                    "length": r[2],
                    "nullable": r[3] == "Y",
                    "default": _to_jsonable(r[4]),
                }
                for r in cursor
            ]

            if not columns:
                return json.dumps({
                    "error": f"table {owner}.{table_name} does not exist or is not accessible"
                })

            cursor.execute(
                """
                SELECT i.index_name, i.uniqueness,
                       LISTAGG(c.column_name, ',') WITHIN GROUP (
                           ORDER BY c.column_position) AS cols
                  FROM all_indexes i
                  JOIN all_ind_columns c
                    ON c.index_owner = i.owner
                   AND c.index_name = i.index_name
                 WHERE i.table_owner = :o AND i.table_name = :t
                 GROUP BY i.index_name, i.uniqueness
                """,
                {"o": owner, "t": table_name},
            )
            indexes = [
                {"name": r[0], "unique": r[1] == "UNIQUE", "columns": r[2]}
                for r in cursor
            ]

            cursor.execute(
                """
                SELECT num_rows, last_analyzed, sample_size
                  FROM all_tab_statistics
                 WHERE owner = :o AND table_name = :t
                """,
                {"o": owner, "t": table_name},
            )
            stats_row = cursor.fetchone()
            stats = (
                {
                    "num_rows": stats_row[0],
                    "last_analyzed": _to_jsonable(stats_row[1]),
                    "sample_size": stats_row[2],
                }
                if stats_row
                else None
            )

            return json.dumps({
                "owner": owner,
                "table": table_name,
                "columns": columns,
                "indexes": indexes,
                "statistics": stats,
            })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


# --- oracle_explain_plan ----------------------------------------------------

ORACLE_EXPLAIN_PLAN_SCHEMA = {
    "name": "oracle_explain_plan",
    "description": (
        "Run EXPLAIN PLAN on a SQL statement and return the optimizer's "
        "predicted plan. Note: this is the optimizer's GUESS under default "
        "binds; for the real runtime plan, use oracle_display_cursor_plan "
        "with a SQL_ID instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SELECT statement to explain. No trailing semicolon.",
            },
        },
        "required": ["sql"],
    },
}


def _handle_explain_plan(args: dict, **kwargs) -> str:
    sql = (args.get("sql") or "").strip().rstrip(";")
    if not sql:
        return json.dumps({"error": "sql is required"})
    if _FORBIDDEN_IN_READ.search(sql):
        return json.dumps({"error": "oracle_explain_plan refuses non-SELECT statements"})

    try:
        pool = get_pool()
        with pool.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"EXPLAIN PLAN FOR {sql}")
            cursor.execute(
                "SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'ALL'))"
            )
            plan_lines = [r[0] for r in cursor]
            return json.dumps({"plan": "\n".join(plan_lines)})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


# --- oracle_display_cursor_plan --------------------------------------------

ORACLE_DISPLAY_CURSOR_PLAN_SCHEMA = {
    "name": "oracle_display_cursor_plan",
    "description": (
        "Pull the REAL runtime execution plan for a given SQL_ID from the "
        "cursor cache, including actual row counts, peeked bind values, and "
        "wait events. Far more useful than oracle_explain_plan for tuning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql_id": {
                "type": "string",
                "description": "The 13-character SQL_ID (e.g. '4a2g8htg9k7bn').",
            },
            "child_number": {
                "type": "integer",
                "description": "Optional child cursor number. Omit for default.",
            },
        },
        "required": ["sql_id"],
    },
}


def _handle_display_cursor_plan(args: dict, **kwargs) -> str:
    sql_id = (args.get("sql_id") or "").strip()
    child_number = args.get("child_number")
    if not sql_id:
        return json.dumps({"error": "sql_id is required"})

    try:
        pool = get_pool()
        with pool.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT plan_table_output
                  FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :cn,
                            'ALLSTATS LAST +PEEKED_BINDS +OUTLINE'))
                """,
                {"sql_id": sql_id, "cn": child_number},
            )
            plan_lines = [r[0] for r in cursor]
            if not plan_lines:
                return json.dumps({
                    "error": (
                        f"no plan in cursor cache for SQL_ID '{sql_id}'. "
                        f"The cursor may have aged out. Try AWR via "
                        f"DBMS_XPLAN.DISPLAY_AWR if Diagnostic Pack is licensed."
                    )
                })
            return json.dumps({"sql_id": sql_id, "plan": "\n".join(plan_lines)})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


# --- oracle_write_with_confirmation -----------------------------------------

ORACLE_WRITE_WITH_CONFIRMATION_SCHEMA = {
    "name": "oracle_write_with_confirmation",
    "description": (
        "Execute a write-side SQL statement (DDL, DML, ALTER SYSTEM) AFTER "
        "the calling skill has collected an explicit user 'yes' confirmation "
        "in the immediately prior chat turn. The user's literal response "
        "must be passed as `user_confirmation_token`. Every call is appended "
        "to an audit log at ~/.hermes/oracleops/writes.jsonl. "
        "DO NOT CALL without explicit prior consent — the skill is the "
        "primary contract; this audit trail is the safety net."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL to execute. Single statement, no trailing semicolon.",
            },
            "binds": {
                "type": "object",
                "description": "Optional bind variables as a name→value mapping.",
                "additionalProperties": True,
            },
            "user_confirmation_token": {
                "type": "string",
                "description": (
                    "The user's literal 'yes' (or similar affirmative) "
                    "response collected by the calling skill in the prior turn."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One-sentence justification for the write, captured in "
                    "the audit log. E.g., 'kill orphaned session 1247 blocking "
                    "23 others'."
                ),
            },
        },
        "required": ["sql", "user_confirmation_token", "reason"],
    },
}


def _is_affirmative(token: str | None) -> bool:
    if not token:
        return False
    t = token.strip().lower()
    return t in {"yes", "y", "confirm", "proceed", "ok", "do it", "kill it",
                 "go ahead", "yes, do it", "yes do it"}


def _handle_write_with_confirmation(args: dict, **kwargs) -> str:
    sql = (args.get("sql") or "").strip().rstrip(";")
    binds = args.get("binds") or {}
    token = args.get("user_confirmation_token")
    reason = (args.get("reason") or "").strip()

    if not sql:
        return json.dumps({"error": "sql is required"})
    if not reason:
        return json.dumps({"error": "reason is required for the audit log"})
    if not _is_affirmative(token):
        return json.dumps({
            "error": (
                "user_confirmation_token must be the user's literal 'yes' "
                "(or equivalent affirmative). The calling skill must collect "
                "explicit consent in the immediately prior turn before "
                "invoking this tool."
            )
        })

    try:
        pool = get_pool()
        with pool.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, binds)
            affected = cursor.rowcount
            conn.commit()
            _audit_write(sql, binds, token, reason, affected)
            return json.dumps({
                "ok": True,
                "rows_affected": affected,
                "committed": True,
                "reason": reason,
            })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc), "reason": reason})


def _audit_write(sql: str, binds: dict | None, token: str,
                 reason: str, affected: int) -> None:
    """Append-only audit log of every executed write. Survives plugin
    upgrades because it lives outside the plugin directory.
    """
    log_dir = pathlib.Path.home() / ".hermes" / "oracleops"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "writes.jsonl"

    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "user": os.environ.get("ORACLE_USER", "?"),
        "dsn": os.environ.get("ORACLE_DSN", "?"),
        "sql": sql,
        "binds": {k: _to_jsonable(v) for k, v in (binds or {}).items()},
        "user_confirmation_token": token,
        "reason": reason,
        "rows_affected": affected,
    }
    with log_file.open("a") as f:
        f.write(json.dumps(entry) + "\n")
