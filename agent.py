"""The agent loop — one inspectable file.

Each email is one *episode*: the agent wakes with the current tracker state plus
the new email, and decides what to do via native tool use. The only forced call
is the first one (`submit_plan`), so every episode's trace starts with an
explicit, auditable plan. Everything after that is the model's choice:

    plan -> tool calls (parse / OCR / match / extract / verify / update) -> done

Escalation is itself a tool: when the worker model (Haiku) judges an email
ambiguous it calls `escalate`, and the episode re-runs from scratch on the next
model in the ladder (Sonnet, then Opus). That is the cost-routing mechanism —
cheap by default, expensive by exception.
"""
from __future__ import annotations

import json

import models
import tools
from store import Store

MAX_TURNS = 24  # hard stop against runaway loops


def build_system(pbc_items: list[dict], pbc_header: str, profile_text: str) -> str:
    """Stable system prompt: instructions + PBC config + client profile.

    This string is identical for every episode and carries the cache_control
    breakpoint, so per-episode input is mostly ~0.1x cache reads.
    """
    item_lines = "\n".join(
        f"{it['item_id']} [{it['category']}] priority={it['priority']}: {it['description']}\n"
        f"    acceptance: {it['acceptance']} | expected: {it['expected_docs']}"
        for it in pbc_items
    )
    return f"""You are the PBC tracking agent on an audit team's shared inbox. Your job: keep the
PBC (prepared-by-client) tracker accurate as emails arrive, with evidence a PCAOB
inspector could defend. You process one email at a time.

RULES
1. Your first tool call is always submit_plan. Classify the email and plan your steps.
   Many emails need no tracker change (acknowledgements, pure requests from the audit
   team, scheduling chatter) — then the plan is short and you call done immediately.
2. When the client sends attachments: register_document each one, inspect it (parse_pdf /
   parse_excel / ocr_image / unzip), use match_pbc_items to find which item(s) it answers,
   then verify_item before any status change. Never decide sufficiency yourself — the
   independent verifier does that, and update_item_status to Received/Insufficient/Complete
   is rejected in code without a verifier verdict.
3. Map statuses: 'Received' = verifier says sufficient (pending audit review);
   'Insufficient' = document(s) arrived but verifier says the item is not satisfied
   (wrong period/entity, partial coverage, informal evidence, unsigned);
   'Requested' = the audit team asked for the item but nothing has arrived;
   'Under review' = evidence arrived but you genuinely cannot conclude yet;
   'Complete' = the audit team explicitly signs the item off in an email.
4. Read the thread context you are given. If the audit team already rejected a document
   or noted missing pieces in a later email you have processed, that matters. Pass such
   facts (facts only, not conclusions) to verify_item via its context argument.
5. When an email from the audit team requests items, set those items to 'Requested'
   (no verifier needed) unless documents already arrived for them.
6. A document can bear on multiple items; verify each separately. One email can also be
   irrelevant to every item — doing nothing is a valid outcome.
7. If the client's message claims to answer an item but something is off that you cannot
   resolve (ambiguous reference, unreadable file, contradictory claims), flag_clarification.
8. escalate when you genuinely cannot classify or handle the email — ambiguous multi-item
   claims, suspected wrong-entity documents, contradictions between sender claims and
   document content. Escalation re-runs the whole email on a stronger model. Use sparingly:
   most emails are simple.
9. Finish every episode with done and a one-line summary.

PBC LIST (config version — this is the engagement config, parsed at runtime):
{pbc_header}

ITEMS:
{item_lines}

CLIENT PROFILE (defines what 'correct' means for entity/period checks):
{profile_text}
"""


def episode_message(store: Store, email, attachments: dict) -> str:
    att_lines = "\n".join(
        f"  {ref}: {att.filename} ({att.size} bytes)" for ref, att in attachments.items()
    ) or "  (none)"
    return f"""TRACKER STATE:
{store.tracker_summary()}

NEW EMAIL:
From: {email.from_name} <{email.from_addr}>
To: {', '.join(email.to_addrs)}
Subject: {email.subject}
Date: {email.date}
Attachments:
{att_lines}

BODY:
{email.body}

Process this email. Start with submit_plan."""


