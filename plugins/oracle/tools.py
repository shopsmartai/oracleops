"""Tool definitions exposed to Hermes Agent.

Every tool here is what an OracleOps skill calls. The split between
read-only and write paths matters: read-only tools auto-run; write
tools must be wrapped in the agent's confirmation gate before they
execute.

Hermes' plugin contract expects a `register_tools(registry)` function
that registers each tool with the agent's tool router. The exact
registry API is in flux across Hermes versions, so this module is
written to be import-clean — the actual registration happens when
Hermes calls `register_tools`. If the registry signature changes,
update this file's `register_tools` function only.
"""

from __future__ import annotations

import re
from typing import Any

from .connection import get_pool


# --- Read-only path ---------------------------------------------------------

# A conservative deny-list. Anything matching these is rejected before the
# server sees it. We trust the agent to use the *write_* helpers when it
# genuinely needs to mutate state — see the write-path section below.
FORBIDDEN_IN_READ = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|GRANT|"
    r"REVOKE|COMMIT|ROLLBACK|SAVEPOINT)\b",
    re.IGNORECASE,
)


def run_select(sql: str, binds: dict[str, Any] | None = None,
               fetch_limit: int = 100) -> list[dict]:
    """Execute a SELECT and return rows as a list of dicts.

    The deny-list is a safety net; the skills layer is the primary
    contract that this is read-only. We never grant the connection
    user any privilege beyond SELECT on the target schemas — DBA
    grants come from a separate, confirmation-gated path.
    """
    if FORBIDDEN_IN_READ.search(sql):
        raise PermissionError(
            "run_select refuses to execute this statement because it "
            "contains a write keyword. Use write_with_confirmation for "
            "actual mutations."
        )

    pool = get_pool()
    with pool.connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, binds or {})
        columns = [c[0].lower() for c in cursor.description]
        rows = []
        for i, raw in enumerate(cursor):
            if i >= fetch_limit:
                break
            rows.append(dict(zip(columns, raw)))
        return rows


def describe_table(owner: str, table_name: str) -> dict:
    """Return columns + indexes + last-analyzed for a table."""
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
            {"o": owner.upper(), "t": table_name.upper()},
        )
        columns = [
            {
                "name": r[0],
                "type": r[1],
                "length": r[2],
                "nullable": r[3] == "Y",
                "default": r[4],
            }
            for r in cursor
        ]

        cursor.execute(
            """
            SELECT i.index_name, i.uniqueness, LISTAGG(c.column_name, ',')
                     WITHIN GROUP (ORDER BY c.column_position) AS cols
              FROM all_indexes i
              JOIN all_ind_columns c
                ON c.index_owner = i.owner
               AND c.index_name = i.index_name
             WHERE i.table_owner = :o AND i.table_name = :t
             GROUP BY i.index_name, i.uniqueness
            """,
            {"o": owner.upper(), "t": table_name.upper()},
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
            {"o": owner.upper(), "t": table_name.upper()},
        )
        stats_row = cursor.fetchone()
        stats = (
            {
                "num_rows": stats_row[0],
                "last_analyzed": str(stats_row[1]) if stats_row[1] else None,
                "sample_size": stats_row[2],
            }
            if stats_row
            else None
        )

        return {
            "owner": owner,
            "table": table_name,
            "columns": columns,
            "indexes": indexes,
            "statistics": stats,
        }


def explain_plan(sql: str) -> list[dict]:
    """Run EXPLAIN PLAN and return the plan rows.

    Note: This is the optimizer's guess under default binds. For real
    runtime plans, use display_cursor_plan(sql_id) instead.
    """
    if FORBIDDEN_IN_READ.search(sql):
        raise PermissionError("explain_plan refuses non-SELECT statements.")

    pool = get_pool()
    with pool.connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"EXPLAIN PLAN FOR {sql}")
        cursor.execute(
            "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY(NULL, NULL, 'ALL'))"
        )
        return [{"line": r[0]} for r in cursor]


