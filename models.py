"""Model routing + cost meter.

The $2/list hard cap drives the routing: the default worker is cheap (Haiku 4.5)
and escalation to bigger models is an explicit tool the agent calls. Every API
call's tokens and USD are logged to the trace; the meter warns at 80% of budget
and raises BudgetExceeded at the cap.
"""
from __future__ import annotations

import json
import sys

import anthropic

# Escalation ladder: agent loop starts on WORKER; the escalate tool moves up.
WORKER = "claude-haiku-4-5"
ESCALATION = ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"]
VERIFIER = "claude-sonnet-5"
VISION = "claude-sonnet-5"
EXTRACTOR = "claude-haiku-4-5"
DRAFTER = "claude-haiku-4-5"
GROUPER = "claude-sonnet-5"

# USD per 1M tokens: (input, output). Cache read = 0.1x input, cache write = 1.25x input.
# Conservative sticker prices (Sonnet 5 has intro pricing through 2026-08-31; we
# budget at sticker so the meter never under-counts).
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}


class BudgetExceeded(Exception):
    pass


def call_cost(model: str, usage) -> float:
    inp, out = PRICES[model]
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        usage.input_tokens * inp
        + cache_read * inp * 0.10
        + cache_write * inp * 1.25
        + usage.output_tokens * out
    ) / 1_000_000


class Router:
    """Single entry point for every LLM call: pricing, logging, budget enforcement."""

    def __init__(self, store, budget_usd: float = 2.00):
        self.client = anthropic.Anthropic()
        self.store = store
        self.budget = budget_usd
        self._warned = False

    def spent(self) -> float:
        return self.store.total_cost()

    def call(self, model: str, *, purpose: str, episode_id: int | None = None,
             max_tokens: int = 2048, **kwargs) -> anthropic.types.Message:
        if self.spent() >= self.budget:
            raise BudgetExceeded(
                f"Budget cap ${self.budget:.2f} reached (spent ${self.spent():.4f})")
        response = self.client.messages.create(model=model, max_tokens=max_tokens, **kwargs)
        usage = response.usage
        cost = call_cost(model, usage)
        self.store.add_api_call(
            episode_id, model, purpose,
            usage.input_tokens, usage.output_tokens,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cost,
        )
        total = self.spent()
        if total >= 0.8 * self.budget and not self._warned:
            self._warned = True
            print(f"[cost] WARNING: at {total / self.budget:.0%} of ${self.budget:.2f} budget",
                  file=sys.stderr)
        return response


def structured_json(router: Router, model: str, *, purpose: str, schema: dict, user: str,
                    system: str | None = None, episode_id: int | None = None,
                    max_tokens: int = 2000, enable_thinking: bool = False) -> dict:
    """Structured-output call that survives token truncation.

    Sonnet 5 runs adaptive thinking by default; thinking counts against
    max_tokens, so a JSON answer can be truncated mid-string ('Unterminated
    string'). We disable thinking for these constrained-judgment calls, join
    every text block (a thinking-only response has none), and retry with a
    doubled budget + brevity reminder on truncation or unparseable JSON.
    Retries are written to the episode trace as kind='token_retry' so the UI
    can show hit-limit → 2× budget explicitly.
    
    Set enable_thinking=True for critical judgment calls (e.g., verification)
    where extended reasoning improves accuracy.
    """
    base_user = user
    kwargs: dict = {
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    if system:
        kwargs["system"] = system
    if model.startswith(("claude-sonnet-5", "claude-opus")):
        if enable_thinking:
            # Enable extended thinking for critical judgment calls
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10000}
        else:
            kwargs["thinking"] = {"type": "disabled"}

    def _trace(payload: dict) -> None:
        store = getattr(router, "store", None)
        if store is not None and episode_id is not None:
            store.add_trace(episode_id, "token_retry", purpose, payload)

    last_err = "no attempt"
    started_at = max_tokens
    for attempt in range(3):
        content = base_user
        if attempt > 0:
            content += (
                "\n\nIMPORTANT: Previous reply was truncated or invalid JSON. "
                "Reply with COMPLETE valid JSON only. Keep rationale and each "
                "evidence string to one short sentence; omit long quotes."
            )
        kwargs["messages"] = [{"role": "user", "content": content}]
        resp = router.call(model, purpose=purpose, episode_id=episode_id,
                           max_tokens=max_tokens, **kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        usage = getattr(resp, "usage", None)
        out_tok = getattr(usage, "output_tokens", None) if usage else None

        if resp.stop_reason == "max_tokens" or not text:
            reason = ("hit_max_tokens" if resp.stop_reason == "max_tokens"
                      else "empty_text")
            next_budget = max_tokens * 2
            last_err = f"truncated at {max_tokens} tokens (stop_reason={resp.stop_reason})"
            _trace({
                "event": "retry",
                "reason": reason,
                "detail": last_err,
                "attempt": attempt + 1,
                "max_tokens": max_tokens,
                "next_max_tokens": next_budget,
                "stop_reason": resp.stop_reason,
                "output_tokens": out_tok,
            })
            max_tokens = next_budget
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            next_budget = max_tokens * 2
            last_err = f"bad JSON: {e}"
            _trace({
                "event": "retry",
                "reason": "unterminated_or_invalid_json",
                "detail": str(e),
                "attempt": attempt + 1,
                "max_tokens": max_tokens,
                "next_max_tokens": next_budget,
                "stop_reason": resp.stop_reason,
                "output_tokens": out_tok,
            })
            max_tokens = next_budget
            continue

        if attempt > 0:
            _trace({
                "event": "recovered",
                "reason": "ok_after_retry",
                "detail": f"parsed on attempt {attempt + 1}",
                "attempt": attempt + 1,
                "max_tokens": max_tokens,
                "started_max_tokens": started_at,
                "stop_reason": resp.stop_reason,
                "output_tokens": out_tok,
            })
        return parsed

    _trace({
        "event": "failed",
        "reason": "exhausted_retries",
        "detail": last_err,
        "attempt": 3,
        "max_tokens": max_tokens,
        "started_max_tokens": started_at,
    })
    raise ValueError(f"structured output failed after 3 attempts ({purpose}): {last_err}")
