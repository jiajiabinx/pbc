"""Backend-agnostic DB layer: SQLite for local/tests, Postgres for prod.

The backend is chosen from the connection string:
  * a ``postgresql://`` (or ``postgres://``) URL  -> Postgres via psycopg
  * anything else                                 -> a SQLite file path

Every table name is transparently prefixed (default ``pbc_``) so this app can
share a single Postgres instance with other apps without clashing. The prefix is
applied centrally here, so all the raw SQL scattered across the codebase keeps
using the bare table names (``items``, ``trace``, …) and still targets the
prefixed tables (``pbc_items``, ``pbc_trace``, …) on both backends.

The wrapper also papers over the two dialects the app relies on:
  * ``?`` placeholders are rewritten to ``%s`` for Postgres (and literal ``%``
    escaped) so store code stays in the SQLite ``?`` style.
  * ``execute_returning`` returns a new row id from either ``cur.lastrowid``
    (SQLite) or an appended ``RETURNING`` clause (Postgres).
  * rows support both ``row["col"]`` and ``row[0]`` access plus ``.keys()`` /
    ``dict(row)`` on either backend.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

# psycopg is only needed when actually connecting to Postgres; keep it optional
# so local dev and the test suite run on SQLite with no extra install.
try:  # pragma: no cover - import guard
    import psycopg
    import psycopg.errors as _pg_errors

    _PG_ERRORS: tuple = (psycopg.Error,)
    _PG_INTEGRITY: tuple = (_pg_errors.IntegrityError,)
except Exception:  # psycopg not installed
    psycopg = None  # type: ignore[assignment]
    _PG_ERRORS = ()
    _PG_INTEGRITY = ()

# Table-name prefix used to namespace this app inside a shared Postgres DB.
PREFIX = os.environ.get("PBC_TABLE_PREFIX", "pbc_")

# Exception tuples callers can `except` regardless of backend.
Error: tuple = (sqlite3.Error,) + _PG_ERRORS
IntegrityError: tuple = (sqlite3.IntegrityError,) + _PG_INTEGRITY

# Every table the app owns. Longer / underscored names first so the alternation
# never leaves a stray unprefixed fragment (word boundaries already prevent
# matching inside `item_reviews`, `api_calls_snapshot`, `n_episodes`, …).
_TABLES = (
    "item_reviews", "api_calls", "run_history", "clarifications",
    "verifications", "documents", "episodes", "drafts", "emails",
    "items", "trace", "users", "meta",
)
_TABLE_RE = re.compile(r"\b(" + "|".join(_TABLES) + r")\b")


def is_pg(dsn: str) -> bool:
    return dsn.startswith(("postgresql://", "postgres://"))


def _apply_prefix(sql: str) -> str:
    """Prefix bare table names. Word boundaries mean already-prefixed names
    (`pbc_items`) and columns/aliases sharing a stem (`item_id`, `n_episodes`)
    are left untouched, so this is safe to run over every statement."""
    if not PREFIX:
        return sql
    return _TABLE_RE.sub(lambda m: PREFIX + m.group(1), sql)


def _pg_prepare(sql: str, params) -> str:
    sql = _apply_prefix(sql)
    if params:  # only rewrite when psycopg will parse the query for placeholders
        sql = sql.replace("%", "%%").replace("?", "%s")
    return sql


class _PgRow(dict):
    """dict (gives ``row["col"]``, ``.keys()``, ``dict(row)``) plus positional
    ``row[0]`` access, matching what the codebase expects from sqlite3.Row."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._seq = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._seq[key]
        return super().__getitem__(key)


def _pg_row_factory(cursor):
    cols = [d.name for d in cursor.description] if cursor.description else []

    def make(values):
        return _PgRow(cols, values)

    return make


def _split_statements(script: str) -> list[str]:
    # The schema has no ';' inside string literals or comments, so a plain split
    # is safe and keeps Postgres (which rejects multi-statement execute) happy.
    return [s for s in (part.strip() for part in script.split(";")) if s]


class Conn:
    """Thin connection wrapper exposing the SQLite-flavoured API the app uses."""

    def __init__(self, raw, backend: str):
        self._raw = raw
        self.backend = backend

    def execute(self, sql: str, params=()):
        if self.backend == "pg":
            cur = self._raw.cursor()
            cur.execute(_pg_prepare(sql, params), tuple(params) if params else None)
            return cur
        return self._raw.execute(_apply_prefix(sql), params)

    def execute_returning(self, sql: str, params=(), *, column: str):
        """Run an INSERT and return the generated id (lastrowid on SQLite, an
        appended ``RETURNING <column>`` on Postgres)."""
        if self.backend == "pg":
            q = _pg_prepare(sql, params)
            if "returning" not in q.lower():
                q = q.rstrip().rstrip(";") + f" RETURNING {column}"
            cur = self._raw.cursor()
            cur.execute(q, tuple(params) if params else None)
            row = cur.fetchone()
            return row[0] if row is not None else None
        return self._raw.execute(_apply_prefix(sql), params).lastrowid

    def executescript(self, script: str):
        script = _apply_prefix(script)
        if self.backend == "pg":
            for stmt in _split_statements(script):
                with self._raw.cursor() as cur:
                    cur.execute(stmt)
            return
        self._raw.executescript(script)

    def commit(self):
        # No-op under Postgres autocommit, real commit under SQLite.
        try:
            self._raw.commit()
        except Exception:
            pass

    def close(self):
        self._raw.close()

    @property
    def raw(self):
        return self._raw


def connect(dsn: str) -> Conn:
    if is_pg(dsn):
        if psycopg is None:
            raise RuntimeError(
                "A Postgres DATABASE_URL was given but psycopg is not installed. "
                "Install it with: pip install 'psycopg[binary]'")
        raw = psycopg.connect(dsn, autocommit=True, row_factory=_pg_row_factory)
        return Conn(raw, "pg")

    # SQLite file path.
    parent = Path(dsn).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: Streamlit caches the Store across reruns that
    # execute on different threads.
    raw = sqlite3.connect(dsn, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    # Two processes share this DB (the agent runner and the Streamlit UI).
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=5000")
    return Conn(raw, "sqlite")
