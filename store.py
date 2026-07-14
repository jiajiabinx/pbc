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
    kind TEXT,           -- plan | tool_call | tool_result | text | verdict | escalation | error
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
        """Auditor sign-off on an item's tracked state — separate from agent status."""
        if review not in ("Unreviewed", "Approved", "Rejected"):
            raise ValueError(f"Unknown review value {review!r}")
        if self.get_item(item_id) is None:
            raise ValueError(f"Unknown item {item_id!r}")
        self.conn.execute(
            "UPDATE items SET human_review=?, human_note=?, reviewed_at=? WHERE item_id=?",
            (review, note, time.time(), item_id))
        self.conn.commit()

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
