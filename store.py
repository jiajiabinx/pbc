"""SQLite persistence: tracker rows, document lineage, full agent traces, cost ledger.

The status guard lives here, in code, not in a prompt: no item may move to
Received / Insufficient / Complete without a verifier verdict recorded for that
(item, document) pair.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

STATUSES = ("Not started", "Requested", "Under review", "Received", "Insufficient", "Complete")
# Statuses that require an independent verifier verdict on file.
GUARDED_STATUSES = {"Received", "Insufficient", "Complete"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    category TEXT, priority TEXT, description TEXT,
    acceptance TEXT, expected_docs TEXT,
    status TEXT DEFAULT 'Not started',
    confidence REAL, rationale TEXT,
    latest_doc_id INTEGER, source_email_id TEXT,
    human_review TEXT DEFAULT 'Unreviewed',   -- Unreviewed | Approved | Rejected
    human_note TEXT, reviewed_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT, path TEXT, sha256 TEXT,
    email_id TEXT, semantic_key TEXT,
    version INTEGER, supersedes INTEGER,
    parent_doc_id INTEGER,          -- set for files extracted from a zip
    registered_at REAL
);
CREATE TABLE IF NOT EXISTS episodes (
    episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT, model TEXT, escalated_from INTEGER,
    started_at REAL, ended_at REAL, summary TEXT
);
CREATE TABLE IF NOT EXISTS trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER, seq INTEGER,
    kind TEXT,           -- plan | tool_call | tool_result | text | verdict | escalation | token_retry | error
    name TEXT, payload TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT, doc_id INTEGER, verdict TEXT,
    rationale TEXT, criteria TEXT, confidence REAL,
    episode_id INTEGER, ts REAL
);
CREATE TABLE IF NOT EXISTS clarifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT, question TEXT, recipient TEXT,
    email_id TEXT, episode_id INTEGER, status TEXT DEFAULT 'open', ts REAL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient TEXT, subject TEXT, body TEXT,
    item_ids TEXT, status TEXT DEFAULT 'pending',   -- pending | approved | edited | rejected | sent
    created_at REAL
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER, model TEXT, purpose TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    cost_usd REAL, ts REAL
);
CREATE TABLE IF NOT EXISTS emails (
    email_id TEXT PRIMARY KEY,
    thread_id TEXT, from_addr TEXT, from_name TEXT,
    to_addrs TEXT, subject TEXT, date REAL, body TEXT,
    attachments TEXT,    -- JSON [{filename, path, sha256, size}] as received
    processed_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'reviewer',  -- reviewer | lead | admin
    created_at REAL
);
CREATE TABLE IF NOT EXISTS item_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    review TEXT NOT NULL,           -- Unreviewed | Approved | Rejected
    note TEXT,
    reviewed_at REAL,
    UNIQUE(item_id, user_id)
);
CREATE TABLE IF NOT EXISTS run_history (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL,
    ended_at REAL,
    status TEXT,           -- finished | stopped | budget_exceeded | error
    summary TEXT,          -- JSON {total_emails, total_cost, etc}
    items_snapshot TEXT,   -- JSON snapshot of items table
    episodes_snapshot TEXT,-- JSON snapshot of episodes + trace
    drafts_snapshot TEXT,  -- JSON snapshot of drafts
    api_calls_snapshot TEXT-- JSON snapshot of api_calls for cost breakdown
);
"""


class StatusGuardError(Exception):
    """Raised when a status change lacks the required verifier verdict."""


