"""Streamlit UI: Tracker view · Agent trace view · Follow-up review (send mocked).

    streamlit run ui.py [-- --db data/pbc.db]
"""
from __future__ import annotations

import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import versioning
from store import Store

DB = "data/pbc.db"
if "--db" in sys.argv:
    DB = sys.argv[sys.argv.index("--db") + 1]

st.set_page_config(page_title="PBC Tracker", layout="wide")


@st.cache_resource
def get_store() -> Store:
    return Store(DB)


store = get_store()

STATUS_COLORS = {
    "Not started": "⚪", "Requested": "🔵", "Under review": "🟡",
    "Received": "🟢", "Insufficient": "🔴", "Complete": "✅",
}


def ts(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""


REVIEW_ICONS = {"Unreviewed": "◻️", "Approved": "☑️", "Rejected": "❌"}


def render_attachment(path: str, filename: str, key: str) -> None:
    """Inline preview of an attachment so the human can check the source directly."""
    if not Path(path).exists():
        st.caption("(file no longer on disk)")
        return
    fam = versioning.ext_family(filename)
    try:
        if fam == "image":
            st.image(path, caption=filename)
        elif fam == "pdf":
            if hasattr(st, "pdf"):
                st.pdf(path)
            else:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    for i, page in enumerate(pdf.pages, 1):
                        st.text(f"[page {i}]\n{(page.extract_text() or '').strip()}"[:4000])
        elif fam == "excel":
            sheets = pd.read_excel(path, sheet_name=None)
            for name, df in sheets.items():
                st.caption(f"sheet: {name}")
                st.dataframe(df.head(100), width="stretch", key=f"{key}_{name}")
        elif fam == "archive":
            with zipfile.ZipFile(path) as zf:
                st.text("Archive contents:\n" + "\n".join(
                    f"  {i.filename} ({i.file_size:,} bytes)"
                    for i in zf.infolist() if not i.is_dir()))
        else:
            st.text(Path(path).read_text(errors="replace")[:4000])
    except Exception as e:
        st.warning(f"Preview failed: {e}")


# ------------------------------------------------------------------ sidebar
total = store.total_cost()
st.sidebar.title("PBC Email Agent")
st.sidebar.metric("Measured LLM cost", f"${total:.4f}")
st.sidebar.progress(min(total / 2.0, 1.0), text=f"{total / 2.0:.0%} of $2.00 budget")
calls = store.conn.execute(
    "SELECT model, COUNT(*) n, SUM(cost_usd) c FROM api_calls GROUP BY model").fetchall()
for c in calls:
    st.sidebar.caption(f"{c['model']}: {c['n']} calls, ${c['c']:.4f}")

tab_tracker, tab_trace, tab_drafts = st.tabs(
    ["📋 Tracker", "🔍 Agent trace", "✉️ Follow-up review"])

# ------------------------------------------------------------------ tracker
with tab_tracker:
    items = store.all_items()
    rows = []
    for it in items:
        doc = store.get_document(it["latest_doc_id"]) if it["latest_doc_id"] else None
        email = None
        if it["source_email_id"]:
            email = store.conn.execute(
                "SELECT subject, from_addr FROM emails WHERE email_id=?",
                (it["source_email_id"],)).fetchone()
        review = it["human_review"] or "Unreviewed"
        rows.append({
            "Item": it["item_id"],
            "Status": f"{STATUS_COLORS.get(it['status'], '')} {it['status']}",
            "Human review": f"{REVIEW_ICONS.get(review, '')} {review}",
            "Category": it["category"],
            "Priority": it["priority"],
            "Latest version": f"{doc['filename']} (v{doc['version']})" if doc else "",
            "Source email": email["subject"] if email else "",
            "Confidence": round(it["confidence"], 2) if it["confidence"] else None,
            "Description": (it["description"] or "")[:90],
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=600)

    st.subheader("Item detail")
    sel = st.selectbox("Item", [it["item_id"] for it in items])
    it = store.get_item(sel)
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**{it['item_id']}** [{it['category']}] priority={it['priority']}")
        st.write(it["description"])
        st.caption(f"Acceptance: {it['acceptance']}")
        st.markdown(f"Status: **{it['status']}**" +
                    (f" (confidence {it['confidence']:.2f})" if it["confidence"] else ""))
        if it["rationale"]:
            st.info(it["rationale"])

        st.markdown("**👤 Human review (your sign-off)**")
        current = it["human_review"] or "Unreviewed"
        choice = st.radio("Review", ["Unreviewed", "Approved", "Rejected"],
                          index=["Unreviewed", "Approved", "Rejected"].index(current),
                          horizontal=True, key=f"rev_{sel}",
                          label_visibility="collapsed")
        note = st.text_input("Review note", it["human_note"] or "", key=f"revnote_{sel}",
                             placeholder="e.g. checked source doc, agree with Insufficient")
        if st.button("Save review", key=f"revsave_{sel}"):
            store.set_human_review(sel, choice, note)
            st.toast(f"{sel} marked {choice}")
            st.rerun()
        if it["reviewed_at"]:
            st.caption(f"Last reviewed {ts(it['reviewed_at'])}")
    with col2:
        if it["latest_doc_id"]:
            st.markdown("**Version lineage**")
            for d in store.lineage(it["latest_doc_id"]):
                marker = "→" if d["doc_id"] == it["latest_doc_id"] else " "
                st.text(f"{marker} v{d['version']}  {d['filename']}  ({ts(d['registered_at'])})"
                        + (f"  supersedes doc {d['supersedes']}" if d["supersedes"] else ""))
        verifs = store.conn.execute(
            "SELECT * FROM verifications WHERE item_id=? ORDER BY ts", (sel,)).fetchall()
        if verifs:
            st.markdown("**Verifier verdicts**")
            for v in verifs:
                doc = store.get_document(v["doc_id"])
                icon = "🟢" if v["verdict"] == "sufficient" else "🔴"
                with st.expander(f"{icon} {v['verdict']} — {doc['filename'] if doc else v['doc_id']}"
                                 f" ({ts(v['ts'])})"):
                    st.write(v["rationale"])
                    for c in json.loads(v["criteria"] or "[]"):
                        st.text(f"{'✓' if c['met'] else '✗'} {c['criterion']}: {c['evidence']}")

# ------------------------------------------------------------------ trace
with tab_trace:
    episodes = store.conn.execute(
        """SELECT e.*, m.subject, m.from_addr FROM episodes e
           LEFT JOIN emails m ON m.email_id = e.email_id ORDER BY e.episode_id""").fetchall()
    options = {
        f"#{e['episode_id']} [{e['model'].split('-')[1]}] {e['subject']} — {e['from_addr']}"
        + (" (escalated)" if e["escalated_from"] else ""): e["episode_id"]
        for e in episodes
    }
    if not options:
        st.info("No episodes yet — run `python run.py ...` first.")
    else:
        chosen = st.selectbox("Episode", list(options))
        eid = options[chosen]
        ep = store.conn.execute("SELECT * FROM episodes WHERE episode_id=?", (eid,)).fetchone()
        cost = store.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM api_calls WHERE episode_id=?",
            (eid,)).fetchone()
        st.caption(f"model: {ep['model']} · {cost['n']} API calls · ${cost['c']:.4f}"
                   + (f" · escalated from episode #{ep['escalated_from']}" if ep["escalated_from"] else ""))
        if ep["summary"]:
            st.success(f"Outcome: {ep['summary']}")

        # ---- audit context: the original email, its attachments, and what got labeled
        email_row = store.conn.execute(
            "SELECT * FROM emails WHERE email_id=?", (ep["email_id"],)).fetchone()
        col_mail, col_items = st.columns([3, 2])
        with col_mail:
            st.markdown("**📧 Original email**")
            if email_row is None:
                st.caption("(email not on record)")
            else:
                st.text(f"From:    {email_row['from_name']} <{email_row['from_addr']}>\n"
                        f"To:      {', '.join(json.loads(email_row['to_addrs'] or '[]'))}\n"
                        f"Date:    {ts(email_row['date'])}\n"
                        f"Subject: {email_row['subject']}")
                st.code(email_row["body"] or "(empty body)", language=None)
                # older DBs (pre-migration) may lack the attachments column
                atts = (json.loads(email_row["attachments"] or "[]")
                        if "attachments" in email_row.keys() else [])
                if atts:
                    st.markdown("**📎 Attachments as received**")
                    for a in atts:
                        reg = store.conn.execute(
                            "SELECT doc_id, version FROM documents WHERE sha256=?",
                            (a["sha256"],)).fetchone()
                        line = f"{a['filename']} ({a['size']:,} bytes, sha {a['sha256'][:12]}…)"
                        line += (f" — registered as doc {reg['doc_id']} v{reg['version']}"
                                 if reg else " — ⚠️ never registered by the agent")
                        st.text(line)
                        with st.expander(f"👁 View {a['filename']}"):
                            render_attachment(a["path"], a["filename"],
                                              key=f"pv{eid}_{a['sha256'][:8]}")
                            try:
                                with open(a["path"], "rb") as fh:
                                    st.download_button(f"⬇ Download {a['filename']}", fh.read(),
                                                       file_name=a["filename"],
                                                       key=f"dl{eid}_{a['sha256'][:8]}")
                            except OSError:
                                pass
        with col_items:
            st.markdown("**🏷️ Items labeled in this episode**")
            updates = [json.loads(t["payload"]) for t in store.conn.execute(
                "SELECT payload FROM trace WHERE episode_id=? AND kind='tool_call' "
                "AND name='update_item_status' ORDER BY seq", (eid,))]
            verifs_ep = {v["item_id"]: v for v in store.conn.execute(
                "SELECT * FROM verifications WHERE episode_id=?", (eid,))}
            if not updates and not verifs_ep:
                st.caption("(no tracker changes in this episode)")
            for u in updates:
                v = verifs_ep.get(u["item_id"])
                icon = STATUS_COLORS.get(u["status"], "")
                st.markdown(f"{icon} **{u['item_id']} → {u['status']}**")
                st.caption(f"Agent rationale: {u.get('rationale', '')}")
                if v:
                    doc = store.get_document(v["doc_id"])
                    st.caption(f"Verifier ({'🟢' if v['verdict'] == 'sufficient' else '🔴'} "
                               f"{v['verdict']}, conf {v['confidence']:.2f}) on "
                               f"{doc['filename'] if doc else v['doc_id']}: {v['rationale']}")
                elif u["status"] in ("Received", "Insufficient", "Complete"):
                    st.warning("No verifier verdict in this episode — check the guard trace.")
            # verdicts that didn't lead to a status change still matter for audit
            for iid, v in verifs_ep.items():
                if not any(u["item_id"] == iid for u in updates):
                    st.markdown(f"⚖️ **{iid}** verified "
                                f"({'🟢' if v['verdict'] == 'sufficient' else '🔴'} {v['verdict']}) "
                                "without a status change")
                    st.caption(v["rationale"])

        st.markdown("**🧵 Step-by-step trace**")
        for t in store.conn.execute(
                "SELECT * FROM trace WHERE episode_id=? ORDER BY seq", (eid,)).fetchall():
            payload = t["payload"]
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                pass
            if t["kind"] == "plan":
                st.markdown("**📌 Plan** — classification: "
                            f"`{payload.get('classification', '?')}`")
                for s in payload.get("steps", []):
                    st.text(f"  {s}")
            elif t["kind"] == "tool_call":
                with st.expander(f"🔧 {t['name']} — call", expanded=False):
                    st.json(payload)
            elif t["kind"] == "tool_result":
                with st.expander(f"↩️ {t['name']} — result", expanded=False):
                    if isinstance(payload, (dict, list)):
                        st.json(payload)
                    else:
                        st.text(str(payload)[:3000])
            elif t["kind"] == "verdict":
                icon = "🟢" if isinstance(payload, dict) and payload.get("verdict") == "sufficient" else "🔴"
                st.markdown(f"{icon} **Verifier** `{t['name']}`: "
                            f"{payload.get('verdict') if isinstance(payload, dict) else payload}")
                if isinstance(payload, dict):
                    st.caption(payload.get("rationale", ""))
            elif t["kind"] == "escalation":
                st.warning(f"⬆️ Escalated from {t['name']}: {payload}")
            elif t["kind"] == "text":
                st.caption(f"💬 {payload}")

        with st.expander("Per-call cost detail"):
            df = pd.read_sql_query(
                "SELECT model, purpose, input_tokens, output_tokens, cache_read_tokens,"
                " cache_write_tokens, cost_usd FROM api_calls WHERE episode_id=?",
                store.conn, params=(eid,))
            st.dataframe(df, width="stretch", hide_index=True)

# ------------------------------------------------------------------ drafts
with tab_drafts:
    drafts = store.conn.execute("SELECT * FROM drafts ORDER BY id").fetchall()
    if not drafts:
        st.info("No drafts yet.")
    for d in drafts:
        status = d["status"]
        badge = {"pending": "🟠 pending", "approved": "🟢 approved", "sent": "📤 sent (mocked)",
                 "rejected": "⛔ rejected", "edited": "✏️ edited"}.get(status, status)
        with st.expander(f"{badge} — To: {d['recipient']} — {d['subject']}",
                         expanded=(status == "pending")):
            st.caption(f"Covers: {', '.join(json.loads(d['item_ids']))}")
            new_subject = st.text_input("Subject", d["subject"], key=f"subj{d['id']}")
            new_body = st.text_area("Body", d["body"], key=f"body{d['id']}", height=260)
            c1, c2, c3 = st.columns(3)
            if c1.button("✅ Approve & send (mocked)", key=f"ap{d['id']}",
                         disabled=status == "sent"):
                edited = new_subject != d["subject"] or new_body != d["body"]
                store.conn.execute(
                    "UPDATE drafts SET subject=?, body=?, status='sent' WHERE id=?",
                    (new_subject, new_body, d["id"]))
                store.conn.commit()
                st.toast(f"Mock-sent to {d['recipient']}" + (" (with edits)" if edited else ""))
                st.rerun()
            if c2.button("💾 Save edits", key=f"ed{d['id']}"):
                store.conn.execute(
                    "UPDATE drafts SET subject=?, body=?, status='edited' WHERE id=?",
                    (new_subject, new_body, d["id"]))
                store.conn.commit()
                st.rerun()
            if c3.button("⛔ Reject", key=f"rj{d['id']}"):
                store.conn.execute("UPDATE drafts SET status='rejected' WHERE id=?", (d["id"],))
                store.conn.commit()
                st.rerun()
