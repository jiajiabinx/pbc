"""Model routing + cost meter via OpenRouter.

OpenRouter provides live pricing in API responses — no hardcoded prices needed.
The $2/list hard cap drives the routing: the default worker is cheap (Haiku)
and escalation to bigger models is an explicit tool the agent calls. Every API
call's tokens and USD are logged to the trace; the meter warns at 80% of budget
and raises BudgetExceeded at the cap.
"""
from __future__ import annotations

import json
import os
import sys

import httpx

# OpenRouter model names
WORKER = "anthropic/claude-3-5-haiku-20241022"
ESCALATION = [
    "anthropic/claude-3-5-haiku-20241022",
    "anthropic/claude-sonnet-4-20250514",
    "anthropic/claude-opus-4-20250514",
]
VERIFIER = "anthropic/claude-sonnet-4-20250514"
VISION = "anthropic/claude-sonnet-4-20250514"
EXTRACTOR = "anthropic/claude-3-5-haiku-20241022"
DRAFTER = "anthropic/claude-3-5-haiku-20241022"
GROUPER = "anthropic/claude-sonnet-4-20250514"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class BudgetExceeded(Exception):
    pass


class OpenRouterClient:
    """Minimal OpenRouter client that extracts live cost from responses."""
    
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY not set")
        self.client = httpx.Client(
            base_url=OPENROUTER_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/pbc-agent",  # Optional, for OpenRouter rankings
                "X-Title": "PBC Email Agent",
            },
            timeout=120.0,
        )
    
    def messages_create(self, model: str, max_tokens: int, messages: list,
                        system: str | None = None, tools: list | None = None,
                        tool_choice: dict | None = None,
                        response_format: dict | None = None,
                        **kwargs) -> dict:
        """Create a message, returns dict with content, usage, and cost."""
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            # OpenRouter uses system in messages array
            payload["messages"] = [{"role": "system", "content": system}] + messages
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if response_format:
            payload["response_format"] = response_format
        
        # Handle thinking/extended thinking if specified
        thinking = kwargs.get("thinking")
        if thinking and thinking.get("type") == "enabled":
            # OpenRouter may support this via provider-specific params
            payload["provider"] = {"anthropic": {"thinking": thinking}}
        
        resp = self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        
        # Extract cost from OpenRouter response
        # OpenRouter returns cost in usage.total_cost or we can get from headers
        usage = data.get("usage", {})
        cost = usage.get("total_cost") or float(resp.headers.get("x-openrouter-cost", 0))
        
        # Normalize to Anthropic-like response structure
        return {
            "id": data.get("id"),
            "model": data.get("model"),
            "content": self._normalize_content(data),
            "stop_reason": self._normalize_stop_reason(data),
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                "cost": cost,  # Live cost from OpenRouter!
            },
        }
    
    def _normalize_content(self, data: dict) -> list:
        """Convert OpenAI-style response to Anthropic-style content blocks."""
        choices = data.get("choices", [])
        if not choices:
            return []
        
        message = choices[0].get("message", {})
        content = []
        
        # Text content
        if message.get("content"):
            content.append(_ContentBlock("text", message["content"]))
        
        # Tool calls
        for tc in message.get("tool_calls", []):
            content.append(_ContentBlock("tool_use", None, 
                                         id=tc["id"],
                                         name=tc["function"]["name"],
                                         input=json.loads(tc["function"]["arguments"])))
        
        return content
    
    def _normalize_stop_reason(self, data: dict) -> str:
        """Convert OpenAI finish_reason to Anthropic stop_reason."""
        choices = data.get("choices", [])
        if not choices:
            return "end_turn"
        
        reason = choices[0].get("finish_reason", "stop")
        mapping = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "end_turn",
        }
        return mapping.get(reason, "end_turn")


class _ContentBlock:
    """Mimics Anthropic content block structure."""
    def __init__(self, type: str, text: str | None = None, **kwargs):
        self.type = type
        self.text = text
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Usage:
    """Mimics Anthropic usage structure with live cost."""
    def __init__(self, data: dict):
        self.input_tokens = data.get("input_tokens", 0)
        self.output_tokens = data.get("output_tokens", 0)
        self.cache_read_input_tokens = data.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = data.get("cache_creation_input_tokens", 0)
        self.cost = data.get("cost", 0)  # Live cost from OpenRouter


class _Response:
    """Mimics Anthropic Message response."""
    def __init__(self, data: dict):
        self.id = data.get("id")
        self.model = data.get("model")
        self.content = data.get("content", [])
        self.stop_reason = data.get("stop_reason")
        self.usage = _Usage(data.get("usage", {}))


class Router:
    """Single entry point for every LLM call: pricing, logging, budget enforcement."""

    def __init__(self, store, budget_usd: float = 2.00):
        self.client = OpenRouterClient()
        self.store = store
        self.budget = budget_usd
        self._warned = False

    def spent(self) -> float:
        return self.store.total_cost()

    def call(self, model: str, *, purpose: str, episode_id: int | None = None,
             max_tokens: int = 2048, messages: list, system: str | None = None,
             tools: list | None = None, tool_choice: dict | None = None,
             **kwargs) -> _Response:
        if self.spent() >= self.budget:
            raise BudgetExceeded(
                f"Budget cap ${self.budget:.2f} reached (spent ${self.spent():.4f})")
        
        # Convert Anthropic-style tool_choice to OpenRouter format
        or_tool_choice = None
        if tool_choice:
            if tool_choice.get("type") == "tool":
                or_tool_choice = {"type": "function", "function": {"name": tool_choice["name"]}}
            elif tool_choice.get("type") == "any":
                or_tool_choice = {"type": "required"}
            elif tool_choice.get("type") == "auto":
                or_tool_choice = {"type": "auto"}
        
        # Convert Anthropic-style tools to OpenRouter (OpenAI) format
        or_tools = None
        if tools:
            or_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    }
                }
                for t in tools
            ]
        
        # Handle output_config for structured JSON output
        response_format = None
        if "output_config" in kwargs:
            oc = kwargs.pop("output_config")
            if oc.get("format", {}).get("type") == "json_schema":
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "response",
                        "schema": oc["format"]["schema"],
                    }
                }
        
        data = self.client.messages_create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            system=system,
            tools=or_tools,
            tool_choice=or_tool_choice,
            response_format=response_format,
            **kwargs
        )
        
        response = _Response(data)
        usage = response.usage
        
        # Use live cost from OpenRouter!
        cost = usage.cost
        
        self.store.add_api_call(
            episode_id, model, purpose,
            usage.input_tokens, usage.output_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
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
    """Structured-output call that survives token truncation."""
    base_user = user
    kwargs: dict = {}
    
    # Use JSON schema response format
    kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    
    if enable_thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10000}

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
        
        response = router.call(
            model, purpose=purpose, episode_id=episode_id,
            max_tokens=max_tokens, messages=[{"role": "user", "content": content}],
            system=system, **kwargs
        )
        
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        usage = response.usage
        out_tok = usage.output_tokens if usage else None

        if response.stop_reason == "max_tokens" or not text:
            reason = ("hit_max_tokens" if response.stop_reason == "max_tokens"
                      else "empty_text")
            next_budget = max_tokens * 2
            last_err = f"truncated at {max_tokens} tokens (stop_reason={response.stop_reason})"
            _trace({
                "event": "retry",
                "reason": reason,
                "detail": last_err,
                "attempt": attempt + 1,
                "max_tokens": max_tokens,
                "next_max_tokens": next_budget,
                "stop_reason": response.stop_reason,
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
                "stop_reason": response.stop_reason,
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
                "stop_reason": response.stop_reason,
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
