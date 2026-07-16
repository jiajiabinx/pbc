"""Deterministic tests of the agent loop wiring (no LLM calls):
forced first plan, done handling, and the escalation ladder."""
from types import SimpleNamespace

import pytest

import agent
import models
from store import Store


class Block(SimpleNamespace):
    pass


def tool_use(name, inp, id_="tu_1"):
    return Block(type="tool_use", name=name, input=inp, id=id_)


def response(blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=0, output_tokens=0))


class FakeRouter:
    """Pops scripted responses keyed by model; records the calls it saw."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def call(self, model, **kwargs):
        self.calls.append((model, kwargs))
        return self.scripts[model].pop(0)

    def spent(self):
        return 0.0


class FakeMatcher:
    def match(self, query, top_k=5):
        return []


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.load_items([{"item_id": "PBC-01", "category": "Cash", "priority": "High",
                   "description": "d", "acceptance": "a", "expected_docs": "pdf"}])
    return s


def make_email():
    return SimpleNamespace(email_id="<m1>", thread_id="<m1>", from_addr="a@b.c",
                           from_name="A", to_addrs=[], cc_addrs=[], subject="s",
                           date=0.0, body="hello", attachments=[])


def test_first_call_forces_plan_and_done_ends(store):
    router = FakeRouter({models.WORKER: [
        response([tool_use("submit_plan", {"classification": "other", "steps": ["nothing"]})]),
        response([tool_use("done", {"summary": "no action"})]),
    ]})
    res = agent.run_episode(store, router, FakeMatcher(), "profile", "system", make_email())
    assert res["outcome"] == "completed"
    assert res["summary"] == "no action"
    # first request forced submit_plan
    first_model, first_kwargs = router.calls[0]
    assert first_kwargs["tool_choice"] == {"type": "tool", "name": "submit_plan"}
    # second request back to auto
    assert router.calls[1][1]["tool_choice"] == {"type": "auto"}
    kinds = [r["kind"] for r in store.conn.execute(
        "SELECT kind FROM trace WHERE episode_id=? ORDER BY seq", (res["episode_id"],))]
    assert kinds[0] == "plan"


def test_escalation_reruns_on_next_model(store):
    router = FakeRouter({
        models.WORKER: [
            response([tool_use("submit_plan", {"classification": "other", "steps": ["?"]})]),
            response([tool_use("escalate", {"reason": "ambiguous"})]),
        ],
        "claude-sonnet-5": [
            response([tool_use("submit_plan", {"classification": "other", "steps": ["ok"]})]),
            response([tool_use("done", {"summary": "handled by sonnet"})]),
        ],
    })
    res = agent.run_episode(store, router, FakeMatcher(), "profile", "system", make_email())
    assert res["model"] == "claude-sonnet-5"
    assert res["summary"] == "handled by sonnet"
    eps = store.conn.execute("SELECT * FROM episodes ORDER BY episode_id").fetchall()
    assert len(eps) == 2
    assert eps[1]["escalated_from"] == eps[0]["episode_id"]
    # escalation reason is in the first episode's trace
    esc = store.conn.execute(
        "SELECT payload FROM trace WHERE episode_id=? AND kind='escalation'",
        (eps[0]["episode_id"],)).fetchone()
    assert "ambiguous" in esc["payload"]


def test_run_control_stop_halts_before_next_episode(store):
    # UI sets run_control=stop -> the mailbox loop must not start new episodes.
    store.set_meta("run_control", "stop")
    router = FakeRouter({})  # any model call would raise (empty script)
    results = agent.run_mailbox(store, router, FakeMatcher(), "profile",
                                [{"item_id": "PBC-01", "category": "c", "priority": "High",
                                  "description": "d", "acceptance": "a", "expected_docs": "pdf"}],
                                "header", [make_email()])
    assert results == []
    assert store.get_meta("run_status") == "stopped"
    assert router.calls == []
    store.set_meta("run_control", "run")  # reset for other tests


PBC_ITEMS = [{"item_id": "PBC-01", "category": "c", "priority": "High",
              "description": "d", "acceptance": "a", "expected_docs": "pdf"}]


def test_completed_episode_marks_email_processed(store):
    router = FakeRouter({models.WORKER: [
        response([tool_use("submit_plan", {"classification": "other", "steps": ["nothing"]})]),
        response([tool_use("done", {"summary": "no action"})]),
    ]})
    email = make_email()
    agent.run_mailbox(store, router, FakeMatcher(), "profile", PBC_ITEMS, "header", [email])
    row = store.conn.execute(
        "SELECT processed_at FROM emails WHERE email_id=?", (email.email_id,)).fetchone()
    assert row["processed_at"] is not None


def test_crashed_episode_leaves_email_unprocessed_for_resume(store):
    # Regression: a crash mid-episode must NOT mark the email processed, or the
    # resume filter (processed_at IS NOT NULL) silently skips it on the next run.
    class Boom(FakeRouter):
        def call(self, model, **kwargs):
            self.calls.append((model, kwargs))
            raise RuntimeError("network blew up mid-episode")

    email = make_email()
    with pytest.raises(RuntimeError):
        agent.run_mailbox(store, Boom({}), FakeMatcher(), "profile",
                          PBC_ITEMS, "header", [email])
    row = store.conn.execute(
        "SELECT processed_at FROM emails WHERE email_id=?", (email.email_id,)).fetchone()
    assert row is not None                # the email was recorded
    assert row["processed_at"] is None    # but not marked processed -> resume retries it


def test_status_guard_error_returned_to_model_not_raised(store):
    router = FakeRouter({models.WORKER: [
        response([tool_use("submit_plan", {"classification": "client_documents", "steps": ["x"]})]),
        response([tool_use("update_item_status",
                           {"item_id": "PBC-01", "status": "Received", "rationale": "looks fine"})]),
        response([tool_use("done", {"summary": "blocked"})]),
    ]})
    res = agent.run_episode(store, router, FakeMatcher(), "profile", "system", make_email())
    assert res["outcome"] == "completed"
    # the guard refused the update; item unchanged
    assert store.get_item("PBC-01")["status"] == "Not started"
    # and the model received the error as a tool result (search the transcript;
    # kwargs["messages"] is the live list, so inspect its final state)
    _, kwargs = router.calls[-1]
    transcript = str(kwargs["messages"])
    assert "verify_item" in transcript and "Refusing to set" in transcript
