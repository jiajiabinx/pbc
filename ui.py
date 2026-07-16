"""Streamlit UI: Tracker view · Agent trace view · Follow-up review (send mocked).

    streamlit run ui.py [-- --db data/pbc.db]
"""
from __future__ import annotations

import json
import os
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
    s = Store(DB)
    s.ensure_default_admin()  # Create default admin if no users exist
    return s


store = get_store()

# ------------------------------------------------------------------ auth
def init_auth_state():
    """Initialize authentication state."""
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
        st.session_state.user = None


def login_form():
    """Display login form and handle authentication."""
    st.title("🔐 PBC Tracker Login")
    
    tab_login, tab_register = st.tabs(["Login", "Register"])
    
    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", type="primary")
            
            if submitted:
                if username and password:
                    user = store.authenticate_user(username, password)
                    if user:
                        st.session_state.authenticated = True
                        st.session_state.user = user
                        st.rerun()
                    else:
                        st.error("Invalid username or password")
                else:
                    st.warning("Please enter username and password")
        
        st.caption("Default credentials: admin / admin")
    
    with tab_register:
        with st.form("register_form"):
            new_username = st.text_input("Username", key="reg_user")
            new_display = st.text_input("Display name", key="reg_display",
                                        placeholder="How your name appears to others")
            new_password = st.text_input("Password", type="password", key="reg_pass")
            new_password2 = st.text_input("Confirm password", type="password", key="reg_pass2")
            reg_submitted = st.form_submit_button("Register")
            
            if reg_submitted:
                if not new_username or not new_password:
                    st.warning("Username and password are required")
                elif new_password != new_password2:
                    st.error("Passwords do not match")
                elif len(new_password) < 4:
                    st.error("Password must be at least 4 characters")
                else:
                    try:
                        store.create_user(new_username, new_password, 
                                         new_display or new_username, "reviewer")
                        st.success(f"Account created! You can now login as '{new_username}'")
                    except ValueError as e:
                        st.error(str(e))


def logout():
    """Log out the current user."""
    st.session_state.authenticated = False
    st.session_state.user = None
    st.rerun()


init_auth_state()

# Show login form if not authenticated
if not st.session_state.authenticated:
    login_form()
    st.stop()

# User is authenticated - get current user
current_user = st.session_state.user

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
                     fresh=fresh,
                     api_key=(st.session_state.get("rc_api_key") or "").strip())
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
            st.text_input(
                "Anthropic API key", key="rc_api_key", type="password",
                placeholder=("using ANTHROPIC_API_KEY from the environment"
                             if os.environ.get("ANTHROPIC_API_KEY")
                             or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                             else "sk-ant-…"),
                help="Passed to the runner's environment only — never stored. "
                     "Leave empty to use the key Streamlit was launched with.")
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
    # User info and logout
    st.markdown(f"**👤 {current_user['display_name']}** ({current_user['role']})")
    if st.button("🚪 Logout", key="logout_btn"):
        logout()
    
    # Admin: user management
    if current_user["role"] == "admin":
        with st.expander("👥 Manage users"):
            users = store.list_users()
            st.caption(f"{len(users)} registered user(s)")
            for u in users:
                st.text(f"{u['display_name']} (@{u['username']}) - {u['role']}")
            
            st.markdown("**Add new user**")
            new_u = st.text_input("Username", key="admin_new_user")
            new_d = st.text_input("Display name", key="admin_new_display")
            new_p = st.text_input("Password", type="password", key="admin_new_pass")
            new_r = st.selectbox("Role", ["reviewer", "lead", "admin"], key="admin_new_role")
            if st.button("Create user", key="admin_create_user"):
                if new_u and new_p:
                    try:
                        store.create_user(new_u, new_p, new_d or new_u, new_r)
                        st.success(f"Created user '{new_u}'")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
                else:
                    st.warning("Username and password required")
    
    st.divider()
    run_control_panel()

tab_tracker, tab_trace, tab_drafts, tab_history, tab_evals = st.tabs(
    ["📋 Tracker", "🔍 Agent trace", "✉️ Follow-up review", "📜 History", "📊 Evals"])

