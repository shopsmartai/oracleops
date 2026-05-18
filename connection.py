"""Thin connection pool for Oracle 23ai/26ai (including Autonomous Database).

Uses ``oracledb`` in thin mode (no Instant Client install required). For
mTLS-protected Autonomous Database, point ``ORACLE_WALLET_DIR`` at the
unzipped wallet directory and oracledb thin mode handles TLS.

Two configuration styles supported. The separate-variables form is
preferred because it tolerates passwords containing /, @, #, !, or any
other character without escape gymnastics. The combined form is kept
for compatibility.

Preferred (separate variables):
  ORACLE_USER              database user (e.g. "admin")
  ORACLE_PASSWORD          password (any characters allowed)
  ORACLE_DSN               connection alias or easy-connect string

Fallback (combined string — only safe when password has no @ or /):
  ORACLE_CONNECTION_STRING user/password@dsn

Always required for Autonomous Database:
  ORACLE_WALLET_DIR        path to unzipped wallet directory
  ORACLE_WALLET_PASSWORD   password set when downloading the wallet

Optional:
  ORACLE_POOL_MIN          pool minimum (default 1)
  ORACLE_POOL_MAX          pool maximum (default 4)
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
    state to survive across tool calls within the same agent turn but
    not leak between Hermes restarts.
    """

    _instance: "OraclePool | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._pool: oracledb.ConnectionPool | None = None

    def _resolve_credentials(self) -> tuple[str, str, str]:
        """Resolve (user, password, dsn) from environment.

        Preferred form is the three separate variables ORACLE_USER /
        ORACLE_PASSWORD / ORACLE_DSN — the only form that survives
        passwords containing /, @, or # without escape pain.

        Fallback is ORACLE_CONNECTION_STRING in user/password@dsn form.
        We rsplit on @ (not split) so a password ending in @something
        is at least partially handled.
        """
        user = os.environ.get("ORACLE_USER")
        password = os.environ.get("ORACLE_PASSWORD")
        dsn = os.environ.get("ORACLE_DSN")

        if user and password and dsn:
            return user, password, dsn

        combined = os.environ.get("ORACLE_CONNECTION_STRING")
        if not combined:
            raise RuntimeError(
                "No Oracle credentials configured. Set either:\n"
                "  ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN  (recommended)\n"
                "or\n"
                "  ORACLE_CONNECTION_STRING=user/password@dsn  (only safe\n"
                "    when password contains no '/' or '@')"
            )

        if "@" not in combined or "/" not in combined:
            raise RuntimeError(
                "ORACLE_CONNECTION_STRING must be user/password@dsn. "
                "If your password contains '/' or '@', use the separate "
                "ORACLE_USER / ORACLE_PASSWORD / ORACLE_DSN variables instead."
            )

        creds, dsn = combined.rsplit("@", 1)
        if "/" not in creds:
            raise RuntimeError(
                "ORACLE_CONNECTION_STRING has no '/' before '@'. "
                "Expected user/password@dsn."
            )
        user, password = creds.split("/", 1)
        if not user or not password or not dsn:
            raise RuntimeError(
                "ORACLE_CONNECTION_STRING parsed to empty user, password, "
                "or dsn. Switch to the separate-variables form."
            )
        return user, password, dsn

    def _ensure_pool(self) -> oracledb.ConnectionPool:
        if self._pool is not None:
            return self._pool

        user, password, dsn = self._resolve_credentials()

        wallet_dir = os.environ.get("ORACLE_WALLET_DIR")
        pool_min = int(os.environ.get("ORACLE_POOL_MIN", "1"))
        pool_max = int(os.environ.get("ORACLE_POOL_MAX", "4"))

        kwargs: dict = {
            "user": user,
            "password": password,
            "dsn": dsn,
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


def credentials_available() -> bool:
    """Lightweight check that does NOT touch the network.

    Returns True if enough env vars exist to attempt a connection.
    Used by the Hermes plugin's ``check_fn`` so tools stay listed even
    when creds are missing — dispatch then fails at call time with a
    clear message instead of the tools silently disappearing.
    """
    if (
        os.environ.get("ORACLE_USER")
        and os.environ.get("ORACLE_PASSWORD")
        and os.environ.get("ORACLE_DSN")
    ):
        return True
    combined = os.environ.get("ORACLE_CONNECTION_STRING") or ""
    return "@" in combined and "/" in combined
