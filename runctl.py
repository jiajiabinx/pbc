"""Run control: links the Streamlit UI to the agent runner process.

Both sides talk through the shared SQLite `meta` table:
    run_control   'run' | 'pause' | 'stop'   (UI writes, agent loop reads)
    run_status    'idle'|'launching'|'running'|'paused'|'stopped'|'drafting'|
                  'finished'|'budget_exceeded'|'error'  (runner writes, UI reads)
    run_progress  {"done": n, "total": m, "current": subject}
    run_pid / run_args / run_error

The pause is cooperative — the loop checks between episodes, so an in-flight
episode always completes and the trace stays consistent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LOG_PATH = "data/run.log"


def _pid_alive(pid: str | None) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except ValueError:
        return False
    try:
        # The runner is our child: if it exited, reap it here. A dead child we
        # never wait() on is a zombie, and the os.kill probe below reports
        # zombies as alive — leaving the UI stuck on "launching" forever.
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            return False
    except ChildProcessError:
        pass  # not our child (the UI restarted since the run began)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # os.kill(pid, 0) also succeeds for zombies we can't reap (the runner was
    # spawned by a previous UI process that still exists) — check the process
    # state instead of trusting the signal probe.
    stat = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                          capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def state(store) -> dict:
    status = store.get_meta("run_status") or "idle"
    alive = _pid_alive(store.get_meta("run_pid"))
    if status in ("launching", "running", "paused", "drafting") and not alive:
        status = "crashed"
    return {
        "status": status,
        "alive": alive,
        "progress": json.loads(store.get_meta("run_progress") or "{}"),
        "args": json.loads(store.get_meta("run_args") or "null"),
        "error": store.get_meta("run_error"),
    }


def request(store, control: str) -> None:
    assert control in ("run", "pause", "stop")
    store.set_meta("run_control", control)


def start(store, mailbox: str, pbc: str, profile: str, *,
          fresh: bool = False, budget: float = 2.0, api_key: str = "") -> int:
    """Spawn `run.py` as a background process. The Anthropic key comes from the
    `api_key` argument (the UI field) or falls back to the UI's environment.
    The key is only put in the child's env — never written to the store."""
    if state(store)["alive"]:
        raise RuntimeError("A run is already in progress.")
    env = os.environ.copy()
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    if not (env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "No Anthropic API key — the runner would exit immediately. Paste a "
            "key in the run inputs, or restart Streamlit from a shell where "
            "ANTHROPIC_API_KEY is exported.")
    if fresh:
        store.reset_all()
    store.set_meta("run_control", "run")
    store.set_meta("run_status", "launching")
    store.set_meta("run_error", "")
    store.set_meta("run_progress", "{}")
    cmd = [sys.executable, "run.py", "--mailbox", mailbox, "--pbc", pbc,
           "--profile", profile, "--db", store.db_path, "--budget", str(budget)]
    root = Path(__file__).resolve().parent
    (root / "data").mkdir(exist_ok=True)
    with open(root / LOG_PATH, "ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=log,
                                stderr=subprocess.STDOUT, env=env)
    store.set_meta("run_pid", str(proc.pid))
    store.set_meta("run_args", json.dumps(
        {"mailbox": mailbox, "pbc": pbc, "profile": profile, "budget": budget}))
    return proc.pid
