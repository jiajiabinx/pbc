"""structured_json must survive thinking-truncation / malformed model output."""
from types import SimpleNamespace

import pytest

import models


class Block(SimpleNamespace):
    pass


def resp(text, stop_reason="end_turn", with_thinking=False, output_tokens=0):
    blocks = []
    if with_thinking:
        blocks.append(Block(type="thinking", thinking=""))
    if text is not None:
        blocks.append(Block(type="text", text=text))
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=0, output_tokens=output_tokens))


class FakeStore:
    def __init__(self):
        self.traces = []

    def add_trace(self, episode_id, kind, name, payload):
        self.traces.append({
            "episode_id": episode_id, "kind": kind, "name": name, "payload": payload,
        })


class FakeRouter:
    def __init__(self, responses, store=None):
        self.responses = list(responses)
        self.max_tokens_seen = []
        self.kwargs_seen = []
        self.store = store or FakeStore()

    def call(self, model, *, max_tokens, **kwargs):
        self.max_tokens_seen.append(max_tokens)
        self.kwargs_seen.append(kwargs)
        return self.responses.pop(0)


def test_happy_path_with_thinking_block():
    r = FakeRouter([resp('{"ok": true}', with_thinking=True)])
    out = models.structured_json(r, "claude-sonnet-5", purpose="t", schema={}, user="u",
                                 episode_id=1)
    assert out == {"ok": True}
    assert r.store.traces == []  # no retries → no token_retry events


def test_retries_on_truncation_with_doubled_budget():
    r = FakeRouter([
        resp('{"ok": tr', stop_reason="max_tokens", output_tokens=1000),
        resp(None, stop_reason="max_tokens", output_tokens=2000),
        resp('{"ok": true}', output_tokens=50),
    ])
    out = models.structured_json(r, "claude-sonnet-5", purpose="verify:PBC-07", schema={},
                                 user="u", max_tokens=1000, episode_id=7)
    assert out == {"ok": True}
    assert r.max_tokens_seen == [1000, 2000, 4000]
    assert "Previous reply was truncated" in r.kwargs_seen[1]["messages"][0]["content"]

    events = [t["payload"]["event"] for t in r.store.traces]
    assert events == ["retry", "retry", "recovered"]
    assert r.store.traces[0]["payload"]["reason"] == "hit_max_tokens"
    assert r.store.traces[0]["payload"]["max_tokens"] == 1000
    assert r.store.traces[0]["payload"]["next_max_tokens"] == 2000
    assert r.store.traces[1]["payload"]["next_max_tokens"] == 4000
    assert r.store.traces[2]["payload"]["started_max_tokens"] == 1000
    assert all(t["kind"] == "token_retry" and t["name"] == "verify:PBC-07"
                for t in r.store.traces)


def test_retries_on_unterminated_json_string():
    r = FakeRouter([
        resp('{"ok": "untermin', stop_reason="end_turn"),
        resp('{"ok": true}'),
    ])
    out = models.structured_json(r, "claude-sonnet-5", purpose="t", schema={}, user="u",
                                 max_tokens=500, episode_id=1)
    assert out == {"ok": True}
    assert r.store.traces[0]["payload"]["reason"] == "unterminated_or_invalid_json"
    assert r.store.traces[0]["payload"]["next_max_tokens"] == 1000


def test_raises_cleanly_after_three_failures():
    r = FakeRouter([resp("not json"), resp("{broken"), resp("", stop_reason="max_tokens")])
    with pytest.raises(ValueError, match="structured output failed"):
        models.structured_json(r, "claude-sonnet-5", purpose="t", schema={}, user="u",
                               episode_id=1)
    assert r.store.traces[-1]["payload"]["event"] == "failed"
