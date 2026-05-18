"""OracleOps — Hermes Agent plugin for Oracle Database 23ai/26ai DBAs.

Registers the ``oracle_db`` toolset with five tools:

- ``oracle_run_select`` — read-only SQL with a deny-list safety net
- ``oracle_describe_table`` — schema + indexes + stats for a table
- ``oracle_explain_plan`` — EXPLAIN PLAN for a SQL statement
- ``oracle_display_cursor_plan`` — real runtime plan for a SQL_ID
- ``oracle_write_with_confirmation`` — gated write path requiring a
  user_confirmation_token populated by the calling skill from the user's
  literal "yes" response

The skill pack in ``skills/`` (agentskills.io format) uses these tools.
Skills describe HOW to diagnose Oracle issues; the tools above are the
executable primitives.

Plugin contract follows ``plugins/spotify`` as the template — a flat
plugin directory with ``plugin.yaml``, ``__init__.py``, and ``tools.py``,
auto-loaded as ``kind: backend``.
"""

from __future__ import annotations

from .tools import (
    ORACLE_DESCRIBE_TABLE_SCHEMA,
    ORACLE_DISPLAY_CURSOR_PLAN_SCHEMA,
    ORACLE_EXPLAIN_PLAN_SCHEMA,
    ORACLE_RUN_SELECT_SCHEMA,
    ORACLE_WRITE_WITH_CONFIRMATION_SCHEMA,
    _check_oracle_available,
    _handle_describe_table,
    _handle_display_cursor_plan,
    _handle_explain_plan,
    _handle_run_select,
    _handle_write_with_confirmation,
)


_TOOLS = (
    ("oracle_run_select",              ORACLE_RUN_SELECT_SCHEMA,              _handle_run_select,              "🔍"),
    ("oracle_describe_table",          ORACLE_DESCRIBE_TABLE_SCHEMA,          _handle_describe_table,          "📋"),
    ("oracle_explain_plan",            ORACLE_EXPLAIN_PLAN_SCHEMA,            _handle_explain_plan,            "📊"),
    ("oracle_display_cursor_plan",     ORACLE_DISPLAY_CURSOR_PLAN_SCHEMA,     _handle_display_cursor_plan,     "📈"),
    ("oracle_write_with_confirmation", ORACLE_WRITE_WITH_CONFIRMATION_SCHEMA, _handle_write_with_confirmation, "⚠️"),
)


def register(ctx) -> None:
    """Called once by the Hermes plugin loader at startup.

    Registers all five Oracle tools into the ``oracle_db`` toolset. The
    check_fn ensures tools are listed in ``hermes tools`` even when
    Oracle creds aren't configured — dispatch fails clearly at call time
    with a "set ORACLE_USER..." message instead of the tool silently
    disappearing.
    """
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="oracle_db",
            schema=schema,
            handler=handler,
            check_fn=_check_oracle_available,
            emoji=emoji,
        )
