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

# DROP / TRUNCATE / ALTER are destructive enough that we require a
# stronger user-side confirmation than just 'yes'. The user must type
# the target object name (e.g., 'orders' for DROP TABLE orders) OR the
# explicit phrase 'I understand'. This blocks the failure mode where a
# user reflexively says 'yes' to a proposal they didn't fully read.
_DESTRUCTIVE_OPS = re.compile(r"\b(DROP|TRUNCATE|ALTER)\b", re.IGNORECASE)

# Pulls the first object identifier out of a DROP/TRUNCATE/ALTER
# statement. Handles optional `schema.` qualifier. Returns lowercase
# name or None if the parse can't locate it (in which case the user
# falls back to the 'I understand' acknowledgment phrase).
_TARGET_OBJECT_RE = re.compile(
    r"\b(?:DROP|TRUNCATE|ALTER)\s+"
    r"(?:TABLE|INDEX|VIEW|SEQUENCE|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|"
    r"SYNONYM|TYPE|USER|TABLESPACE|MATERIALIZED\s+VIEW|SYSTEM|SESSION)\s+"
    r"(?:[a-zA-Z_][a-zA-Z0-9_$#]*\.)?"
    r"([a-zA-Z_][a-zA-Z0-9_$#]*)",
    re.IGNORECASE,
)


def _extract_target_object(sql: str) -> str | None:
    m = _TARGET_OBJECT_RE.search(sql)
    return m.group(1).lower() if m else None


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


def _validate_confirmation(token: str | None, sql: str) -> tuple[bool, str | None]:
    """Two-tier confirmation check.

    - Non-destructive writes (INSERT/UPDATE/DELETE/MERGE/CREATE/GRANT etc.)
      require the user's literal 'yes' or an equivalent from the small
      affirmative allowlist.
    - DESTRUCTIVE writes (DROP/TRUNCATE/ALTER) additionally require the
      user to type the target object's name (e.g. 'orders' for
      DROP TABLE orders) OR the explicit phrase 'I understand'. This
      blocks the reflexive 'yes' to a proposal that wasn't fully read.

    Returns (is_valid, error_message_if_invalid).
    """
    token_lower = (token or "").strip().lower()

    if not _DESTRUCTIVE_OPS.search(sql):
        if _is_affirmative(token):
            return True, None
        return False, (
            "user_confirmation_token must be the user's literal 'yes' "
            "(or equivalent affirmative). The calling skill must collect "
            "explicit consent in the immediately prior turn before "
            "invoking this tool."
        )

    # Destructive path. 'I understand' is the universal acknowledgment.
    if "i understand" in token_lower:
        return True, None

    target = _extract_target_object(sql)
    if target and target in token_lower:
        return True, None

    expected_hint = target if target else "the target object's name"
    return False, (
        f"This statement is destructive (DROP/TRUNCATE/ALTER), so a "
        f"plain 'yes' is not enough. The user must type the target "
        f"object's name ('{expected_hint}') or the literal phrase "
        f"'I understand' as the confirmation token. This intentionally "
        f"adds friction to prevent reflexive approval of a proposal the "
        f"user did not fully read."
    )


def _handle_write_with_confirmation(args: dict, **kwargs) -> str:
    sql = (args.get("sql") or "").strip().rstrip(";")
    binds = args.get("binds") or {}
    token = args.get("user_confirmation_token")
    reason = (args.get("reason") or "").strip()

    if not sql:
        return json.dumps({"error": "sql is required"})
    if not reason:
        return json.dumps({"error": "reason is required for the audit log"})

    valid, err = _validate_confirmation(token, sql)
    if not valid:
        return json.dumps({"error": err})

    try:
        pool = get_pool()
        with pool.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, binds)
            affected = cursor.rowcount
            conn.commit()
            _audit_event("approved", sql=sql, binds=binds, token=token,
                         reason=reason, rows_affected=affected)
            return json.dumps({
                "ok": True,
                "rows_affected": affected,
                "committed": True,
                "reason": reason,
            })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc), "reason": reason})


# --- oracle_record_denial --------------------------------------------------
#
# Logs a proposal the agent made that the user rejected. The other half of
# the audit story: today we know every write that ran; with this we also
# know every write the agent suggested and the user vetoed, which is gold
# for tuning the agent's judgment over time. The skills layer is responsible
# for calling this tool whenever a user replies 'no' (or any negative) to a
# write proposal — same convention as collecting 'yes' for the confirmation
# path.

ORACLE_RECORD_DENIAL_SCHEMA = {
    "name": "oracle_record_denial",
    "description": (
        "Log a user's rejection of a proposed write (DDL/DML) to the audit "
        "trail. Call this when the user replies 'no' (or any negative) to a "
        "proposal that would otherwise have gone through "
        "oracle_write_with_confirmation. Together with the approval log, "
        "this gives the user a record of EVERY decision point: what was "
        "proposed, what was approved, and what was rejected — useful for "
        "tuning the agent's judgment over time. Does NOT execute any SQL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "proposed_sql": {
                "type": "string",
                "description": "The SQL the agent proposed but did not execute.",
            },
            "user_response": {
                "type": "string",
                "description": (
                    "The user's literal denial response (e.g. 'no', "
                    "'not now', 'show alternatives')."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Why the proposal was rejected, in the user's words or "
                    "the agent's best interpretation. Used to improve the "
                    "agent's future suggestions for the same situation."
                ),
            },
        },
        "required": ["proposed_sql", "user_response", "reason"],
    },
}


def _handle_record_denial(args: dict, **kwargs) -> str:
    proposed_sql = (args.get("proposed_sql") or "").strip().rstrip(";")
    user_response = (args.get("user_response") or "").strip()
    reason = (args.get("reason") or "").strip()

    if not proposed_sql:
        return json.dumps({"error": "proposed_sql is required"})
    if not user_response:
        return json.dumps({"error": "user_response is required"})
    if not reason:
        return json.dumps({"error": "reason is required"})

    try:
        _audit_event("denied", proposed_sql=proposed_sql,
                     user_response=user_response, reason=reason)
        return json.dumps({
            "ok": True,
            "logged": True,
            "event": "denied",
        })
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def _audit_event(event: str, **fields) -> None:
    """Append-only audit log capturing every approved write AND every
    denied proposal. Survives plugin upgrades because it lives outside
    the plugin directory.

    Two event shapes share one log file (writes.jsonl):

    event=approved: {ts, event, user, dsn, sql, binds,
                     user_confirmation_token, reason, rows_affected}
    event=denied:   {ts, event, user, dsn, proposed_sql,
                     user_response, reason}

    Both share the metadata (ts/user/dsn/reason) so a tail of the file
    gives the full decision history. Use `jq 'select(.event=="approved")'`
    to filter to executions; `select(.event=="denied")` for vetoes.
    """
    log_dir = pathlib.Path.home() / ".hermes" / "oracleops"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "writes.jsonl"

    base = {
        "event": event,
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "user": os.environ.get("ORACLE_USER", "?"),
        "dsn": os.environ.get("ORACLE_DSN", "?"),
    }

    # Normalize bind values (datetimes etc) for JSON serialization
    if "binds" in fields and fields["binds"]:
        fields["binds"] = {k: _to_jsonable(v) for k, v in fields["binds"].items()}

    entry = {**base, **fields}
    with log_file.open("a") as f:
        f.write(json.dumps(entry) + "\n")
