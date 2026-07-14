# PBC Email Agent — Implementation Plan

## Reading of the three key constraints

1. **Python** — plain Python against the Anthropic SDK, no agent framework. The loop lives in one inspectable file (`agent.py`, target ~250 lines), which is what the reviewers said they prefer.
2. **Conversation stream framework** — the mailbox is treated as a chronological event stream. Each email is one *agent episode*: the agent wakes with the current tracker state plus the new email, and decides what to do. State evolves email-by-email exactly as it would on a live inbox, so the same code handles the held-out mailbox cold.
3. **Dynamic tool calls** — no hardcoded pipeline. Every episode is a native tool-use loop (`while stop_reason == "tool_use"`); the model chooses per email whether to parse, OCR, re-classify, verify, escalate to a bigger model, or do nothing. The only forced call is the first one: `tool_choice` forces a `submit_plan` tool so every episode's trace starts with an explicit, auditable plan (requirement #1 in the brief).

## Architecture

```
mbox/.eml ──> ingest.py ──> chronological email stream
PBC_List.pdf ─> parsed into config (items + acceptance criteria)  ← swappable, nothing hardcoded
Client_Profile.pdf ─> entity/FY context for verification

for each email:                        ┌── tools.py ──────────────────┐
  agent.py (single tool-use loop) ───> │ parse_pdf, parse_excel,      │
    plan → tool calls → done           │ ocr_image, unzip,            │
                                       │ match_pbc_items (embeddings),│
        │                              │ extract_fields (citations),  │
        ▼                              │ verify_item (separate model),│
  store.py (SQLite): tracker rows,     │ update_item_status,          │
  document version lineage,            │ register_document, escalate, │
  full trace + per-call cost           │ flag_clarification, done     │
        │                              └──────────────────────────────┘
        ▼
  drafts.py: group outstanding items per recipient → one draft each
        ▼
  ui.py (Streamlit): Tracker view · Agent trace view · Follow-up review (send mocked)
```

## Model routing (the $2/list hard cap drives this)

The README tightens the budget to **$2 per list**, and the brief explicitly requires routing by cost — so the default worker is cheap and escalation is a tool the agent calls when it judges a case ambiguous:

| Step | Model | Why |
|---|---|---|
| Agent loop (per email) | Haiku 4.5 ($1/$5 per MTok) | Most emails are simple; Haiku handles classify/route/no-op |
| Escalation (`escalate` tool) | Sonnet 5 → Opus 4.8 | Agent re-runs the episode on a bigger model when it flags ambiguity — this is also the demo of "two similar emails, different paths" |
| Verifier | Sonnet 5 | Independent judgment, separate prompt, never sees the agent's claim rationale |
| OCR of images/scans | Sonnet 5 vision (single call) | The whiteboard JPG case |
| Field extraction | Haiku 4.5 with structured outputs | Cheap, schema-enforced, citation fields required |
| Candidate matching | Local embeddings (sentence-transformers MiniLM) | $0, deterministic, offline — satisfies "embeddings for matching" without an external API |

The stable system prompt (instructions + PBC config + client profile + tool schemas) gets a `cache_control` breakpoint, so per-episode input is mostly ~0.1× cache reads. Rough estimate for the 90-email held-out set: ~$1.20–1.60, with a cost meter that logs every call's tokens/USD to the trace and warns at 80% of budget.

## Hallucination guardrails (enforced in code, not prompts)

- `update_item_status` **rejects** any move to Received/Insufficient/Complete unless a `verify_item` verdict exists in the trace for that (item, document) pair.
- Every extracted field must carry a citation (page/cell/bbox); the tool validates the cited page or cell actually exists in the source document before accepting it.
- The verifier gets only the evidence + the item's acceptance criteria + client profile — it re-derives sufficiency independently (this is what catches Q2-sent-when-Q3-asked, wrong entity, 3-of-10 confirmations).
- Drafts may only reference tracker facts, and "send" is mocked behind the approve/edit/reject UI.

## Versioning (deliberately simple)

Deterministic normalization (`Final_v3_REAL.xlsx` → semantic key) plus the agent's `register_document` classification; lineage is a chain in SQLite (`supersedes` pointer). Pure Python, covered by unit tests.

## Deliverables mapping

- **Repo**: `agent.py`, `tools.py`, `models.py` (router + cost meter), `ingest.py`, `store.py`, `versioning.py`, `drafts.py`, `embeddings.py`, `ui.py` (Streamlit), `run.py` CLI, `evals/`, `tests/`.
- **Cold run**: `pip install -r requirements.txt && python run.py --mailbox X.mbox --pbc list.pdf --profile profile.pdf` then `streamlit run ui.py`. PBC list is parsed at runtime — swap-safe.
- **Evals**: labels derived from `sample_groundtruth.json` plus hand-labeled expected tool sequences per email; `run_evals.py` reports status precision/recall, insufficiency F1, tool-sequence match, and measured cost.
- **Deterministic tests**: pytest on mbox/PDF parsing, versioning lineage, state transitions, cost accounting — no LLM calls.

## Build order (3 days)

1. **Day 1**: ingest + store + versioning + the agent loop with core tools; run end-to-end on thread01.
2. **Day 2**: verifier, citation-checked extraction, embedding matcher, escalation, drafting, cost meter; full sample run scored against ground truth.
3. **Day 3**: Streamlit UI, eval harness, tests, README (architecture, cost, 10/100-concurrency answer), adversarial hardening (wrong-entity, partial coverage, zip, OCR).

## Decisions taken (flag if you want to switch)

- **UI**: Streamlit — fastest path to the three required views.
- **Embeddings**: local sentence-transformers — zero cost, no second API key.
- **Budget**: the brief says $5/list but the README says $2 — designing to the stricter $2.