def display_cursor_plan(sql_id: str, child_number: int | None = None) -> list[dict]:
    """Pull the actual runtime plan from the cursor cache."""
    pool = get_pool()
    with pool.connection() as conn:
        cursor = conn.cursor()
        cn = child_number if child_number is not None else None
        cursor.execute(
            """
            SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR(:sql_id, :cn,
                          'ALLSTATS LAST +PEEKED_BINDS +OUTLINE'))
            """,
            {"sql_id": sql_id, "cn": cn},
        )
        return [{"line": r[0]} for r in cursor]


# --- Write path (CONFIRMATION REQUIRED at the agent layer) ------------------

# Skills that call write_with_confirmation MUST first surface the SQL to
# the user, get an explicit "yes", and only then call this helper. The
# helper itself does not enforce confirmation — that is the skill's job.
# Two-layer defense: the skill asks, this function logs.


def write_with_confirmation(sql: str, binds: dict[str, Any] | None = None,
                            user_confirmation_token: str | None = None) -> dict:
    """Execute a write-side statement after the agent collected explicit
    user confirmation.

    `user_confirmation_token` is an opaque string the calling skill must
    pass through, populated from the user's "yes" response. It is logged
    alongside the executed SQL so that every mutation has an audit trail.

    Returns row count and any output. Commits implicitly — the agent
    is responsible for getting confirmation BEFORE calling.
    """
    if not user_confirmation_token:
        raise PermissionError(
            "write_with_confirmation requires user_confirmation_token. "
            "The calling skill must collect explicit user 'yes' first."
        )

    pool = get_pool()
    with pool.connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, binds or {})
        affected = cursor.rowcount
        conn.commit()
        _audit_write(sql, binds, user_confirmation_token, affected)
        return {"rows_affected": affected, "committed": True}


def _audit_write(sql: str, binds: dict | None, token: str, affected: int) -> None:
    """Append a write to the local audit log. The log is for the human,
    not the agent. Stored next to the Hermes state DB.
    """
    import datetime
    import json
    import pathlib

    log_dir = pathlib.Path.home() / ".hermes" / "oracleops"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "writes.jsonl"

    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "sql": sql,
        "binds": binds,
        "user_confirmation_token": token,
        "rows_affected": affected,
    }
    with log_file.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Registration -----------------------------------------------------------


def register_tools(registry: Any) -> None:
    """Called by Hermes when the plugin loads.

    Registers each tool function with the agent's tool registry. The
    exact registry API depends on Hermes version; treat this function
    as the integration point and update if the contract changes.
    """
    # The registry contract is being finalized in Hermes v0.14+; this
    # is the pattern from the example-plugins repo. If the upstream
    # contract changes, only this function needs updating — the tool
    # implementations above are independent of the registry shape.
    registry.add_tool(
        name="oracle.run_select",
        func=run_select,
        description="Run a SELECT against Oracle. Read-only.",
        toolset="oracle_db",
    )
    registry.add_tool(
        name="oracle.describe_table",
        func=describe_table,
        description="Describe an Oracle table: columns, indexes, stats.",
        toolset="oracle_db",
    )
    registry.add_tool(
        name="oracle.explain_plan",
        func=explain_plan,
        description="Run EXPLAIN PLAN on a SQL statement and return the plan.",
        toolset="oracle_db",
    )
    registry.add_tool(
        name="oracle.display_cursor_plan",
        func=display_cursor_plan,
        description="Pull the real runtime plan for a SQL_ID from the cursor cache.",
        toolset="oracle_db",
    )
    registry.add_tool(
        name="oracle.write_with_confirmation",
        func=write_with_confirmation,
        description=(
            "Execute a write-side SQL after the agent has collected explicit "
            "user confirmation. Pass the user's 'yes' as user_confirmation_token."
        ),
        toolset="oracle_db_write",
        requires_confirmation=True,
    )