# ------------------------------------------------------------------ tracker
with tab_tracker:
    items = store.all_items()
    all_reviewers = store.list_users()
    
    if not items:
        st.info("No items yet — start a run from the sidebar.")

    TRACKER_COLS = [0.5, 1.1, 1.7, 2.5, 1.5, 0.9, 2.0, 2.0, 0.8, 3.0]
    hdr = st.columns(TRACKER_COLS, vertical_alignment="bottom")
    for col, name in zip(hdr, ["", "Item", "Status", "Reviewer Status", "Category",
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
        
        # Get per-reviewer status
        item_reviews = store.get_item_reviews(sel)
        reviews_by_user = {r["user_id"]: r for r in item_reviews}
        
        # Build reviewer status summary
        approved_count = sum(1 for r in item_reviews if r["review"] == "Approved")
        rejected_count = sum(1 for r in item_reviews if r["review"] == "Rejected")
        pending_count = len(all_reviewers) - len(item_reviews)
        
        # Current user's review
        my_review = reviews_by_user.get(current_user["user_id"], {})
        my_review_status = my_review.get("review", "Unreviewed")

        open_key = f"open_{sel}"
        is_open = st.session_state.get(open_key, False)
        row = st.columns(TRACKER_COLS, vertical_alignment="center")
        if row[0].button("▾" if is_open else "▸", key=f"exp_{sel}",
                         help="Expand row for detail & review"):
            st.session_state[open_key] = not is_open
            st.rerun()
        row[1].markdown(f"**{sel}**")
        row[2].markdown(f"{STATUS_COLORS.get(it['status'], '')} {it['status']}")
        # Show reviewer status summary with icons
        reviewer_summary = f"✅{approved_count} ❌{rejected_count} ⏳{pending_count}"
        row[3].markdown(f"{REVIEW_ICONS.get(my_review_status, '')} You: {my_review_status}\n\n{reviewer_summary}")
        row[4].markdown(it["category"] or "")
        row[5].markdown(it["priority"] or "")
        row[6].caption(f"{doc['filename']} (v{doc['version']})" if doc else "")
        row[7].caption(email["subject"] if email else "")
        row[8].markdown(f"{it['confidence']:.2f}" if it["confidence"] else "")
        row[9].caption((it["description"] or "")[:90])

        if not is_open:
            continue
        with st.container(border=True):
            col1, col2, col3 = st.columns([2, 2, 2])
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

                st.markdown(f"**👤 Your review ({current_user['display_name']})**")
                choice = st.radio("Review", ["Unreviewed", "Approved", "Rejected"],
                                  index=["Unreviewed", "Approved", "Rejected"].index(my_review_status),
                                  horizontal=True, key=f"rev_{sel}",
                                  label_visibility="collapsed")
                note = st.text_input("Review note", my_review.get("note", "") or "", key=f"revnote_{sel}",
                                     placeholder="e.g. checked source doc, agree with Insufficient")
                if st.button("Save my review", key=f"revsave_{sel}"):
                    store.set_user_review(sel, current_user["user_id"], choice, note)
                    st.toast(f"{sel} marked {choice}")
                    st.rerun()
                if my_review.get("reviewed_at"):
                    st.caption(f"Your last review: {ts(my_review['reviewed_at'])}")
                
                # Show all reviewer statuses
                st.markdown("**👥 All reviewer statuses**")
                for reviewer in all_reviewers:
                    r = reviews_by_user.get(reviewer["user_id"])
                    if r:
                        icon = REVIEW_ICONS.get(r["review"], "□")
                        reviewer_line = f"{icon} **{reviewer['display_name']}**: {r['review']}"
                        if r.get("note"):
                            reviewer_line += f" — _{r['note']}_"
                        st.markdown(reviewer_line)
                    else:
                        st.markdown(f"□ **{reviewer['display_name']}**: _pending_")
                        
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
            
            with col3:
                # Evidence preview
                st.markdown("**📄 Evidence preview**")
                if doc:
                    st.caption(f"Showing: {doc['filename']}")
                    render_attachment(doc["path"], doc["filename"], key=f"evidence_{sel}")
                    if Path(doc["path"]).exists():
                        try:
                            with open(doc["path"], "rb") as fh:
                                st.download_button(f"⬇ Download {doc['filename']}", fh.read(),
                                                   file_name=doc["filename"],
                                                   key=f"dl_evidence_{sel}")
                        except OSError:
                            pass
                else:
                    st.caption("No evidence document linked to this item yet.")

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

# ------------------------------------------------------------------ history
with tab_history:
    st.markdown("""**Run History** — Previous runs are archived here when you click 
    "🔁 Restart fresh". Inspect past agent traces, item states, and cost breakdowns.""")
    
    history = store.get_run_history()
    if not history:
        st.info("No archived runs yet. Previous runs will appear here after you "
                "click '🔁 Restart fresh' in the sidebar.")
    else:
        # Run selector
        run_options = {
            f"Run #{r['run_id']} — {ts(r['started_at'])} — {r['status']} — "
            f"{json.loads(r['summary'] or '{}').get('total_emails', '?')} emails, "
            f"${json.loads(r['summary'] or '{}').get('total_cost_usd', 0):.4f}": r["run_id"]
            for r in history
        }
        selected_label = st.selectbox("Select archived run", list(run_options.keys()))
        selected_run_id = run_options[selected_label]
        
        # Delete button
        col_del, col_space = st.columns([1, 4])
        if col_del.button("🗑️ Delete this run", key=f"del_run_{selected_run_id}"):
            store.delete_run_history(selected_run_id)
            st.toast(f"Deleted run #{selected_run_id}")
            st.rerun()
        
        snapshot = store.get_run_snapshot(selected_run_id)
        if snapshot:
            summary = snapshot["summary"]
            
            # Summary metrics
            st.subheader("Run summary")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Emails processed", summary.get("total_emails", 0))
            m2.metric("Episodes", summary.get("total_episodes", 0))
            m3.metric("Escalations", summary.get("escalations", 0))
            m4.metric("Total cost", f"${summary.get('total_cost_usd', 0):.4f}")
            
            run_args = summary.get("run_args") or {}
            if run_args:
                st.caption(f"Inputs: mailbox={run_args.get('mailbox')}, "
                           f"pbc={run_args.get('pbc')}, profile={run_args.get('profile')}, "
                           f"budget=${run_args.get('budget', 2.0):.2f}")
            st.caption(f"Started: {ts(snapshot['started_at'])} — "
                       f"Ended: {ts(snapshot['ended_at'])} — "
                       f"Status: {snapshot['status']}")
            
            # Tabs for different aspects of the archived run
            hist_tab_items, hist_tab_trace, hist_tab_cost = st.tabs(
                ["📋 Items snapshot", "🔍 Trace archive", "💰 Cost breakdown"])
            
            with hist_tab_items:
                items = snapshot.get("items", [])
                if not items:
                    st.info("No items in this run.")
                else:
                    # Show items as a table
                    df_items = pd.DataFrame([{
                        "Item": it["item_id"],
                        "Status": f"{STATUS_COLORS.get(it['status'], '')} {it['status']}",
                        "Human review": f"{REVIEW_ICONS.get(it.get('human_review', 'Unreviewed'), '')} {it.get('human_review', 'Unreviewed')}",
                        "Category": it.get("category", ""),
                        "Priority": it.get("priority", ""),
                        "Confidence": f"{it['confidence']:.2f}" if it.get("confidence") else "",
                        "Rationale": (it.get("rationale") or "")[:80],
                    } for it in items])
                    st.dataframe(df_items, width="stretch", hide_index=True)
                    
                    # Expandable detail for each item
                    for it in items:
                        with st.expander(f"{STATUS_COLORS.get(it['status'], '')} {it['item_id']} — {it['status']}"):
                            st.write(it.get("description", ""))
                            st.caption(f"Acceptance: {it.get('acceptance', '')}")
                            if it.get("rationale"):
                                st.info(it["rationale"])
                            if it.get("human_note"):
                                st.caption(f"Reviewer note: {it['human_note']}")
            
            with hist_tab_trace:
                episodes = snapshot.get("episodes", [])
                if not episodes:
                    st.info("No episodes in this run.")
                else:
                    # Episode selector
                    ep_options = {
                        f"Episode #{ep['episode_id']} — {ep['model']} — {ep.get('summary', '')[:50]}"
                        + (f" (escalated from #{ep['escalated_from']})" if ep.get('escalated_from') else ""): i
                        for i, ep in enumerate(episodes)
                    }
                    selected_ep_label = st.selectbox("Select episode", list(ep_options.keys()),
                                                     key=f"hist_ep_{selected_run_id}")
                    ep_idx = ep_options[selected_ep_label]
                    ep = episodes[ep_idx]
                    
                    st.markdown(f"**Email ID:** `{ep['email_id']}`")
                    st.markdown(f"**Model:** {ep['model']}")
                    if ep.get("summary"):
                        st.success(f"Outcome: {ep['summary']}")
                    
                    # Show traces
                    st.markdown("**🧵 Step-by-step trace**")
                    for t in ep.get("traces", []):
                        payload = t["payload"]
                        try:
                            payload = json.loads(payload) if isinstance(payload, str) else payload
                        except (TypeError, json.JSONDecodeError):
                            pass
                        
                        if t["kind"] == "plan":
                            st.markdown("**📌 Plan** — classification: "
                                        f"`{payload.get('classification', '?') if isinstance(payload, dict) else '?'}`")
                            if isinstance(payload, dict):
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
                        elif t["kind"] == "token_retry":
                            p = payload if isinstance(payload, dict) else {}
                            event = p.get("event", "retry")
                            if event == "retry":
                                st.warning(f"⚠️ **Token retry** on `{t['name']}` — {p.get('reason', 'unknown')}")
                            elif event == "recovered":
                                st.success(f"✅ **Recovered** on `{t['name']}`")
                            elif event == "failed":
                                st.error(f"❌ **Failed** on `{t['name']}` — {p.get('detail', '')}")
                    
                    # Show verifications for this episode
                    verifs = ep.get("verifications", [])
                    if verifs:
                        st.markdown("**⚖️ Verifications**")
                        for v in verifs:
                            icon = "🟢" if v["verdict"] == "sufficient" else "🔴"
                            st.markdown(f"{icon} **{v['item_id']}** — {v['verdict']} "
                                        f"(conf {v['confidence']:.2f})")
                            st.caption(v.get("rationale", ""))
            
            with hist_tab_cost:
                api_calls = snapshot.get("api_calls", [])
                if not api_calls:
                    st.info("No API calls recorded.")
                else:
                    # Summary by model
                    st.markdown("**Cost by model**")
                    df_model = pd.DataFrame(api_calls).groupby("model").agg({
                        "cost_usd": "sum",
                        "input_tokens": "sum",
                        "output_tokens": "sum",
                        "cache_read_tokens": "sum",
                        "cache_write_tokens": "sum",
                    }).reset_index()
                    df_model["calls"] = pd.DataFrame(api_calls).groupby("model").size().values
                    df_model = df_model.rename(columns={
                        "model": "Model", "cost_usd": "Cost $", "calls": "Calls",
                        "input_tokens": "Input tok", "output_tokens": "Output tok",
                        "cache_read_tokens": "Cache read", "cache_write_tokens": "Cache write"
                    })
                    st.dataframe(df_model, width="stretch", hide_index=True)
                    
                    # Summary by purpose
                    st.markdown("**Cost by purpose**")
                    df_purpose = pd.DataFrame(api_calls).groupby("purpose").agg({
                        "cost_usd": "sum"
                    }).reset_index().sort_values("cost_usd", ascending=False)
                    df_purpose["calls"] = pd.DataFrame(api_calls).groupby("purpose").size().values
                    df_purpose = df_purpose.rename(columns={
                        "purpose": "Purpose", "cost_usd": "Cost $", "calls": "Calls"
                    })
                    st.dataframe(df_purpose, width="stretch", hide_index=True)
                    
                    # Full call log
                    with st.expander("Full API call log"):
                        df_calls = pd.DataFrame(api_calls)[[
                            "model", "purpose", "input_tokens", "output_tokens",
                            "cache_read_tokens", "cache_write_tokens", "cost_usd"
                        ]]
                        st.dataframe(df_calls, width="stretch", hide_index=True)

# ------------------------------------------------------------------ evals
BENCH_BADGES = {
    "idle": "⚪ idle", "launching": "🚀 launching…", "running": "🏃 running",
    "finished": "✅ finished", "stopped": "⏹️ stopped",
    "error": "🔥 error", "crashed": "💀 crashed",
}


def _bench_start() -> None:
    st.session_state.pop("bm_error", None)
    try:
        runctl.bench_start(
            store, int(st.session_state.bm_runs), st.session_state.bm_mailbox,
            st.session_state.bm_pbc, st.session_state.bm_profile,
            budget=st.session_state.bm_budget,
            groundtruth=st.session_state.get("ev_gt", "input/sample/sample_groundtruth.json"),
            labels=st.session_state.get("ev_labels", "evals/labels.json"),
            api_key=(st.session_state.get("rc_api_key") or "").strip())
    except RuntimeError as e:
        st.session_state["bm_error"] = str(e)


def _bench_stop() -> None:
    runctl.bench_stop(store)


@st.fragment(run_every=2)
def benchmark_panel() -> None:
    bs = runctl.bench_state(store)
    status = bs["status"]
    st.markdown(f"**Benchmark:** {BENCH_BADGES.get(status, status)}")
    prog = bs["progress"]
    if status in ("launching", "running") and prog.get("total"):
        st.progress(min(prog.get("done", 0) / prog["total"], 1.0),
                    text=f"{prog.get('done', 0)}/{prog['total']} runs complete")
        run = bs.get("run") or {}
        if run.get("total"):
            st.caption(f"current run: email {run.get('done', 0)}/{run['total']} · "
                       f"{(run.get('current') or '')[:38]} · "
                       f"${run.get('cost_usd', 0):.4f} so far")
    if status == "error" and bs["error"]:
        st.error(bs["error"])
    if status == "crashed":
        st.warning("Benchmark process died — see data/benchmark.log")

    if status in ("launching", "running"):
        st.button("⏹ Stop benchmark", key="bm_stop", on_click=_bench_stop)
        st.caption("Stop takes effect after the in-flight run finishes.")
    else:
        with st.expander("Benchmark inputs", expanded=False):
            st.number_input("sample runs", min_value=1, max_value=20, value=3,
                            step=1, key="bm_runs")
            st.text_input("mailbox", "input/sample/sample_mailbox.mbox", key="bm_mailbox")
            st.text_input("PBC list", "input/PBC_List_FY2026.pdf", key="bm_pbc")
            st.text_input("client profile", "input/Client_Profile.pdf", key="bm_profile")
            st.number_input("budget $ per run", value=2.0, min_value=0.1, step=0.5,
                            key="bm_budget")
        st.button("🏁 Run benchmark", key="bm_start", type="primary",
                  on_click=_bench_start)
        st.caption("Each run is a fresh agent pass over the mailbox on a scratch DB "
                   "(data/benchmark.db) — the live tracker is untouched, and every "
                   "run is real API spend. Uses the ground truth / labels paths "
                   "above and the API key from the sidebar (or the environment).")
        if st.session_state.get("bm_error"):
            st.error(st.session_state["bm_error"])

    if bs["results"]:
        st.dataframe(pd.DataFrame([
            {"Run": r["run"], "Status acc": f"{r['status_accuracy']:.0%}",
             "Insuff. F1": round(r["insufficiency_f1"], 2),
             "Tool seq": f"{r['sequence_match']:.0%}",
             "Cost $": round(r["cost_usd"], 4),
             "API calls": r["api_calls"], "Escalations": r["escalations"]}
            for r in bs["results"]]), width="stretch", hide_index=True)
    if bs["summary"]:
        sm = bs["summary"]
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Status accuracy", f"{sm['status_accuracy']['mean']:.0%}",
                  f"± {sm['status_accuracy']['stdev']:.0%}", delta_color="off")
        a2.metric("Insufficiency F1", f"{sm['insufficiency_f1']['mean']:.2f}",
                  f"± {sm['insufficiency_f1']['stdev']:.2f}", delta_color="off")
        a3.metric("Tool-seq match", f"{sm['sequence_match']['mean']:.0%}",
                  f"± {sm['sequence_match']['stdev']:.0%}", delta_color="off")
        a4.metric("Cost / run", f"${sm['cost_usd']['mean']:.4f}",
                  f"± ${sm['cost_usd']['stdev']:.4f}", delta_color="off")
        st.caption(f"Mean ± stdev over {len(bs['results'])} run(s).")


with tab_evals:
    from evals.run_evals import evaluate

    c1, c2 = st.columns(2)
    gt_path = c1.text_input("Ground truth", "input/sample/sample_groundtruth.json",
                            key="ev_gt")
    labels_path = c2.text_input("Labels", "evals/labels.json", key="ev_labels")
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

                # source email + evidence, so a miss can be judged without
                # leaving the eval page
                em = store.conn.execute(
                    "SELECT body, attachments FROM emails WHERE email_id=?",
                    (row.get("email_id"),)).fetchone()
                if em:
                    st.markdown("**Source email**")
                    st.text((em["body"] or "").strip()[:4000])
                    for j, att in enumerate(json.loads(em["attachments"] or "[]")):
                        st.markdown(f"**Evidence: {att['filename']}**")
                        render_attachment(att["path"], att["filename"],
                                          key=f"ev_att_{row['email_id']}_{j}")
                        if Path(att["path"]).exists():
                            st.download_button(
                                f"⬇ Download {att['filename']}",
                                data=Path(att["path"]).read_bytes(),
                                file_name=att["filename"],
                                key=f"ev_dl_{row['email_id']}_{j}")

        st.subheader("Cost by model")
        st.dataframe(pd.DataFrame([
            {"Model": m["model"], "Calls": m["n"], "USD": round(m["c"], 4)}
            for m in c["by_model"]]), width="stretch", hide_index=True)

    st.divider()
    st.subheader("Benchmark — repeated sample runs")
    benchmark_panel()
