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

import runctl
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
RUN_BADGES = {
    "idle": "⚪ idle", "launching": "🚀 launching…", "running": "🟢 running",
    "paused": "⏸️ paused", "drafting": "✍️ drafting follow-ups",
    "stopped": "⏹️ stopped", "finished": "✅ finished",
    "budget_exceeded": "💸 budget cap hit", "error": "🔥 error", "crashed": "💀 crashed",
}


def _rc_request(action: str) -> None:
    runctl.request(store, action)


def _rc_start(fresh: bool) -> None:
    st.session_state.pop("rc_error", None)
    try:
        runctl.start(store, st.session_state.rc_mailbox, st.session_state.rc_pbc,
                     st.session_state.rc_profile, budget=st.session_state.rc_budget,
                     fresh=fresh)
    except RuntimeError as e:
        st.session_state["rc_error"] = str(e)


@st.fragment(run_every=2)
def run_control_panel():
    stt = runctl.state(store)
    status = stt["status"]
    st.markdown(f"**Run:** {RUN_BADGES.get(status, status)}")
    prog = stt["progress"]
    if prog.get("total"):
        st.progress(min(prog.get("done", 0) / prog["total"], 1.0),
                    text=f"{prog.get('done', 0)}/{prog['total']} emails · "
                         f"{(prog.get('current') or '')[:38]}")
    if status == "error" and stt["error"]:
        st.error(stt["error"])
    if status == "crashed":
        st.warning("Runner process died — see data/run.log")

    if status in ("running", "drafting", "launching"):
        c1, c2 = st.columns(2)
        c1.button("⏸ Pause", key="rc_pause", width="stretch",
                  on_click=_rc_request, args=("pause",))
        c2.button("⏹ Stop", key="rc_stop", width="stretch",
                  on_click=_rc_request, args=("stop",))
        st.caption("Pause/stop take effect after the current email finishes.")
    elif status == "paused":
        c1, c2 = st.columns(2)
        c1.button("▶ Resume", key="rc_resume", width="stretch",
                  on_click=_rc_request, args=("run",))
        c2.button("⏹ Stop", key="rc_stop2", width="stretch",
                  on_click=_rc_request, args=("stop",))
    else:  # idle / finished / stopped / budget_exceeded / error / crashed
        prev = stt["args"] or {}
        with st.expander("Run inputs", expanded=False):
            st.text_input("mailbox", prev.get("mailbox",
                          "input/sample/sample_mailbox.mbox"), key="rc_mailbox")
            st.text_input("PBC list", prev.get("pbc",
                          "input/PBC_List_FY2026.pdf"), key="rc_pbc")
            st.text_input("client profile", prev.get("profile",
                          "input/Client_Profile.pdf"), key="rc_profile")
            st.number_input("budget $", value=float(prev.get("budget", 2.0)),
                            min_value=0.1, step=0.5, key="rc_budget")
        c1, c2 = st.columns(2)
        c1.button("▶ Start", key="rc_start", type="primary", width="stretch",
                  help="Resume: processes only emails not already in the tracker",
                  on_click=_rc_start, args=(False,))
        c2.button("🔁 Restart fresh", key="rc_restart", width="stretch",
                  help="Wipes tracker, traces and drafts (keeps the OCR cache), then re-runs everything",
                  on_click=_rc_start, args=(True,))
        if st.session_state.get("rc_error"):
            st.error(st.session_state["rc_error"])

    total = store.total_cost()
    budget_cap = float((stt["args"] or {}).get("budget", 2.0))
    st.metric("Measured LLM cost", f"${total:.4f}")
    st.progress(min(total / budget_cap, 1.0),
                text=f"{total / budget_cap:.0%} of ${budget_cap:.2f} budget")
    for c in store.conn.execute(
            "SELECT model, COUNT(*) n, SUM(cost_usd) c FROM api_calls GROUP BY model"):
        st.caption(f"{c['model']}: {c['n']} calls, ${c['c']:.4f}")


st.sidebar.title("PBC Email Agent")
with st.sidebar:
    run_control_panel()

tab_tracker, tab_trace, tab_drafts, tab_evals = st.tabs(
    ["📋 Tracker", "🔍 Agent trace", "✉️ Follow-up review", "📊 Evals"])

