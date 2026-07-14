import pytest

from store import Store, StatusGuardError


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    s.load_items([
        {"item_id": "PBC-01", "category": "Cash", "priority": "High",
         "description": "Bank statements", "acceptance": "period_end=2026-06-30",
         "expected_docs": "pdf"},
    ])
    return s


def test_status_guard_blocks_unverified(store):
    for status in ("Received", "Insufficient", "Complete"):
        with pytest.raises(StatusGuardError):
            store.update_item_status("PBC-01", status, rationale="x")
    assert store.get_item("PBC-01")["status"] == "Not started"


def test_unguarded_statuses_allowed(store):
    store.update_item_status("PBC-01", "Requested", rationale="kickoff email")
    store.update_item_status("PBC-01", "Under review", rationale="doc arrived")
    assert store.get_item("PBC-01")["status"] == "Under review"


def test_guard_lifts_after_verification(store):
    info = store.register_document("stmt.pdf", "/tmp/stmt.pdf", "abc", "<m1>", "stmt::pdf")
    store.add_verification("PBC-01", info["doc_id"], "sufficient", "ok", [], 0.9, episode_id=1)
    store.update_item_status("PBC-01", "Received", confidence=0.9, rationale="verified",
                             doc_id=info["doc_id"])
    row = store.get_item("PBC-01")
    assert row["status"] == "Received"
    assert row["latest_doc_id"] == info["doc_id"]


def test_guard_checks_specific_doc(store):
    a = store.register_document("a.pdf", "/tmp/a", "h1", "<m1>", "a::pdf")
    b = store.register_document("b.pdf", "/tmp/b", "h2", "<m1>", "b::pdf")
    store.add_verification("PBC-01", a["doc_id"], "sufficient", "ok", [], 0.9, 1)
    with pytest.raises(StatusGuardError):
        store.update_item_status("PBC-01", "Received", rationale="x", doc_id=b["doc_id"])


def test_invalid_status_rejected(store):
    with pytest.raises(ValueError):
        store.update_item_status("PBC-01", "Done-ish", rationale="x")
    with pytest.raises(ValueError):
        store.update_item_status("PBC-99", "Requested", rationale="x")


def test_version_lineage(store):
    v1 = store.register_document("tb_v1.xlsx", "/t/1", "s1", "<m1>", "tb::excel")
    v2 = store.register_document("tb_v2.xlsx", "/t/2", "s2", "<m2>", "tb::excel")
    v3 = store.register_document("tb_Final_v3_REAL.xlsx", "/t/3", "s3", "<m3>", "tb::excel")
    assert (v1["version"], v2["version"], v3["version"]) == (1, 2, 3)
    assert v3["supersedes"] == v2["doc_id"]
    chain = store.lineage(v3["doc_id"])
    assert [d["version"] for d in chain] == [1, 2, 3]


def test_duplicate_by_hash(store):
    a = store.register_document("x.pdf", "/t/x", "same", "<m1>", "x::pdf")
    b = store.register_document("x (1).pdf", "/t/x2", "same", "<m2>", "x::pdf")
    assert b["duplicate"] is True
    assert b["doc_id"] == a["doc_id"]


def test_cost_accounting(store):
    store.add_api_call(None, "claude-haiku-4-5", "t", 1000, 100, 0, 0, 0.0015)
    store.add_api_call(1, "claude-sonnet-5", "v", 2000, 200, 500, 0, 0.0093)
    assert store.total_cost() == pytest.approx(0.0108)


def test_call_cost_math():
    import models

    class U:
        input_tokens = 1_000_000
        output_tokens = 100_000
        cache_read_input_tokens = 2_000_000
        cache_creation_input_tokens = 400_000

    # haiku: 1*1.00 + 2*0.10*1.00 + 0.4*1.25*1.00 + 0.1*5.00 = 2.20
    assert models.call_cost("claude-haiku-4-5", U()) == pytest.approx(2.20)
