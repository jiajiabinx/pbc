"""Database persistence: tracker rows, document lineage, full agent traces, cost ledger.

Supports both SQLite (local development) and PostgreSQL (production/Railway).
Set DATABASE_URL env var for PostgreSQL, otherwise falls back to SQLite.

The status guard lives here, in code, not in a prompt: no item may move to
Received / Insufficient / Complete without a verifier verdict recorded for that
(item, document) pair.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

STATUSES = ("Not started", "Requested", "Under review", "Received", "Insufficient", "Complete")
# Statuses that require an independent verifier verdict on file.
GUARDED_STATUSES = {"Received", "Insufficient", "Complete"}

# PostgreSQL schema (uses SERIAL instead of AUTOINCREMENT, TEXT for all strings)
_SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS pbc_items (
    item_id TEXT PRIMARY KEY,
    category TEXT, priority TEXT, description TEXT,
    acceptance TEXT, expected_docs TEXT,
    status TEXT DEFAULT 'Not started',
    confidence REAL, rationale TEXT,
    latest_doc_id INTEGER, source_email_id TEXT,
    human_review TEXT DEFAULT 'Unreviewed',
    human_note TEXT, reviewed_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_documents (
    doc_id SERIAL PRIMARY KEY,
    filename TEXT, path TEXT, sha256 TEXT,
    email_id TEXT, semantic_key TEXT,
    version INTEGER, supersedes INTEGER,
    parent_doc_id INTEGER,
    registered_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_episodes (
    episode_id SERIAL PRIMARY KEY,
    email_id TEXT, model TEXT, escalated_from INTEGER,
    started_at DOUBLE PRECISION, ended_at DOUBLE PRECISION, summary TEXT
);
CREATE TABLE IF NOT EXISTS pbc_trace (
    id SERIAL PRIMARY KEY,
    episode_id INTEGER, seq INTEGER,
    kind TEXT,
    name TEXT, payload TEXT, ts DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_verifications (
    id SERIAL PRIMARY KEY,
    item_id TEXT, doc_id INTEGER, verdict TEXT,
    rationale TEXT, criteria TEXT, confidence REAL,
    episode_id INTEGER, ts DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_clarifications (
    id SERIAL PRIMARY KEY,
    item_id TEXT, question TEXT, recipient TEXT,
    email_id TEXT, episode_id INTEGER, status TEXT DEFAULT 'open', ts DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_drafts (
    id SERIAL PRIMARY KEY,
    recipient TEXT, subject TEXT, body TEXT,
    item_ids TEXT, status TEXT DEFAULT 'pending',
    created_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_api_calls (
    id SERIAL PRIMARY KEY,
    episode_id INTEGER, model TEXT, purpose TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL, ts DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_emails (
    email_id TEXT PRIMARY KEY,
    thread_id TEXT, from_addr TEXT, from_name TEXT,
    to_addrs TEXT, subject TEXT, date DOUBLE PRECISION, body TEXT,
    attachments TEXT,
    processed_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pbc_users (
    user_id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'reviewer',
    created_at DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS pbc_item_reviews (
    id SERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    review TEXT NOT NULL,
    note TEXT,
    reviewed_at DOUBLE PRECISION,
    UNIQUE(item_id, user_id)
);
CREATE TABLE IF NOT EXISTS pbc_run_history (
    run_id SERIAL PRIMARY KEY,
    started_at DOUBLE PRECISION,
    ended_at DOUBLE PRECISION,
    status TEXT,
    summary TEXT,
    items_snapshot TEXT,
    episodes_snapshot TEXT,
    drafts_snapshot TEXT,
    api_calls_snapshot TEXT
);
"""