# ------------------------------------------------------------------ tracker
with tab_tracker:
    items = store.all_items()
    if not items:
        st.info("No items yet — start a run from the sidebar.")

    TRACKER_COLS = [0.5, 1.1, 1.7, 1.7, 1.8, 0.9, 2.2, 2.4, 0.8, 3.4]
    hdr = st.columns(TRACKER_COLS, vertical_alignment="bottom")
    for col, name in zip(hdr, ["", "Item", "Status", "Human review", "Category",
                               "Priority", "Latest version", "Source email",
                               "Conf.", "Description"]):
        col.markdown(f"**{name}**" if name else "")
    st.markdown("<hr style='margin:0'>", unsafe_allow_html=True)

    for it in items:
        sel = it["item_id"]
        doc = store.get_document(it["latest_doc_id"]) if it["latest_doc_id"] else None
        email = None
        if it["source_email_id"]:
            email = store.conn.execute(
                "SELECT subject, from_addr FROM emails WHERE email_id=?",
                (it["source_email_id"],)).fetchone()
        review = it["human_review"] or "Unreviewed"

        open_key = f"open_{sel}"
        is_open = st.session_state.get(open_key, False)
        row = st.columns(TRACKER_COLS, vertical_alignment="center")
        if row[0].button("▾" if is_open else "▸", key=f"exp_{sel}",
                         help="Expand row for detail & review"):
            st.session_state[open_key] = not is_open
            st.rerun()
        row[1].markdown(f"**{sel}**")
        row[2].markdown(f"{STATUS_COLORS.get(it['status'], '')} {it['status']}")
        row[3].markdown(f"{REVIEW_ICONS.get(review, '')} {review}")
        row[4].markdown(it["category"] or "")
        row[5].markdown(it["priority"] or "")
        row[6].caption(f"{doc['filename']} (v{doc['version']})" if doc else "")
        row[7].caption(email["subject"] if email else "")
        row[8].markdown(f"{it['confidence']:.2f}" if it["confidence"] else "")
        row[9].caption((it["description"] or "")[:90])

        if not is_open:
            continue
        with st.container(border=True):
            col1, col2 = st.columns(2)
            with col1:
                st.write(it["description"])
                st.caption(f"Acceptance: {it['acceptance']}")
                st.markdown(f"Status: **{it['status']}**" +
                            (f" (confidence {it['confidence']:.2f})" if it["confidence"] else ""))
                if doc:
                    st.caption(f"Latest version: {doc['filename']} (v{doc['version']})")
                if email:
                    st.caption(f"Source email: {email['subject']} — {email['from_addr']}")
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
                        vdoc = store.get_document(v["doc_id"])
                        icon = "🟢" if v["verdict"] == "sufficient" else "🔴"
                        st.markdown(f"{icon} **{v['verdict']}** — "
                                    f"{vdoc['filename'] if vdoc else v['doc_id']} ({ts(v['ts'])})")
                        st.caption(v["rationale"])
                        for c in json.loads(v["criteria"] or "[]"):
                            st.text(f"{'✓' if c['met'] else '✗'} {c['criterion']}: {c['evidence']}")

