"""Grouped follow-up drafting: one clean email per recipient, not 15 separate ones.

Two-stage: a grouping call (Sonnet — judgment about who owns what) decides which
outstanding items go to which client contact, then a cheap drafting call (Haiku)
writes each email constrained to tracker facts passed in. Drafts land in the
review queue; "send" is mocked behind the approve/edit/reject UI.
"""
from __future__ import annotations

import json

import models
from store import Store

_GROUP_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "email address"},
                    "recipient_name": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["recipient", "recipient_name", "item_ids", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["groups"],
    "additionalProperties": False,
}


def _outstanding_items(store: Store) -> list[dict]:
    out = []
    for it in store.all_items():
        if it["status"] in ("Requested", "Insufficient", "Under review"):
            out.append(dict(it))
        elif it["status"] == "Not started" and it["priority"] == "High":
            out.append(dict(it))
    return out


def _correspondents(store: Store) -> str:
    rows = store.conn.execute(
        "SELECT MAX(from_name) from_name, from_addr, COUNT(*) n "
        "FROM emails GROUP BY from_addr ORDER BY n DESC"
    ).fetchall()
    return "\n".join(f"  {r['from_name']} <{r['from_addr']}> ({r['n']} emails)" for r in rows)


def generate_drafts(store: Store, router: models.Router, profile_text: str) -> list[int]:
    items = _outstanding_items(store)
    if not items:
        return []
    clar_rows = store.conn.execute(
        "SELECT item_id, question, recipient FROM clarifications WHERE status='open'").fetchall()
    clarifications = [dict(r) for r in clar_rows]

    item_lines = "\n".join(
        f"{it['item_id']} [{it['status']}] {it['description'][:120]}"
        + (f" — {it['rationale'][:150]}" if it["rationale"] else "")
        for it in items
    )
    groups = models.structured_json(
        router, models.GROUPER, purpose="draft-grouping", max_tokens=2000,
        schema=_GROUP_SCHEMA,
        user=(
            f"You are the audit senior's assistant deciding follow-up emails for outstanding "
            f"PBC items.\n\nCLIENT PROFILE:\n{profile_text}\n\n"
            f"PEOPLE SEEN ON THE MAILBOX:\n{_correspondents(store)}\n\n"
            f"OUTSTANDING ITEMS (status + why):\n{item_lines}\n\n"
            f"OPEN CLARIFICATIONS:\n{json.dumps(clarifications)}\n\n"
            "Group the outstanding items into follow-up emails, one per client-side recipient. "
            "Send the bulk to the primary client contact (usually the controller). If a "
            "specific person owns specific items (e.g. a bookkeeper who owns bank recs and was "
            "asked for a corrected format), give them their own short email — an item may "
            "appear in more than one group when both people need to act. Never address audit "
            "firm staff. Exclude items already sufficient/complete."))["groups"]

    by_id = {it["item_id"]: it for it in items}
    draft_ids = []
    for g in groups:
        detail = "\n".join(
            f"- {iid}: {by_id[iid]['description'][:150]} (status: {by_id[iid]['status']}"
            + (f"; note: {by_id[iid]['rationale'][:180]}" if by_id[iid].get("rationale") else "")
            + ")"
            for iid in g["item_ids"] if iid in by_id
        )
        rel_clar = [c for c in clarifications
                    if c["recipient"] == g["recipient"] or c["item_id"] in g["item_ids"]]
        resp = router.call(
            models.DRAFTER, purpose=f"draft:{g['recipient']}", max_tokens=1000,
            messages=[{"role": "user", "content":
                f"Write a professional, concise follow-up email from the audit senior to "
                f"{g['recipient_name']} <{g['recipient']}> chasing these PBC items. Use ONLY "
                f"the facts below — do not invent deadlines, amounts, or history.\n\n"
                f"ITEMS:\n{detail}\n\n"
                + (f"CLARIFICATION QUESTIONS TO INCLUDE:\n{json.dumps(rel_clar)}\n\n" if rel_clar else "")
                + "Group related items, state specifically what is missing or wrong per item, "
                  "keep it under 250 words, sign as 'Audit Team'. Output only: first line "
                  "'Subject: ...', then a blank line, then the body."}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        subject, _, body = text.partition("\n")
        subject = subject.removeprefix("Subject:").strip()
        draft_ids.append(store.add_draft(g["recipient"], subject, body.strip(),
                                         [i for i in g["item_ids"] if i in by_id]))
    return draft_ids
