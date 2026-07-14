"""Run-control liveness: a runner that died at launch must not look alive."""
import subprocess
import sys
import time

import pytest

import runctl
from store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def spawn_dead_child() -> int:
    """Start a child that exits immediately and do NOT wait on it, mirroring
    runctl.start dropping its Popen handle — the child becomes a zombie."""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(2)"])
    pid = proc.pid
    proc.returncode = None  # keep Popen.__del__ from reaping it for us
    for _ in range(50):  # give it a moment to actually exit
        time.sleep(0.02)
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                             capture_output=True, text=True).stdout.strip()
        if not out or out.startswith("Z"):
            break
    return pid

def test_pid_alive_reaps_zombie_child():
    pid = spawn_dead_child()
    assert runctl._pid_alive(str(pid)) is False


def test_state_reports_crash_when_runner_died_at_launch(store):
    # runctl.start advertises "launching"; if the child dies before run.py
    # takes over, the UI must see "crashed", not a live run stuck on
    # "launching" whose Pause/Stop buttons write into the void.
    store.set_meta("run_status", "launching")
    store.set_meta("run_pid", str(spawn_dead_child()))
    assert runctl.state(store)["status"] == "crashed"


def test_pid_alive_detects_foreign_zombie():
    # A runner spawned by a *previous* UI process: we can't waitpid it, and
    # os.kill(pid, 0) succeeds on zombies, so the ps state check must catch it.
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         "p = subprocess.Popen([sys.executable, '-c', 'pass'])\n"
         "print(p.pid, flush=True)\n"
         "time.sleep(30)\n"],  # parent lingers without reaping -> child zombifies
        stdout=subprocess.PIPE, text=True)
    try:
        zombie_pid = int(parent.stdout.readline())
        for _ in range(50):
            out = subprocess.run(["ps", "-p", str(zombie_pid), "-o", "stat="],
                                 capture_output=True, text=True).stdout.strip()
            if out.startswith("Z"):
                break
            time.sleep(0.02)
        assert runctl._pid_alive(str(zombie_pid)) is False
    finally:
        parent.kill()
        parent.wait()


def test_pid_alive_basics():
    assert runctl._pid_alive(None) is False
    assert runctl._pid_alive("") is False
    assert runctl._pid_alive("not-a-pid") is False
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert runctl._pid_alive(str(proc.pid)) is True
    finally:
        proc.kill()
        proc.wait()
    assert runctl._pid_alive(str(proc.pid)) is False


def test_start_fails_fast_without_api_key(store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        runctl.start(store, "mbox", "pbc.pdf", "profile.pdf")


def test_bench_start_fails_fast_without_api_key(store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        runctl.bench_start(store, 2, "mbox", "pbc.pdf", "profile.pdf")


def test_benchmark_summarize():
    from evals.benchmark import summarize
    runs = [{"status_accuracy": 0.8, "insufficiency_f1": 1.0,
             "sequence_match": 0.9, "cost_usd": 0.2},
            {"status_accuracy": 1.0, "insufficiency_f1": 0.5,
             "sequence_match": 0.7, "cost_usd": 0.4}]
    sm = summarize(runs)
    assert sm["status_accuracy"]["mean"] == pytest.approx(0.9)
    assert sm["cost_usd"]["min"] == 0.2 and sm["cost_usd"]["max"] == 0.4
    assert sm["insufficiency_f1"]["stdev"] > 0


def test_start_passes_ui_key_to_runner_env_only(store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return type("P", (), {"pid": 12345})()

    monkeypatch.setattr(runctl.subprocess, "Popen", fake_popen)
    runctl.start(store, "mbox", "pbc.pdf", "profile.pdf", api_key="sk-ant-ui-field")
    assert seen["env"]["ANTHROPIC_API_KEY"] == "sk-ant-ui-field"
    # the key must never be persisted to the shared DB
    rows = store.conn.execute("SELECT value FROM meta").fetchall()
    assert not any("sk-ant-ui-field" in r["value"] for r in rows)