# ------------------------------------------------------------------ trace
with tab_trace:
    emails = store.conn.execute(
        """SELECT m.*,
                  (SELECT COUNT(*) FROM episodes e WHERE e.email_id = m.email_id) AS n_episodes
           FROM emails m
           WHERE EXISTS (SELECT 1 FROM episodes e WHERE e.email_id = m.email_id)
           ORDER BY m.date""").fetchall()
    options = {
        f"{ts(m['date'])} · {m['subject']} — {m['from_addr']}  [{m['email_id']}]"
        + (f" · {m['n_episodes']} episodes" if m["n_episodes"] > 1 else ""): m["email_id"]
        for m in emails
    }
    if not options:
        st.info("No episodes yet — run `python run.py ...` first.")
    else:
        chosen = st.selectbox("Email", list(options))
        email_id = options[chosen]
        email_row = store.conn.execute(
            "SELECT * FROM emails WHERE email_id=?", (email_id,)).fetchone()
        episodes = store.conn.execute(
            "SELECT * FROM episodes WHERE email_id=? ORDER BY episode_id",
            (email_id,)).fetchall()

        st.markdown(f"**email_id:** `{email_id}`")
        if len(episodes) > 1:
            st.caption(f"{len(episodes)} episodes for this email "
                       f"(includes escalation re-runs)")

        # ---- audit context: the original email, its attachments
        col_mail, col_items = st.columns([3, 2])
        with col_mail:
            st.markdown("**📧 Original email**")
            if email_row is None:
                st.caption("(email not on record)")
            else:
                st.text(f"email_id: {email_row['email_id']}\n"
                        f"From:     {email_row['from_name']} <{email_row['from_addr']}>\n"
                        f"To:       {', '.join(json.loads(email_row['to_addrs'] or '[]'))}\n"
                        f"Date:     {ts(email_row['date'])}\n"
                        f"Subject:  {email_row['subject']}")
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
                                              key=f"pv_{email_id}_{a['sha256'][:8]}")
                            try:
                                with open(a["path"], "rb") as fh:
                                    st.download_button(f"⬇ Download {a['filename']}", fh.read(),
                                                       file_name=a["filename"],
                                                       key=f"dl_{email_id}_{a['sha256'][:8]}")
                            except OSError:
                                pass
        with col_items:
            # Aggregate labels across all episodes for this email
            ep_ids = [ep["episode_id"] for ep in episodes]
            placeholders = ",".join("?" * len(ep_ids))
            st.markdown("**🏷️ Items labeled for this email**")
            updates = [json.loads(t["payload"]) for t in store.conn.execute(
                f"SELECT payload FROM trace WHERE episode_id IN ({placeholders}) "
                "AND kind='tool_call' AND name='update_item_status' ORDER BY seq",
                ep_ids)]
            verifs_ep = {v["item_id"]: v for v in store.conn.execute(
                f"SELECT * FROM verifications WHERE episode_id IN ({placeholders})",
                ep_ids)}
            if not updates and not verifs_ep:
                st.caption("(no tracker changes for this email)")
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
                    st.warning("No verifier verdict for this email — check the guard trace.")
            for iid, v in verifs_ep.items():
                if not any(u["item_id"] == iid for u in updates):
                    st.markdown(f"⚖️ **{iid}** verified "
                                f"({'🟢' if v['verdict'] == 'sufficient' else '🔴'} {v['verdict']}) "
                                "without a status change")
                    st.caption(v["rationale"])

        # ---- one section per episode (escalations show as chained runs)
        for ep in episodes:
            eid = ep["episode_id"]
            cost = store.conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM api_calls WHERE episode_id=?",
                (eid,)).fetchone()
            header = (f"Episode #{eid} · {ep['model']} · "
                      f"{cost['n']} API calls · ${cost['c']:.4f}")
            if ep["escalated_from"]:
                header += f" · escalated from #{ep['escalated_from']}"
            with st.expander(header, expanded=(ep["episode_id"] == episodes[-1]["episode_id"])):
                if ep["summary"]:
                    st.success(f"Outcome: {ep['summary']}")

                st.markdown("**🧵 Step-by-step trace**")
                for t in store.conn.execute(
                        "SELECT * FROM trace WHERE episode_id=? ORDER BY seq", (eid,)):
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
                        # Legacy traces: truncation used to surface as a raw JSONDecodeError
                        # tool error before token_retry events were recorded.
                        if (isinstance(payload, dict) and isinstance(payload.get("error"), str)
                                and ("JSONDecodeError" in payload["error"]
                                     or "Unterminated string" in payload["error"]
                                     or "structured output failed" in payload["error"])):
                            st.warning(
                                f"⚠️ **Token-budget / truncated JSON** on `{t['name']}` — "
                                "model output was cut off mid-string (hit `max_tokens`). "
                                "Current runs auto-retry with **2×** the token budget and "
                                "log `token_retry` events below; this row is a legacy "
                                "error returned to the agent."
                            )
                        with st.expander(f"↩️ {t['name']} — result", expanded=False):
                            if isinstance(payload, (dict, list)):
                                st.json(payload)
                            else:
                                st.text(str(payload)[:3000])
                    elif t["kind"] == "token_retry":
                        p = payload if isinstance(payload, dict) else {}
                        event = p.get("event", "retry")
                        attempt = p.get("attempt", "?")
                        cur = p.get("max_tokens", "?")
                        nxt = p.get("next_max_tokens")
                        reason = p.get("reason", "unknown")
                        detail = p.get("detail", "")
                        out_tok = p.get("output_tokens")
                        out_s = f", output_tokens={out_tok}" if out_tok is not None else ""
                        if event == "retry":
                            st.warning(
                                f"⚠️ **Hit token limit / invalid JSON** on `{t['name']}` "
                                f"(attempt {attempt}): `{reason}` · "
                                f"max_tokens={cur}{out_s}. "
                                f"**Retrying with doubled budget → {nxt}.**"
                            )
                            if detail:
                                st.caption(detail)
                        elif event == "recovered":
                            st.success(
                                f"✅ **Recovered after retry** on `{t['name']}` "
                                f"(attempt {attempt}): parsed cleanly at "
                                f"max_tokens={cur} "
                                f"(started at {p.get('started_max_tokens', '?')})"
                                f"{out_s}."
                            )
                        elif event == "failed":
                            st.error(
                                f"❌ **Exhausted retries** on `{t['name']}` "
                                f"after 3 attempts (last max_tokens={cur}): {detail}"
                            )
                        else:
                            st.info(f"Token budget event on `{t['name']}`: {payload}")
                    elif t["kind"] == "verdict":
                        icon = ("🟢" if isinstance(payload, dict)
                                and payload.get("verdict") == "sufficient" else "🔴")
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

