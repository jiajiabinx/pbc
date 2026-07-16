"""CLI entry point — cold run from a clean checkout:

    python run.py --mailbox input/sample/sample_mailbox.mbox \\
                  --pbc input/PBC_List_FY2026.pdf --profile input/Client_Profile.pdf
    streamlit run ui.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import agent
import drafts as drafts_mod
import embeddings
import ingest
import models
from store import Store


def llm_pbc_fallback(router: models.Router):
    """If the swapped-in PBC list doesn't match the regex format, one Sonnet call parses it."""
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in
                           ("item_id", "category", "priority", "description",
                            "acceptance", "expected_docs")},
            "required": ["item_id", "description"],
            "additionalProperties": False,
        }}},
        "required": ["items"], "additionalProperties": False,
    }

    def _fallback(text: str) -> list[dict]:
        resp = router.call(
            "claude-sonnet-5", purpose="pbc-list-parse", max_tokens=8000,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content":
                "Extract every requested line item from this PBC list document as structured "
                "data. Preserve ids, categories, priorities, acceptance criteria and expected "
                f"document types verbatim where present.\n\n{text[:40000]}"}],
        )
        items = json.loads(next(b.text for b in resp.content if b.type == "text"))["items"]
        for it in items:
            for k in ("category", "priority", "acceptance", "expected_docs"):
                it.setdefault(k, "")
        return items

    return _fallback


def main() -> int:
    ap = argparse.ArgumentParser(description="PBC email agent")
    ap.add_argument("--mailbox", required=True, help=".mbox file or directory of .eml files")
    ap.add_argument("--pbc", required=True, help="PBC list PDF (the engagement config)")
    ap.add_argument("--profile", required=True, help="Client profile PDF")
    ap.add_argument("--db", default="data/pbc.db")
    ap.add_argument("--budget", type=float, default=2.00, help="hard USD cap per run")
    ap.add_argument("--fresh", action="store_true", help="delete the DB and start over")
    ap.add_argument("--no-drafts", action="store_true", help="skip follow-up drafting")
    args = ap.parse_args()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) is not set.", file=sys.stderr)
        return 2

    if args.fresh and Path(args.db).exists():
        Path(args.db).unlink()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    store = Store(args.db)
    router = models.Router(store, budget_usd=args.budget)

    # advertise this run to the UI (see runctl.py)
    store.set_meta("run_pid", str(os.getpid()))
    store.set_meta("run_status", "running")
    store.set_meta("run_error", "")
    store.set_meta("run_args", json.dumps(
        {"mailbox": args.mailbox, "pbc": args.pbc, "profile": args.profile,
         "budget": args.budget}))
    if store.get_meta("run_control") not in ("run", "pause"):
        store.set_meta("run_control", "run")

    try:
        pbc_items, pbc_header = ingest.parse_pbc_list(args.pbc,
                                                      llm_fallback=llm_pbc_fallback(router))
        print(f"PBC list: {len(pbc_items)} items parsed")
        store.load_items(pbc_items)
        store.set_meta("pbc_header", pbc_header)

        profile_text = ingest.load_profile(args.profile)
        store.set_meta("profile", profile_text)

        matcher = embeddings.ItemMatcher(pbc_items)
        print(f"Embeddings backend: {matcher.backend}")

        emails = ingest.load_mailbox(args.mailbox)
        already = {r["email_id"] for r in store.conn.execute(
            "SELECT email_id FROM emails WHERE processed_at IS NOT NULL")}
        todo = [e for e in emails if e.email_id not in already]
        print(f"Mailbox: {len(emails)} emails ({len(todo)} unprocessed)")

        results = agent.run_mailbox(store, router, matcher, profile_text,
                                    pbc_items, pbc_header, todo)
        stopped = store.get_meta("run_status") == "stopped"
        exceeded = any(r.get("outcome") == "budget_exceeded" for r in results)

        if not args.no_drafts and not stopped and not exceeded:
            store.set_meta("run_status", "drafting")
            try:
                ids = drafts_mod.generate_drafts(store, router, profile_text)
                print(f"Drafted {len(ids)} follow-up email(s) — review in the UI")
            except models.BudgetExceeded as e:
                exceeded = True
                print(f"Skipping drafts: {e}", file=sys.stderr)

        store.set_meta("run_status",
                       "stopped" if stopped else
                       "budget_exceeded" if exceeded else "finished")
    except Exception as e:
        store.set_meta("run_status", "error")
        store.set_meta("run_error", f"{type(e).__name__}: {e}")
        raise

    print(f"\nTotal measured cost: ${store.total_cost():.4f} (budget ${args.budget:.2f})")
    print("Tracker:")
    for it in store.all_items():
        print(f"  {it['item_id']}: {it['status']}")
    print("\nNext: streamlit run ui.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
