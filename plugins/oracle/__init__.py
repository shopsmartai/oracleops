"""OracleOps — Hermes Agent plugin for Oracle Database 23ai.

Exposes a `oracle_db` toolset to the agent. The toolset provides safe,
read-only access by default (`run_select`, `describe_table`, `explain_plan`,
AWR views) and a separate write path that requires explicit confirmation
gates from the agent's response loop.

Loaded by Hermes via the standard plugin contract: drop this package into
`~/.hermes/plugins/oracle/` and the agent's plugin manager picks it up.
"""

from .connection import OraclePool, get_pool
from .tools import register_tools

__all__ = ["OraclePool", "get_pool", "register_tools"]
__version__ = "0.1.0"