def run_episode(store: Store, router: models.Router, matcher, profile_text: str,
                system: str, email, *, model: str = models.WORKER,
                escalated_from: int | None = None) -> dict:
    """Run one email through the tool-use loop on one model. Returns episode result."""
    episode_id = store.start_episode(email.email_id, model, escalated_from)
    attachments = {f"att_{i + 1}": a for i, a in enumerate(email.attachments)}
    ctx = tools.ToolContext(store=store, router=router, matcher=matcher,
                            profile_text=profile_text, email=email, episode_id=episode_id,
                            attachments=attachments)

    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": episode_message(store, email, attachments)}]
    # Force an explicit plan as the first call of every episode (auditable trace).
    tool_choice = {"type": "tool", "name": "submit_plan"}
    summary, outcome = "", "completed"

    for _turn in range(MAX_TURNS):
        response = router.call(
            model, purpose="agent-loop", episode_id=episode_id, max_tokens=2000,
            system=system_blocks, messages=messages,
            tools=tools.TOOL_SCHEMAS, tool_choice=tool_choice,
        )
        tool_choice = {"type": "auto"}  # only the first call is forced

        for block in response.content:
            if block.type == "text" and block.text.strip():
                store.add_trace(episode_id, "text", "assistant", block.text)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            outcome = "ended_without_done"
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        finished = escalation = None
        for tu in tool_uses:
            if tu.name != "submit_plan":  # submit_plan traces itself as the 'plan' entry
                store.add_trace(episode_id, "tool_call", tu.name, tu.input)
            try:
                result = tools.dispatch(ctx, tu.name, tu.input)
            except tools.Escalate as e:
                escalation = e
                result = "escalating"
            except tools.Done as e:
                finished = e
                result = "done"
            if tu.name != "submit_plan":  # plan already traced with full payload
                store.add_trace(episode_id, "tool_result", tu.name,
                                result if len(str(result)) < 4000 else str(result)[:4000])
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})
        messages.append({"role": "user", "content": results})

        if escalation is not None:
            store.add_trace(episode_id, "escalation", model, escalation.reason)
            store.end_episode(episode_id, f"escalated: {escalation.reason}")
            ladder = models.ESCALATION
            nxt = ladder[min(ladder.index(model) + 1, len(ladder) - 1)] if model in ladder else ladder[-1]
            if nxt == model:  # already at the top — keep going here instead
                continue
            return run_episode(store, router, matcher, profile_text, system, email,
                               model=nxt, escalated_from=episode_id)
        if finished is not None:
            summary, outcome = finished.summary, "completed"
            break
    else:
        outcome = "max_turns"

    store.end_episode(episode_id, summary or outcome)
    return {"episode_id": episode_id, "model": model, "outcome": outcome, "summary": summary}


def run_mailbox(store: Store, router: models.Router, matcher, profile_text: str,
                pbc_items: list[dict], pbc_header: str, emails: list) -> list[dict]:
    """Process the chronological email stream, one episode per email."""
    system = build_system(pbc_items, pbc_header, profile_text)
    results = []
    for i, email in enumerate(emails, 1):
        print(f"[{i}/{len(emails)}] {email.date} | {email.from_addr} | {email.subject}")
        store.add_email(email.__dict__ | {"to_addrs": email.to_addrs})
        try:
            res = run_episode(store, router, matcher, profile_text, system, email)
        except models.BudgetExceeded as e:
            print(f"  !! {e}")
            results.append({"email_id": email.email_id, "outcome": "budget_exceeded"})
            break
        print(f"  -> {res['model']} | {res['outcome']} | {res['summary']}"
              f" | total ${router.spent():.4f}")
        results.append(res | {"email_id": email.email_id})
    return results
