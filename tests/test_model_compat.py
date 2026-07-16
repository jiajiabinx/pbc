"""Sonnet 5 / Opus reject sampling params and forced tool_choice with thinking."""

import models


def test_compat_strips_sampling_on_sonnet5():
    out = models.compat_kwargs(
        "claude-sonnet-5",
        {"temperature": 0, "top_p": 0.9, "top_k": 5, "messages": []},
    )
    assert "temperature" not in out
    assert "top_p" not in out
    assert "top_k" not in out
    assert out["messages"] == []


def test_compat_disables_thinking_for_forced_tool_choice():
    out = models.compat_kwargs(
        "claude-sonnet-5",
        {"tool_choice": {"type": "tool", "name": "submit_plan"}, "tools": []},
    )
    assert out["thinking"] == {"type": "disabled"}


def test_compat_disables_thinking_for_tool_choice_any():
    out = models.compat_kwargs(
        "claude-opus-4-8",
        {"tool_choice": {"type": "any"}},
    )
    assert out["thinking"] == {"type": "disabled"}


def test_compat_rewrites_legacy_enabled_thinking():
    out = models.compat_kwargs(
        "claude-sonnet-5",
        {"thinking": {"type": "enabled", "budget_tokens": 8000}},
    )
    assert out["thinking"] == {"type": "disabled"}


def test_compat_leaves_haiku_and_auto_tool_choice_alone():
    kwargs = {
        "temperature": 0.5,
        "tool_choice": {"type": "tool", "name": "submit_plan"},
        "thinking": {"type": "enabled", "budget_tokens": 1000},
    }
    assert models.compat_kwargs("claude-haiku-4-5", kwargs) == kwargs

    auto = {"tool_choice": {"type": "auto"}, "messages": []}
    assert models.compat_kwargs("claude-sonnet-5", auto) == auto
