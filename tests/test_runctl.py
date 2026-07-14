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
