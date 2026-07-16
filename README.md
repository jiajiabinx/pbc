# PBC Email Agent

An agent that lives on the audit inbox and keeps the PBC tracker current, with
reasoning traces a partner could defend to a PCAOB inspector.

## Cold run

```bash
pip install -r requirements.txt        # sentence-transformers is optional (auto-fallback)
export ANTHROPIC_API_KEY=sk-ant-...    # or paste a key in the sidebar's run inputs
streamlit run ui.py                    # Tracker · Agent trace · Follow-up review
python evals/run_evals.py              # scores the run against ground truth
python -m pytest tests/                # deterministic tests, no LLM calls
```

Runs are launched from the UI sidebar: set the mailbox / PBC list / client
profile paths (defaults point at the sample set), then **▶ Start** (processes
only emails not already in the tracker) or **🔁 Restart fresh** (wipes tracker,
traces and drafts, keeps the OCR cache). The sidebar shows live status and
progress, with cooperative **⏸ Pause / ⏹ Stop** — they take effect after the
in-flight email finishes so the trace stays consistent.

For headless / scripted runs the CLI still works and writes to the same DB:

```bash
python run.py --mailbox input/sample/sample_mailbox.mbox \
              --pbc input/PBC_List_FY2026.pdf --profile input/Client_Profile.pdf
```

Swap the mailbox / PBC / profile inputs for the held-out set — the PBC list is
the config, parsed at runtime (regex-parsed, with a one-call LLM fallback for
unfamiliar list formats). Nothing engagement-specific is hardcoded.

## Storage backends

Persistence runs on either SQLite or Postgres, chosen from the connection string
(`db.py`):

- **No `DATABASE_URL`** → SQLite at `data/pbc.db` (default for local dev + tests).
- **`DATABASE_URL=postgresql://…`** → Postgres. 

## Deploy to Railway

The app is containerized (`Dockerfile` + `railway.json`); the Streamlit UI is the
web service and it spawns the agent runner as a subprocess in the same container.

1. Push this repo to GitHub and create a Railway project from it (Railway builds
   the `Dockerfile` automatically; it injects `$PORT`).
2. Set service variables:
   - `DATABASE_URL` — your shared Postgres URL (Railway's Postgres plugin sets
     this automatically if you add one; otherwise paste your own).
   - `ANTHROPIC_API_KEY` — or configure a gateway via `ANTHROPIC_BASE_URL`.
3. Deploy. Tables are created on first boot as `pbc_*` — no manual migration.

See `.env.example` for the full variable list. Note: Railway's filesystem is
ephemeral, so saved attachment files under `data/` don't survive redeploys
(tracker state lives in Postgres regardless) — attach a volume at `/app/data` if
you need the raw files to persist. The container image is lean (no
`sentence-transformers`/torch); the matcher falls back to the deterministic
hashed-ngram backend. Add `sentence-transformers>=3.0` to `requirements-prod.txt`
to ship real embeddings.

## Architecture

```
mbox/.eml ──> ingest.py ──> chronological email stream
PBC list PDF ─> runtime config (items + acceptance criteria)   ← swappable
Client profile PDF ─> entity/FY context for verification

for each email (one *episode*):            ┌─ tools.py ───────────────────┐
  agent.py — native tool-use loop          │ submit_plan (forced 1st call)│
    forced plan → model-chosen tool calls  │ register_document, unzip     │
    → done | escalate                      │ parse_pdf, parse_excel,      │
        │                                  │ ocr_image (vision)           │
        ▼                                  │ match_pbc_items (embeddings) │
  store.py (SQLite): tracker rows,         │ extract_fields (citations)   │
  document version lineage, full trace     │ verify_item (separate model) │
  + per-call token/cost ledger             │ update_item_status (guarded) │
        │                                  │ flag_clarification,          │
        ▼                                  │ escalate, done               │
  drafts.py: outstanding items grouped     └──────────────────────────────┘
  per recipient → one draft each
        ▼
  ui.py (Streamlit): Run control (start/pause/stop via runctl.py) ·
  Tracker · Agent trace · Follow-up review (send mocked)
```

