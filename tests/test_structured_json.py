"""structured_json must survive thinking-truncated / malformed model output."""
from types import SimpleNamespace

import pytest

import models


class Block(SimpleNamespace):
    pass


def resp(text, stop_reason="end_turn", with_thinking=False):
    blocks = []
    if with_thinking:
        blocks.append(Block(type="thinking", thinking=""))
    if text is not None:
        blocks.append(Block(type="text", text=text))
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=0, output_tokens=0))


class FakeRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.max_tokens_seen = []

    def call(self, model, *, max_tokens, **kwargs):
        self.max_tokens_seen.append(max_tokens)
        return self.responses.pop(0)


def test_happy_path_with_thinking_block():
    r = FakeRouter([resp('{"ok": true}', with_thinking=True)])
    out = models.structured_json(r, "claude-sonnet-5", purpose="t", schema={}, user="u")
    assert out == {"ok": True}


def test_retries_on_truncation_with_doubled_budget():
    r = FakeRouter([
        resp('{"ok": tr', stop_reason="max_tokens"),      # truncated
        resp(None, stop_reason="max_tokens"),              # thinking ate everything
        resp('{"ok": true}'),
    ])
    out = models.structured_json(r, "claude-sonnet-5", purpose="t", schema={}, user="u",
                                 max_tokens=1000)
    assert out == {"ok": True}
    assert r.max_tokens_seen == [1000, 2000, 4000]


def test_raises_cleanly_after_three_failures():
    r = FakeRouter([resp("not json"), resp("{broken"), resp("", stop_reason="max_tokens")])
    with pytest.raises(ValueError, match="structured output failed"):
        models.structured_json(r, "claude-sonnet-5", purpose="t", schema={}, user="u")
