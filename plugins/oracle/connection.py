"""Thin connection pool for Oracle 23ai (including Autonomous Database).

Uses `oracledb` in thin mode (no Instant Client install required). For
mTLS-protected Autonomous Database, point `ORACLE_WALLET_DIR` at the
unzipped wallet directory and `oracledb` thin mode handles TLS.

Environment variables:
  ORACLE_CONNECTION_STRING  user/password@//host:port/service_name
                            or user/password@adb_high (for ADB tnsnames)
  ORACLE_WALLET_DIR         path to unzipped wallet directory (ADB only)
  ORACLE_POOL_MIN           connection pool minimum (default 1)
  ORACLE_POOL_MAX           connection pool maximum (default 4)
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

try:
    import oracledb  # type: ignore
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "OracleOps requires the `oracledb` package. "
        "Install with `pip install oracledb>=2.0`."
    ) from exc


class OraclePool:
    """A singleton-style connection pool with lazy initialization.

    Hermes plugins are loaded once per process; we want connection-pool
    state to survive across tool calls within the same agent turn, but
    not leak between Hermes restarts.
    """

    _instance: OraclePool | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._pool: oracledb.ConnectionPool | None = None

    def _ensure_pool(self) -> oracledb.ConnectionPool:
        if self._pool is not None:
            return self._pool

        dsn = os.environ.get("ORACLE_CONNECTION_STRING")
        if not dsn:
            raise RuntimeError(
                "ORACLE_CONNECTION_STRING is not set. "
                "Configure with: hermes config set ORACLE_CONNECTION_STRING "
                "\"user/pass@//host:1521/service\""
            )

        # Split user/password from the host portion. Supports either
        #   user/pass@//host:port/service
        # or
        #   user/pass@tns_alias  (ADB with tnsnames.ora)
        if "/" not in dsn or "@" not in dsn:
            raise RuntimeError(
                "ORACLE_CONNECTION_STRING must be in form "
                "'user/pass@host:port/service' or 'user/pass@tns_alias'"
            )

        creds, host_part = dsn.split("@", 1)
        user, password = creds.split("/", 1)

        wallet_dir = os.environ.get("ORACLE_WALLET_DIR")
        pool_min = int(os.environ.get("ORACLE_POOL_MIN", "1"))
        pool_max = int(os.environ.get("ORACLE_POOL_MAX", "4"))

        kwargs: dict = {
            "user": user,
            "password": password,
            "dsn": host_part,
            "min": pool_min,
            "max": pool_max,
            "increment": 1,
        }
        if wallet_dir:
            kwargs["config_dir"] = wallet_dir
            kwargs["wallet_location"] = wallet_dir
            kwargs["wallet_password"] = os.environ.get("ORACLE_WALLET_PASSWORD")

        self._pool = oracledb.create_pool(**kwargs)
        return self._pool

    @contextmanager
    def connection(self) -> Iterator[oracledb.Connection]:
        """Yield a pooled connection. Always check it back in on exit."""
        pool = self._ensure_pool()
        conn = pool.acquire()
        try:
            yield conn
        finally:
            pool.release(conn)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None


def get_pool() -> OraclePool:
    """Return the process-singleton pool, creating it if needed."""
    if OraclePool._instance is None:
        with OraclePool._lock:
            if OraclePool._instance is None:
                OraclePool._instance = OraclePool()
    return OraclePool._instance