**Dynamic control flow, not a pipeline.** Every episode is a
`while stop_reason == "tool_use"` loop (`agent.py`, one file). The only forced
call is the first — `tool_choice` pins `submit_plan`, so every trace opens with
an explicit, auditable plan. After that the model decides per email whether to
parse, OCR, re-classify, verify, escalate, or do nothing. Two similar-looking
emails take different paths: a client message *with* attachments walks
register→parse→match→verify→update; the near-identical "working on it" reply
plans and immediately calls `done`.

## Model routing (cost-driven)

| Step | Model | Why |
|---|---|---|
| Agent loop | Haiku 4.5 ($1/$5 MTok) | Most emails are classify/route/no-op |
| Escalation (`escalate` tool) | Sonnet 5 → Opus 4.8 | Agent re-runs the episode on a bigger model when it judges the email ambiguous |
| Verifier | Sonnet 5 | Independent judgment, separate prompt |
| OCR | Sonnet 5 vision | One call per image, cached by content hash |
| Field extraction | Haiku 4.5, structured outputs | Schema-enforced, citations required |
| Candidate matching | Local MiniLM embeddings | $0, offline, deterministic (hashed-ngram fallback if torch absent) |

The stable system prompt (instructions + PBC config + client profile + tool
schemas) carries a `cache_control` breakpoint, so per-episode input is mostly
0.1× cache reads. Every call's tokens and USD go to the `api_calls` ledger; the
meter warns at 80% of the $2 budget and stops at the cap. Measured cost per
list is printed at the end of every run and shown in the UI sidebar.

## Hallucination guardrails (in code, not prompts)

- `update_item_status` → Received/Insufficient/Complete is **rejected** by the
  store unless a `verify_item` verdict exists for that (item, document).
- The **verifier never sees the agent's claims** — it gets the raw re-parsed
  document, the item's acceptance criteria, and the client profile, and
  re-derives sufficiency (catches Q2-sent-when-Q3-asked, wrong entity,
  3-of-10 confirmations, whiteboard-instead-of-formal-recon).
- Every extracted field carries a page/cell citation, **validated against the
  actual source** (page in range, cell exists) before it's accepted.
- Drafts are generated only from tracker facts, and "send" is mocked behind
  the approve/edit/reject review queue.

## Versioning

Deterministic filename normalization (`AR_Aging_Final_v3_REAL.xlsx` →
`ar_aging::excel`) chains re-sends with a `supersedes` pointer; exact
duplicates are deduped by content hash; zip children keep a `parent_doc_id`.
Pure Python, unit-tested. Deliberately simple.

## Evals

`evals/run_evals.py` scores a run with **no LLM calls**: per-status
precision/recall against `sample_groundtruth.json`, insufficiency-detection
F1, expected tool-call-sequence match (hand-labeled required subsequences +
forbidden tools per email in `evals/labels.json`), and measured cost. The same
scoring runs in the UI's **📊 Evals** tab, where each per-email result expands
to the source email and inline evidence previews (with download).

For variance across runs, `evals/benchmark.py --runs N` (or the Benchmark
section of the Evals tab) repeats N fresh runs on a scratch DB — never the
live tracker — scoring each and reporting mean ± stdev accuracy and cost.
These runs are real API spend, capped per run by `--budget`.
`tests/` covers parsing, versioning lineage, the status guard, cost math, and
the agent loop itself (scripted fake model: forced plan, escalation ladder,
guard errors returned to the model).

## Measured cost

Run `python evals/run_evals.py` after a run — it prints total measured USD and
the per-model breakdown from the ledger. Sample-mailbox runs land well under
the $2 cap (~$0.15–0.35 depending on escalations); see the cost meter in the
UI sidebar for the live figure.

## What breaks at 10 / 100 concurrent audits

**At 10:** SQLite's single-writer model — moves to Postgres with per-audit
schemas; the prompt cache still works because the system prompt is per-
engagement stable. API rate limits are fine (episodes are sequential per
mailbox, parallel across audits).

**At 100:** (1) per-org token rate limits become the bottleneck — needs the
Batches API for the non-latency-sensitive backfill and tier upgrades for live
mail; (2) the shared cost ledger and tracker need real multi-tenancy: row-level
isolation per engagement, per-tenant budgets, and an audit-log store with
retention guarantees; (3) verification volume justifies a dedicated queue with
retries and idempotency keys (episodes are already idempotent per email-id);
(4) embedding the PBC list per engagement stays local/cheap, but OCR volume
warrants caching at the org level (already keyed by content hash here).