# SQLite schema (original)
_SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS pbc_items (
    item_id TEXT PRIMARY KEY,
    category TEXT, priority TEXT, description TEXT,
    acceptance TEXT, expected_docs TEXT,
    status TEXT DEFAULT 'Not started',
    confidence REAL, rationale TEXT,
    latest_doc_id INTEGER, source_email_id TEXT,
    human_review TEXT DEFAULT 'Unreviewed',
    human_note TEXT, reviewed_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS pbc_documents (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT, path TEXT, sha256 TEXT,
    email_id TEXT, semantic_key TEXT,
    version INTEGER, supersedes INTEGER,
    parent_doc_id INTEGER,
    registered_at REAL
);
CREATE TABLE IF NOT EXISTS pbc_episodes (
    episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT, model TEXT, escalated_from INTEGER,
    started_at REAL, ended_at REAL, summary TEXT
);
CREATE TABLE IF NOT EXISTS pbc_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER, seq INTEGER,
    kind TEXT,
    name TEXT, payload TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS pbc_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT, doc_id INTEGER, verdict TEXT,
    rationale TEXT, criteria TEXT, confidence REAL,
    episode_id INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS pbc_clarifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT, question TEXT, recipient TEXT,
    email_id TEXT, episode_id INTEGER, status TEXT DEFAULT 'open', ts REAL
);
CREATE TABLE IF NOT EXISTS pbc_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT, subject TEXT, body TEXT,
    item_ids TEXT, status TEXT DEFAULT 'pending',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS pbc_api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER, model TEXT, purpose TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL, ts REAL
);
CREATE TABLE IF NOT EXISTS pbc_emails (
    email_id TEXT PRIMARY KEY,
    thread_id TEXT, from_addr TEXT, from_name TEXT,
    to_addrs TEXT, subject TEXT, date REAL, body TEXT,
    attachments TEXT,
    processed_at REAL
);
CREATE TABLE IF NOT EXISTS pbc_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pbc_users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'reviewer',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS pbc_item_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    review TEXT NOT NULL,
    note TEXT,
    reviewed_at REAL,
    UNIQUE(item_id, user_id)
);
CREATE TABLE IF NOT EXISTS pbc_run_history (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    ended_at REAL,
    status TEXT,
    summary TEXT,
    items_snapshot TEXT,
    episodes_snapshot TEXT,
    drafts_snapshot TEXT,
    api_calls_snapshot TEXT
);
"""


class StatusGuardError(Exception):
    """Raised when a status change lacks the required verifier verdict."""


class _RowWrapper:
    """Makes psycopg2 DictRow behave like sqlite3.Row for .keys() access."""
    def __init__(self, row, keys):
        self._row = row
        self._keys = keys
    
    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[key]
        return self._row[self._keys.index(key)] if key in self._keys else None
    
    def keys(self):
        return self._keys


class Store:
    def __init__(self, db_path: str = "data/pbc.db"):
        self.db_path = db_path
        self._is_postgres = False
        self._placeholder = "?"  # SQLite style
        
        # Check for DATABASE_URL (PostgreSQL)
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            self._init_postgres(database_url)
        else:
            self._init_sqlite(db_path)
    
    def _init_postgres(self, database_url: str) -> None:
        """Initialize PostgreSQL connection."""
        import psycopg2
        import psycopg2.extras
        
        self._is_postgres = True
        self._placeholder = "%s"
        
        # Railway uses postgres:// but psycopg2 needs postgresql://
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = False
        
        # Create tables
        self._create_schema_pg()
        
        # Run migrations for existing DBs
        self._migrate_postgres()
    
    def _create_schema_pg(self) -> None:
        """Create tables from the PG schema.

        Each statement runs in its own transaction and commits immediately, so a
        failure in one CREATE cannot roll back tables created earlier in the loop
        (all statements use IF NOT EXISTS, so re-running is safe).
        """
        import psycopg2
        for stmt in _SCHEMA_PG.split(";"):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                with self.conn.cursor() as cur:
                    cur.execute(stmt)
                self.conn.commit()
            except psycopg2.Error:
                self.conn.rollback()
    
    def _pg_table_exists(self, table: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            exists = cur.fetchone()[0] is not None
        self.conn.rollback()
        return exists
    
    def _pg_column_exists(self, table: str, column: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=%s AND column_name=%s",
                (table, column))
            exists = cur.fetchone() is not None
        self.conn.rollback()
        return exists
    
    def _init_sqlite(self, db_path: str) -> None:
        """Initialize SQLite connection."""
        import sqlite3
        from pathlib import Path
        
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA_SQLITE)
        
        # Run migrations for existing DBs
        self._migrate_sqlite()
        self.conn.commit()
    
    def _migrate_sqlite(self) -> None:
        """Run migrations for SQLite."""
        import sqlite3
        for stmt in (
            "ALTER TABLE pbc_emails ADD COLUMN attachments TEXT",
            "ALTER TABLE pbc_items ADD COLUMN human_review TEXT DEFAULT 'Unreviewed'",
            "ALTER TABLE pbc_items ADD COLUMN human_note TEXT",
            "ALTER TABLE pbc_items ADD COLUMN reviewed_at REAL",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
    
    def _migrate_postgres(self) -> None:
        """Run migrations for PostgreSQL."""
        import psycopg2
        
        # Self-heal a users table left in a broken state by an older/interrupted
        # deploy (table exists but is missing the user_id column, e.g. created
        # from the SQLite-only schema). Drop and recreate it along with the
        # dependent item_reviews table, then recreate from the PG schema.
        # ensure_default_admin() will repopulate the default admin account.
        if self._pg_table_exists("pbc_users") and not self._pg_column_exists("pbc_users", "user_id"):
            try:
                with self.conn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS pbc_item_reviews CASCADE")
                    cur.execute("DROP TABLE IF EXISTS pbc_users CASCADE")
                self.conn.commit()
            except psycopg2.Error:
                self.conn.rollback()
            self._create_schema_pg()
        
        migrations = [
            "ALTER TABLE pbc_emails ADD COLUMN IF NOT EXISTS attachments TEXT",
            "ALTER TABLE pbc_items ADD COLUMN IF NOT EXISTS human_review TEXT DEFAULT 'Unreviewed'",
            "ALTER TABLE pbc_items ADD COLUMN IF NOT EXISTS human_note TEXT",
            "ALTER TABLE pbc_items ADD COLUMN IF NOT EXISTS reviewed_at DOUBLE PRECISION",
        ]
        for stmt in migrations:
            try:
                with self.conn.cursor() as cur:
                    cur.execute(stmt)
                self.conn.commit()
            except psycopg2.Error:
                self.conn.rollback()
    
    def _q(self, sql: str) -> str:
        """Convert ? placeholders to %s for PostgreSQL."""
        if self._is_postgres:
            return sql.replace("?", "%s")
        return sql
    
    def execute(self, sql: str, params: tuple = ()) -> Any:
        """Execute a query, handling differences between SQLite and PostgreSQL."""
        sql = self._q(sql)
        if self._is_postgres:
            import psycopg2.extras
            with self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql, params)
                return cur
        else:
            return self.conn.execute(sql, params)
    
    def fetchone(self, sql: str, params: tuple = ()) -> Optional[Any]:
        """Execute and fetch one row."""
        sql = self._q(sql)
        if self._is_postgres:
            import psycopg2.extras
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        else:
            row = self.conn.execute(sql, params).fetchone()
            return row
    
    def fetchall(self, sql: str, params: tuple = ()) -> list:
        """Execute and fetch all rows."""
        sql = self._q(sql)
        if self._is_postgres:
            import psycopg2.extras
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(row) for row in cur.fetchall()]
        else:
            return self.conn.execute(sql, params).fetchall()
    
    def execute_returning(self, sql: str, params: tuple, id_col: str) -> int:
        """Execute an INSERT and return the auto-generated ID."""
        if self._is_postgres:
            sql = self._q(sql) + f" RETURNING {id_col}"
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                result = cur.fetchone()[0]
                self.conn.commit()
                return result
        else:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid
    
    def commit(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    # ---------- items ----------
    def load_items(self, items: list[dict]) -> None:
        for it in items:
            sql = """INSERT INTO pbc_items
                   (item_id, category, priority, description, acceptance, expected_docs, status, updated_at)
                   VALUES (?,?,?,?,?,?, 'Not started', ?)"""
            if self._is_postgres:
                sql = self._q(sql) + " ON CONFLICT (item_id) DO NOTHING"
            else:
                sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
            
            if self._is_postgres:
                with self.conn.cursor() as cur:
                    cur.execute(sql, (it["item_id"], it.get("category"), it.get("priority"),
                                     it.get("description"), it.get("acceptance"),
                                     it.get("expected_docs"), time.time()))
            else:
                self.conn.execute(sql, (it["item_id"], it.get("category"), it.get("priority"),
                                       it.get("description"), it.get("acceptance"),
                                       it.get("expected_docs"), time.time()))
        self.conn.commit()

    def get_item(self, item_id: str) -> Optional[dict]:
        return self.fetchone("SELECT * FROM pbc_items WHERE item_id=?", (item_id,))

    def all_items(self) -> list:
        return self.fetchall("SELECT * FROM pbc_items ORDER BY item_id")

    def update_item_status(self, item_id: str, status: str, *, confidence: float | None = None,
                           rationale: str | None = None, doc_id: int | None = None,
                           email_id: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}; must be one of {STATUSES}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        if status in GUARDED_STATUSES:
            row = self.fetchone(
                "SELECT id FROM pbc_verifications WHERE item_id=? AND (? IS NULL OR doc_id=?) "
                "ORDER BY ts DESC LIMIT 1",
                (item_id, doc_id, doc_id),
            )
            if row is None:
                raise StatusGuardError(
                    f"Refusing to set {item_id} to {status!r}: no verify_item verdict on record"
                    + (f" for doc {doc_id}" if doc_id else "")
                    + ". Call verify_item first."
                )
        
        sql = """UPDATE pbc_items SET status=?,
               confidence=COALESCE(?, confidence),
               rationale=COALESCE(?, rationale),
               latest_doc_id=COALESCE(?, latest_doc_id),
               source_email_id=COALESCE(?, source_email_id),
               updated_at=? WHERE item_id=?"""
        
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (status, confidence, rationale, doc_id, email_id,
                                          time.time(), item_id))
        else:
            self.conn.execute(sql, (status, confidence, rationale, doc_id, email_id,
                                   time.time(), item_id))
        self.conn.commit()

    def set_human_review(self, item_id: str, review: str, note: str = "") -> None:
        """DEPRECATED: Use set_user_review for multi-tenant support."""
        if review not in ("Unreviewed", "Approved", "Rejected"):
            raise ValueError(f"Unknown review value {review!r}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        
        sql = "UPDATE pbc_items SET human_review=?, human_note=?, reviewed_at=? WHERE item_id=?"
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (review, note, time.time(), item_id))
        else:
            self.conn.execute(sql, (review, note, time.time(), item_id))
        self.conn.commit()

    # ---------- users & multi-tenant reviews ----------
    def create_user(self, username: str, password: str, display_name: str = "",
                    role: str = "reviewer") -> int:
        import hashlib
        if role not in ("reviewer", "lead", "admin"):
            raise ValueError(f"Unknown role {role!r}")
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        try:
            return self.execute_returning(
                "INSERT INTO pbc_users (username, password_hash, display_name, role, created_at) "
                "VALUES (?,?,?,?,?)",
                (username.lower(), password_hash, display_name or username, role, time.time()),
                "user_id"
            )
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError(f"Username {username!r} already exists")
            raise

    def authenticate_user(self, username: str, password: str) -> dict | None:
        import hashlib
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        return self.fetchone(
            "SELECT * FROM pbc_users WHERE username=? AND password_hash=?",
            (username.lower(), password_hash))

    def get_user(self, user_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM pbc_users WHERE user_id=?", (user_id,))

    def get_user_by_username(self, username: str) -> dict | None:
        return self.fetchone("SELECT * FROM pbc_users WHERE username=?", (username.lower(),))

    def list_users(self) -> list[dict]:
        return self.fetchall(
            "SELECT user_id, username, display_name, role, created_at FROM pbc_users ORDER BY username")

    def set_user_review(self, item_id: str, user_id: int, review: str, note: str = "") -> None:
        if review not in ("Unreviewed", "Approved", "Rejected"):
            raise ValueError(f"Unknown review value {review!r}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        if self.get_user(user_id) is None:
            raise ValueError(f"Unknown user {user_id!r}")
        
        if self._is_postgres:
            sql = """INSERT INTO pbc_item_reviews (item_id, user_id, review, note, reviewed_at)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT(item_id, user_id) DO UPDATE SET
                   review=EXCLUDED.review, note=EXCLUDED.note, reviewed_at=EXCLUDED.reviewed_at"""
            with self.conn.cursor() as cur:
                cur.execute(sql, (item_id, user_id, review, note, time.time()))
        else:
            sql = """INSERT INTO pbc_item_reviews (item_id, user_id, review, note, reviewed_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(item_id, user_id) DO UPDATE SET
                   review=excluded.review, note=excluded.note, reviewed_at=excluded.reviewed_at"""
            self.conn.execute(sql, (item_id, user_id, review, note, time.time()))
        self.conn.commit()

    def get_item_reviews(self, item_id: str) -> list[dict]:
        return self.fetchall(
            """SELECT r.*, u.username, u.display_name, u.role
               FROM pbc_item_reviews r JOIN pbc_users u ON r.user_id = u.user_id
               WHERE r.item_id=? ORDER BY r.reviewed_at""",
            (item_id,))

    def get_user_reviews(self, user_id: int) -> list[dict]:
        return self.fetchall(
            "SELECT * FROM pbc_item_reviews WHERE user_id=? ORDER BY reviewed_at DESC",
            (user_id,))

    def get_review_summary(self) -> dict:
        items = self.all_items()
        users = self.list_users()
        summary = {
            "total_items": len(items),
            "total_reviewers": len(users),
            "by_item": {},
            "by_user": {},
        }
        for it in items:
            item_id = it["item_id"] if isinstance(it, dict) else it[0]
            reviews = self.get_item_reviews(item_id)
            summary["by_item"][item_id] = {
                "reviews": reviews,
                "approved_count": sum(1 for r in reviews if r["review"] == "Approved"),
                "rejected_count": sum(1 for r in reviews if r["review"] == "Rejected"),
                "pending_count": len(users) - len(reviews),
            }
        for u in users:
            user_reviews = self.get_user_reviews(u["user_id"])
            summary["by_user"][u["username"]] = {
                "display_name": u["display_name"],
                "reviewed_count": len(user_reviews),
                "approved_count": sum(1 for r in user_reviews if r["review"] == "Approved"),
                "rejected_count": sum(1 for r in user_reviews if r["review"] == "Rejected"),
            }
        return summary

    def ensure_default_admin(self) -> None:
        if not self.list_users():
            self.create_user("admin", "admin", "Administrator", "admin")

    # ---------- documents ----------
    def register_document(self, filename: str, path: str, sha256: str, email_id: str,
                          semantic_key: str, parent_doc_id: int | None = None) -> dict:
        dup = self.fetchone(
            "SELECT doc_id, version FROM pbc_documents WHERE sha256=?", (sha256,))
        if dup:
            return {"doc_id": dup["doc_id"], "version": dup["version"],
                    "supersedes": None, "duplicate": True}
        
        prev = self.fetchone(
            "SELECT doc_id, version FROM pbc_documents WHERE semantic_key=? ORDER BY version DESC LIMIT 1",
            (semantic_key,))
        version = (prev["version"] + 1) if prev else 1
        supersedes = prev["doc_id"] if prev else None
        
        doc_id = self.execute_returning(
            """INSERT INTO pbc_documents (filename, path, sha256, email_id, semantic_key,
                                      version, supersedes, parent_doc_id, registered_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (filename, path, sha256, email_id, semantic_key, version, supersedes,
             parent_doc_id, time.time()),
            "doc_id"
        )
        return {"doc_id": doc_id, "version": version,
                "supersedes": supersedes, "duplicate": False}

    def get_document(self, doc_id: int) -> Optional[dict]:
        return self.fetchone("SELECT * FROM pbc_documents WHERE doc_id=?", (doc_id,))

    def lineage(self, doc_id: int) -> list:
        doc = self.get_document(doc_id)
        if doc is None:
            return []
        return self.fetchall(
            "SELECT * FROM pbc_documents WHERE semantic_key=? ORDER BY version",
            (doc["semantic_key"],))

    # ---------- episodes / trace ----------
    def start_episode(self, email_id: str, model: str, escalated_from: int | None = None) -> int:
        return self.execute_returning(
            "INSERT INTO pbc_episodes (email_id, model, escalated_from, started_at) VALUES (?,?,?,?)",
            (email_id, model, escalated_from, time.time()),
            "episode_id"
        )

    def end_episode(self, episode_id: int, summary: str = "") -> None:
        sql = "UPDATE pbc_episodes SET ended_at=?, summary=? WHERE episode_id=?"
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (time.time(), summary, episode_id))
        else:
            self.conn.execute(sql, (time.time(), summary, episode_id))
        self.conn.commit()

    def add_trace(self, episode_id: int, kind: str, name: str, payload: Any) -> None:
        seq_row = self.fetchone(
            "SELECT COALESCE(MAX(seq),0)+1 as seq FROM pbc_trace WHERE episode_id=?", (episode_id,))
        seq = seq_row["seq"] if isinstance(seq_row, dict) else seq_row[0]
        
        sql = "INSERT INTO pbc_trace (episode_id, seq, kind, name, payload, ts) VALUES (?,?,?,?,?,?)"
        payload_str = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (episode_id, seq, kind, name, payload_str, time.time()))
        else:
            self.conn.execute(sql, (episode_id, seq, kind, name, payload_str, time.time()))
        self.conn.commit()

    # ---------- verifications ----------
    def add_verification(self, item_id: str, doc_id: int, verdict: str, rationale: str,
                         criteria: Any, confidence: float, episode_id: int) -> None:
        sql = """INSERT INTO pbc_verifications (item_id, doc_id, verdict, rationale, criteria,
                                          confidence, episode_id, ts) VALUES (?,?,?,?,?,?,?,?)"""
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (item_id, doc_id, verdict, rationale,
                                          json.dumps(criteria, default=str),
                                          confidence, episode_id, time.time()))
        else:
            self.conn.execute(sql, (item_id, doc_id, verdict, rationale,
                                   json.dumps(criteria, default=str),
                                   confidence, episode_id, time.time()))
        self.conn.commit()

    # ---------- cost ----------
    def add_api_call(self, episode_id: int | None, model: str, purpose: str,
                     input_tokens: int, output_tokens: int, cache_read: int,
                     cache_write: int, cost_usd: float) -> None:
        sql = """INSERT INTO pbc_api_calls (episode_id, model, purpose, input_tokens, output_tokens,
                                      cache_read_tokens, cache_write_tokens, cost_usd, ts)
               VALUES (?,?,?,?,?,?,?,?,?)"""
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (episode_id, model, purpose, input_tokens, output_tokens,
                                          cache_read, cache_write, cost_usd, time.time()))
        else:
            self.conn.execute(sql, (episode_id, model, purpose, input_tokens, output_tokens,
                                   cache_read, cache_write, cost_usd, time.time()))
        self.conn.commit()

    def total_cost(self) -> float:
        row = self.fetchone("SELECT COALESCE(SUM(cost_usd),0) as total FROM pbc_api_calls")
        return row["total"] if isinstance(row, dict) else row[0]

    # ---------- misc ----------
    def add_email(self, e: dict) -> None:
        atts = [
            a if isinstance(a, dict) else
            {"filename": a.filename, "path": a.path, "sha256": a.sha256, "size": a.size}
            for a in (e.get("attachments") or [])
        ]
        sql = """INSERT INTO pbc_emails (email_id, thread_id, from_addr, from_name,
               to_addrs, subject, date, body, attachments, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)"""
        if self._is_postgres:
            sql = self._q(sql) + " ON CONFLICT (email_id) DO NOTHING"
            with self.conn.cursor() as cur:
                cur.execute(sql, (e["email_id"], e["thread_id"], e["from_addr"], e["from_name"],
                                 json.dumps(e["to_addrs"]), e["subject"], e["date"], e["body"],
                                 json.dumps(atts), time.time()))
        else:
            sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO")
            self.conn.execute(sql, (e["email_id"], e["thread_id"], e["from_addr"], e["from_name"],
                                   json.dumps(e["to_addrs"]), e["subject"], e["date"], e["body"],
                                   json.dumps(atts), time.time()))
        self.conn.commit()

    def add_clarification(self, item_id: str | None, question: str, recipient: str,
                          email_id: str, episode_id: int) -> None:
        sql = ("INSERT INTO pbc_clarifications (item_id, question, recipient, email_id, episode_id, ts)"
               " VALUES (?,?,?,?,?,?)")
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(self._q(sql), (item_id, question, recipient, email_id, episode_id, time.time()))
        else:
            self.conn.execute(sql, (item_id, question, recipient, email_id, episode_id, time.time()))
        self.conn.commit()

    def add_draft(self, recipient: str, subject: str, body: str, item_ids: list[str]) -> int:
        return self.execute_returning(
            "INSERT INTO pbc_drafts (recipient, subject, body, item_ids, created_at) VALUES (?,?,?,?,?)",
            (recipient, subject, body, json.dumps(item_ids), time.time()),
            "id"
        )

    def reset_all(self) -> None:
        """Wipe run data for a fresh restart."""
        self._archive_current_run()
        
        for table in ("pbc_items", "pbc_documents", "pbc_episodes", "pbc_trace", "pbc_verifications",
                      "pbc_clarifications", "pbc_drafts", "pbc_api_calls", "pbc_emails", "pbc_item_reviews"):
            if self._is_postgres:
                with self.conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table}")
            else:
                self.conn.execute(f"DELETE FROM {table}")
        
        # Keep OCR cache
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM pbc_meta WHERE key NOT LIKE 'ocr:%'")
        else:
            self.conn.execute("DELETE FROM pbc_meta WHERE key NOT LIKE 'ocr:%'")
        self.conn.commit()

    def _archive_current_run(self) -> int | None:
        """Archive current run data to run_history."""
        count_row = self.fetchone("SELECT COUNT(*) as cnt FROM pbc_episodes")
        episode_count = count_row["cnt"] if isinstance(count_row, dict) else count_row[0]
        if episode_count == 0:
            return None
        
        first_row = self.fetchone("SELECT MIN(started_at) as ts FROM pbc_episodes")
        first_episode = first_row["ts"] if isinstance(first_row, dict) else first_row[0]
        
        last_row = self.fetchone("SELECT MAX(ended_at) as ts FROM pbc_episodes")
        last_episode = last_row["ts"] if isinstance(last_row, dict) else last_row[0]
        
        status = self.get_meta("run_status") or "unknown"
        total_cost = self.total_cost()
        
        email_row = self.fetchone("SELECT COUNT(*) as cnt FROM pbc_emails")
        email_count = email_row["cnt"] if isinstance(email_row, dict) else email_row[0]
        
        esc_row = self.fetchone("SELECT COUNT(*) as cnt FROM pbc_episodes WHERE escalated_from IS NOT NULL")
        escalation_count = esc_row["cnt"] if isinstance(esc_row, dict) else esc_row[0]
        
        summary = json.dumps({
            "total_emails": email_count,
            "total_episodes": episode_count,
            "total_cost_usd": total_cost,
            "escalations": escalation_count,
            "run_args": json.loads(self.get_meta("run_args") or "null"),
        })
        
        items = self.fetchall("SELECT * FROM pbc_items")
        items_list = [dict(row) if not isinstance(row, dict) else row for row in items]
        
        episodes_data = []
        for ep in self.fetchall("SELECT * FROM pbc_episodes ORDER BY episode_id"):
            ep_dict = dict(ep) if not isinstance(ep, dict) else ep
            traces = self.fetchall(
                "SELECT * FROM pbc_trace WHERE episode_id=? ORDER BY seq",
                (ep_dict["episode_id"],))
            ep_dict["traces"] = [dict(t) if not isinstance(t, dict) else t for t in traces]
            verifs = self.fetchall(
                "SELECT * FROM pbc_verifications WHERE episode_id=?",
                (ep_dict["episode_id"],))
            ep_dict["verifications"] = [dict(v) if not isinstance(v, dict) else v for v in verifs]
            episodes_data.append(ep_dict)
        
        drafts = self.fetchall("SELECT * FROM pbc_drafts")
        drafts_list = [dict(d) if not isinstance(d, dict) else d for d in drafts]
        
        api_calls = self.fetchall("SELECT * FROM pbc_api_calls")
        api_calls_list = [dict(c) if not isinstance(c, dict) else c for c in api_calls]
        
        return self.execute_returning(
            """INSERT INTO pbc_run_history 
               (started_at, ended_at, status, summary, items_snapshot, 
                episodes_snapshot, drafts_snapshot, api_calls_snapshot)
               VALUES (?,?,?,?,?,?,?,?)""",
            (first_episode, last_episode or time.time(), status, summary,
             json.dumps(items_list), json.dumps(episodes_data),
             json.dumps(drafts_list), json.dumps(api_calls_list)),
            "run_id"
        )

    def get_run_history(self) -> list[dict]:
        return self.fetchall(
            "SELECT run_id, started_at, ended_at, status, summary FROM pbc_run_history ORDER BY run_id DESC")

    def get_run_snapshot(self, run_id: int) -> dict | None:
        row = self.fetchone("SELECT * FROM pbc_run_history WHERE run_id=?", (run_id,))
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
            "summary": json.loads(row["summary"] or "{}"),
            "items": json.loads(row["items_snapshot"] or "[]"),
            "episodes": json.loads(row["episodes_snapshot"] or "[]"),
            "drafts": json.loads(row["drafts_snapshot"] or "[]"),
            "api_calls": json.loads(row["api_calls_snapshot"] or "[]"),
        }

    def delete_run_history(self, run_id: int) -> bool:
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM pbc_run_history WHERE run_id=%s", (run_id,))
                deleted = cur.rowcount > 0
        else:
            cur = self.conn.execute("DELETE FROM pbc_run_history WHERE run_id=?", (run_id,))
            deleted = cur.rowcount > 0
        self.conn.commit()
        return deleted

    def set_meta(self, key: str, value: str) -> None:
        if self._is_postgres:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pbc_meta (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, value))
        else:
            self.conn.execute("INSERT OR REPLACE INTO pbc_meta (key, value) VALUES (?,?)", (key, value))
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.fetchone("SELECT value FROM pbc_meta WHERE key=?", (key,))
        if row is None:
            return None
        return row["value"] if isinstance(row, dict) else row[0]

    def tracker_summary(self) -> str:
        """Compact tracker state injected into each episode prompt."""
        lines = []
        for it in self.all_items():
            item_id = it["item_id"] if isinstance(it, dict) else it[0]
            status = it["status"] if isinstance(it, dict) else it[6]
            latest_doc_id = it["latest_doc_id"] if isinstance(it, dict) else it[9]
            rationale = it["rationale"] if isinstance(it, dict) else it[8]
            
            line = f"{item_id} [{status}]"
            if latest_doc_id:
                doc = self.get_document(latest_doc_id)
                if doc:
                    line += f" latest_doc={doc['filename']} (doc_id={doc['doc_id']} v{doc['version']})"
            if rationale:
                line += f" — {rationale[:120]}"
            lines.append(line)
        return "\n".join(lines)