class Store:
    def __init__(self, db_path: str = "data/pbc.db"):
        self.db_path = db_path
        # check_same_thread=False: Streamlit caches Store across script reruns
        # that execute on different threads (@st.cache_resource).
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Two processes share this DB (the agent runner and the Streamlit UI).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_SCHEMA)
        # migrate pre-existing DBs created before later columns were added
        for stmt in (
            "ALTER TABLE emails ADD COLUMN attachments TEXT",
            "ALTER TABLE items ADD COLUMN human_review TEXT DEFAULT 'Unreviewed'",
            "ALTER TABLE items ADD COLUMN human_note TEXT",
            "ALTER TABLE items ADD COLUMN reviewed_at REAL",
        ):
            try:
                self.conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    # ---------- items ----------
    def load_items(self, items: list[dict]) -> None:
        for it in items:
            self.conn.execute(
                """INSERT OR IGNORE INTO items
                   (item_id, category, priority, description, acceptance, expected_docs, status, updated_at)
                   VALUES (?,?,?,?,?,?, 'Not started', ?)""",
                (it["item_id"], it.get("category"), it.get("priority"), it.get("description"),
                 it.get("acceptance"), it.get("expected_docs"), time.time()),
            )
        self.conn.commit()

    def get_item(self, item_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()

    def all_items(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items ORDER BY item_id").fetchall()

    def update_item_status(self, item_id: str, status: str, *, confidence: float | None = None,
                           rationale: str | None = None, doc_id: int | None = None,
                           email_id: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}; must be one of {STATUSES}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        if status in GUARDED_STATUSES:
            row = self.conn.execute(
                "SELECT id FROM verifications WHERE item_id=? AND (? IS NULL OR doc_id=?) "
                "ORDER BY ts DESC LIMIT 1",
                (item_id, doc_id, doc_id),
            ).fetchone()
            if row is None:
                raise StatusGuardError(
                    f"Refusing to set {item_id} to {status!r}: no verify_item verdict on record"
                    + (f" for doc {doc_id}" if doc_id else "")
                    + ". Call verify_item first."
                )
        self.conn.execute(
            """UPDATE items SET status=?,
               confidence=COALESCE(?, confidence),
               rationale=COALESCE(?, rationale),
               latest_doc_id=COALESCE(?, latest_doc_id),
               source_email_id=COALESCE(?, source_email_id),
               updated_at=? WHERE item_id=?""",
            (status, confidence, rationale, doc_id, email_id, time.time(), item_id),
        )
        self.conn.commit()

    def set_human_review(self, item_id: str, review: str, note: str = "") -> None:
        """Auditor sign-off on an item's tracked state — separate from agent status.
        DEPRECATED: Use set_user_review for multi-tenant support."""
        if review not in ("Unreviewed", "Approved", "Rejected"):
            raise ValueError(f"Unknown review value {review!r}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        self.conn.execute(
            "UPDATE items SET human_review=?, human_note=?, reviewed_at=? WHERE item_id=?",
            (review, note, time.time(), item_id))
        self.conn.commit()

    # ---------- users & multi-tenant reviews ----------
    def create_user(self, username: str, password: str, display_name: str = "",
                    role: str = "reviewer") -> int:
        """Create a new user. Password is hashed with SHA-256 (simple, not production-grade)."""
        import hashlib
        if role not in ("reviewer", "lead", "admin"):
            raise ValueError(f"Unknown role {role!r}")
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        try:
            cur = self.conn.execute(
                "INSERT INTO users (username, password_hash, display_name, role, created_at) "
                "VALUES (?,?,?,?,?)",
                (username.lower(), password_hash, display_name or username, role, time.time()))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError(f"Username {username!r} already exists")

    def authenticate_user(self, username: str, password: str) -> dict | None:
        """Verify credentials. Returns user dict or None if invalid."""
        import hashlib
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        row = self.conn.execute(
            "SELECT * FROM users WHERE username=? AND password_hash=?",
            (username.lower(), password_hash)).fetchone()
        if row:
            return {k: row[k] for k in row.keys()}
        return None

    def get_user(self, user_id: int) -> dict | None:
        """Get user by ID."""
        row = self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return {k: row[k] for k in row.keys()}
        return None

    def get_user_by_username(self, username: str) -> dict | None:
        """Get user by username."""
        row = self.conn.execute(
            "SELECT * FROM users WHERE username=?", (username.lower(),)).fetchone()
        if row:
            return {k: row[k] for k in row.keys()}
        return None

    def list_users(self) -> list[dict]:
        """List all users."""
        rows = self.conn.execute(
            "SELECT user_id, username, display_name, role, created_at FROM users ORDER BY username"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def set_user_review(self, item_id: str, user_id: int, review: str, note: str = "") -> None:
        """Set a specific user's review for an item."""
        if review not in ("Unreviewed", "Approved", "Rejected"):
            raise ValueError(f"Unknown review value {review!r}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        if self.get_user(user_id) is None:
            raise ValueError(f"Unknown user {user_id!r}")
        self.conn.execute(
            """INSERT INTO item_reviews (item_id, user_id, review, note, reviewed_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(item_id, user_id) DO UPDATE SET
               review=excluded.review, note=excluded.note, reviewed_at=excluded.reviewed_at""",
            (item_id, user_id, review, note, time.time()))
        self.conn.commit()

    def get_item_reviews(self, item_id: str) -> list[dict]:
        """Get all reviews for an item, with user info."""
        rows = self.conn.execute(
            """SELECT r.*, u.username, u.display_name, u.role
               FROM item_reviews r JOIN users u ON r.user_id = u.user_id
               WHERE r.item_id=? ORDER BY r.reviewed_at""",
            (item_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def get_user_reviews(self, user_id: int) -> list[dict]:
        """Get all reviews by a specific user."""
        rows = self.conn.execute(
            "SELECT * FROM item_reviews WHERE user_id=? ORDER BY reviewed_at DESC",
            (user_id,)).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def get_review_summary(self) -> dict:
        """Get summary of reviews across all items and users."""
        items = self.all_items()
        users = self.list_users()
        summary = {
            "total_items": len(items),
            "total_reviewers": len(users),
            "by_item": {},
            "by_user": {},
        }
        for it in items:
            reviews = self.get_item_reviews(it["item_id"])
            summary["by_item"][it["item_id"]] = {
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
        """Create a default admin user if no users exist."""
        if not self.list_users():
            self.create_user("admin", "admin", "Administrator", "admin")

    # ---------- documents ----------
    def register_document(self, filename: str, path: str, sha256: str, email_id: str,
                          semantic_key: str, parent_doc_id: int | None = None) -> dict:
        """Insert a document, chaining versions by semantic key. Returns lineage info."""
        dup = self.conn.execute(
            "SELECT doc_id, version FROM documents WHERE sha256=?", (sha256,)
        ).fetchone()
        if dup:
            return {"doc_id": dup["doc_id"], "version": dup["version"],
                    "supersedes": None, "duplicate": True}
        prev = self.conn.execute(
            "SELECT doc_id, version FROM documents WHERE semantic_key=? ORDER BY version DESC LIMIT 1",
            (semantic_key,),
        ).fetchone()
        version = (prev["version"] + 1) if prev else 1
        supersedes = prev["doc_id"] if prev else None
        cur = self.conn.execute(
            """INSERT INTO documents (filename, path, sha256, email_id, semantic_key,
                                      version, supersedes, parent_doc_id, registered_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (filename, path, sha256, email_id, semantic_key, version, supersedes,
             parent_doc_id, time.time()),
        )
        self.conn.commit()
        return {"doc_id": cur.lastrowid, "version": version,
                "supersedes": supersedes, "duplicate": False}

    def get_document(self, doc_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()

    def lineage(self, doc_id: int) -> list[sqlite3.Row]:
        doc = self.get_document(doc_id)
        if doc is None:
            return []
        return self.conn.execute(
            "SELECT * FROM documents WHERE semantic_key=? ORDER BY version", (doc["semantic_key"],)
        ).fetchall()

    # ---------- episodes / trace ----------
    def start_episode(self, email_id: str, model: str, escalated_from: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO episodes (email_id, model, escalated_from, started_at) VALUES (?,?,?,?)",
            (email_id, model, escalated_from, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def end_episode(self, episode_id: int, summary: str = "") -> None:
        self.conn.execute("UPDATE episodes SET ended_at=?, summary=? WHERE episode_id=?",
                          (time.time(), summary, episode_id))
        self.conn.commit()

    def add_trace(self, episode_id: int, kind: str, name: str, payload: Any) -> None:
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM trace WHERE episode_id=?", (episode_id,)
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO trace (episode_id, seq, kind, name, payload, ts) VALUES (?,?,?,?,?,?)",
            (episode_id, seq, kind, name,
             payload if isinstance(payload, str) else json.dumps(payload, default=str),
             time.time()),
        )
        self.conn.commit()

    # ---------- verifications ----------
    def add_verification(self, item_id: str, doc_id: int, verdict: str, rationale: str,
                         criteria: Any, confidence: float, episode_id: int) -> None:
        self.conn.execute(
            """INSERT INTO verifications (item_id, doc_id, verdict, rationale, criteria,
                                          confidence, episode_id, ts) VALUES (?,?,?,?,?,?,?,?)""",
            (item_id, doc_id, verdict, rationale, json.dumps(criteria, default=str),
             confidence, episode_id, time.time()),
        )
        self.conn.commit()

    # ---------- cost ----------
    def add_api_call(self, episode_id: int | None, model: str, purpose: str,
                     input_tokens: int, output_tokens: int, cache_read: int,
                     cache_write: int, cost_usd: float) -> None:
        self.conn.execute(
            """INSERT INTO api_calls (episode_id, model, purpose, input_tokens, output_tokens,
                                      cache_read_tokens, cache_write_tokens, cost_usd, ts)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (episode_id, model, purpose, input_tokens, output_tokens, cache_read,
             cache_write, cost_usd, time.time()),
        )
        self.conn.commit()

    def total_cost(self) -> float:
        return self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM api_calls").fetchone()[0]

    # ---------- misc ----------
    def add_email(self, e: dict) -> None:
        atts = [
            a if isinstance(a, dict) else
            {"filename": a.filename, "path": a.path, "sha256": a.sha256, "size": a.size}
            for a in (e.get("attachments") or [])
        ]
        self.conn.execute(
            """INSERT OR IGNORE INTO emails (email_id, thread_id, from_addr, from_name,
               to_addrs, subject, date, body, attachments, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (e["email_id"], e["thread_id"], e["from_addr"], e["from_name"],
             json.dumps(e["to_addrs"]), e["subject"], e["date"], e["body"],
             json.dumps(atts), time.time()),
        )
        self.conn.commit()

    def add_clarification(self, item_id: str | None, question: str, recipient: str,
                          email_id: str, episode_id: int) -> None:
        self.conn.execute(
            "INSERT INTO clarifications (item_id, question, recipient, email_id, episode_id, ts)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, question, recipient, email_id, episode_id, time.time()),
        )
        self.conn.commit()

    def add_draft(self, recipient: str, subject: str, body: str, item_ids: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO drafts (recipient, subject, body, item_ids, created_at) VALUES (?,?,?,?,?)",
            (recipient, subject, body, json.dumps(item_ids), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def reset_all(self) -> None:
        """Wipe run data for a fresh restart, in place (keeps the file/inode so an
        open UI connection stays valid, and keeps the OCR cache — content-hash
        keyed vision transcriptions stay correct and cost money to redo).
        
        Archives the current run to run_history before clearing."""
        # Archive the current run if there's anything to archive
        self._archive_current_run()
        
        for table in ("items", "documents", "episodes", "trace", "verifications",
                      "clarifications", "drafts", "api_calls", "emails"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute("DELETE FROM meta WHERE key NOT LIKE 'ocr:%'")
        self.conn.commit()

    def _archive_current_run(self) -> int | None:
        """Archive current run data to run_history. Returns run_id or None if nothing to archive."""
        # Check if there's anything to archive
        episode_count = self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        if episode_count == 0:
            return None
        
        # Gather timestamps
        first_episode = self.conn.execute(
            "SELECT MIN(started_at) FROM episodes").fetchone()[0]
        last_episode = self.conn.execute(
            "SELECT MAX(ended_at) FROM episodes").fetchone()[0]
        
        # Determine run status
        status = self.get_meta("run_status") or "unknown"
        
        # Build summary
        total_cost = self.total_cost()
        email_count = self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        escalation_count = self.conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE escalated_from IS NOT NULL").fetchone()[0]
        summary = json.dumps({
            "total_emails": email_count,
            "total_episodes": episode_count,
            "total_cost_usd": total_cost,
            "escalations": escalation_count,
            "run_args": json.loads(self.get_meta("run_args") or "null"),
        })
        
        # Snapshot items
        items = [{k: row[k] for k in row.keys()}
                 for row in self.conn.execute("SELECT * FROM items").fetchall()]
        
        # Snapshot episodes with their traces
        episodes_data = []
        for ep in self.conn.execute("SELECT * FROM episodes ORDER BY episode_id").fetchall():
            ep_dict = {k: ep[k] for k in ep.keys()}
            traces = [{k: t[k] for k in t.keys()}
                      for t in self.conn.execute(
                          "SELECT * FROM trace WHERE episode_id=? ORDER BY seq",
                          (ep["episode_id"],)).fetchall()]
            ep_dict["traces"] = traces
            # Include verifications for this episode
            verifs = [{k: v[k] for k in v.keys()}
                      for v in self.conn.execute(
                          "SELECT * FROM verifications WHERE episode_id=?",
                          (ep["episode_id"],)).fetchall()]
            ep_dict["verifications"] = verifs
            episodes_data.append(ep_dict)
        
        # Snapshot drafts
        drafts = [{k: d[k] for k in d.keys()}
                  for d in self.conn.execute("SELECT * FROM drafts").fetchall()]
        
        # Snapshot API calls for cost breakdown
        api_calls = [{k: c[k] for k in c.keys()}
                     for c in self.conn.execute("SELECT * FROM api_calls").fetchall()]
        
        # Insert into run_history
        cur = self.conn.execute(
            """INSERT INTO run_history 
               (started_at, ended_at, status, summary, items_snapshot, 
                episodes_snapshot, drafts_snapshot, api_calls_snapshot)
               VALUES (?,?,?,?,?,?,?,?)""",
            (first_episode, last_episode or time.time(), status, summary,
             json.dumps(items), json.dumps(episodes_data),
             json.dumps(drafts), json.dumps(api_calls))
        )
        self.conn.commit()
        return cur.lastrowid

    def get_run_history(self) -> list[dict]:
        """Get list of archived runs (metadata only, not full snapshots)."""
        rows = self.conn.execute(
            "SELECT run_id, started_at, ended_at, status, summary FROM run_history ORDER BY run_id DESC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]

    def get_run_snapshot(self, run_id: int) -> dict | None:
        """Get full snapshot data for a specific run."""
        row = self.conn.execute(
            "SELECT * FROM run_history WHERE run_id=?", (run_id,)).fetchone()
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
        """Delete a specific run from history."""
        cur = self.conn.execute("DELETE FROM run_history WHERE run_id=?", (run_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def tracker_summary(self) -> str:
        """Compact tracker state injected into each episode prompt."""
        lines = []
        for it in self.all_items():
            line = f"{it['item_id']} [{it['status']}]"
            if it["latest_doc_id"]:
                doc = self.get_document(it["latest_doc_id"])
                if doc:
                    line += f" latest_doc={doc['filename']} (doc_id={doc['doc_id']} v{doc['version']})"
            if it["rationale"]:
                line += f" — {it['rationale'][:120]}"
            lines.append(line)
        return "\n".join(lines)