# ------------------------------------------------------------------ evals
with tab_evals:
    from evals.run_evals import evaluate

    c1, c2 = st.columns(2)
    gt_path = c1.text_input("Ground truth", "input/sample/sample_groundtruth.json")
    labels_path = c2.text_input("Labels", "evals/labels.json")
    st.caption("Evals are pure DB reads — no LLM calls, safe to run any time "
               "(mid-run results reflect emails processed so far).")

    if st.button("▶ Run evals", type="primary"):
        try:
            st.session_state["eval_result"] = evaluate(DB, gt_path, labels_path)
        except FileNotFoundError as e:
            st.error(f"File not found: {e}")
        except Exception as e:
            st.error(f"Eval failed: {type(e).__name__}: {e}")

    r = st.session_state.get("eval_result")
    if r:
        s, i, q, c = r["status"], r["insufficiency"], r["sequences"], r["cost"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Status accuracy", f"{s['correct']}/{s['total']}",
                  f"{s['correct'] / s['total']:.0%}", delta_color="off")
        m2.metric("Insufficiency F1", f"{i['f1']:.2f}",
                  f"P {i['precision']:.2f} · R {i['recall']:.2f}", delta_color="off")
        m3.metric("Tool sequences", f"{q['matched']}/{q['total']}",
                  f"{(q['matched'] / q['total']):.0%}" if q["total"] else "—",
                  delta_color="off")
        m4.metric("Measured cost", f"${c['total_usd']:.4f}",
                  f"{c['escalations']} escalation(s)", delta_color="off")

        st.subheader("Per-status precision / recall")
        st.dataframe(pd.DataFrame([
            {"Status": cls, "Precision": round(m["precision"], 2),
             "Recall": round(m["recall"], 2), "F1": round(m["f1"], 2),
             "Support": m["support"]}
            for cls, m in s["classes"].items()]),
            width="stretch", hide_index=True)

        if s["mismatches"]:
            st.subheader("Status mismatches")
            st.dataframe(pd.DataFrame(s["mismatches"]).rename(columns={
                "item_id": "Item", "expected": "Expected", "predicted": "Predicted"}),
                width="stretch", hide_index=True)
        else:
            st.success("All item statuses match ground truth.")

        st.subheader("Tool-sequence match per email")
        for row in q["rows"]:
            head = (f"{'✅' if row['ok'] else '❌'} [{row['date']}] {row['note']}")
            with st.expander(head, expanded=not row["ok"]):
                st.caption(f"Email: {row['subject']}")
                st.text("required: " + " → ".join(row["required"]))
                if row["no_episode"]:
                    st.warning("No episode recorded for this email.")
                else:
                    st.text("actual:   " + " → ".join(row["actual"]))
                if row["forbidden_hit"]:
                    st.error(f"Forbidden tool(s) used: {', '.join(row['forbidden_hit'])}")

        st.subheader("Cost by model")
        st.dataframe(pd.DataFrame([
            {"Model": m["model"], "Calls": m["n"], "USD": round(m["c"], 4)}
            for m in c["by_model"]]), width="stretch", hide_index=True)
