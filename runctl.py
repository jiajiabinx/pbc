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

import db

LOG_PATH = "data/run.log"


def _proc_state(pid: int) -> str | None:
    """Return the process state letter (R/S/Z/…) or None if the pid is gone.

    Prefer /proc (always present in Linux containers; the slim image has no
    `ps`). Fall back to `ps` on macOS/dev where /proc does not exist.
    """
    try:
        # /proc/<pid>/stat field 3 is the state; field 2 is "(comm)" and may
        # contain spaces, so take the char after the last ')'.
        with open(f"/proc/{pid}/stat") as f:
            body = f.read()
        parts = body.rsplit(")", 1)[-1].split()
        return parts[0] if parts else None
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return out[0] if out else None
    except OSError:
        # No /proc and no ps (slim image) — caller already confirmed kill(0).
        return "?"


def _pid_alive(pid: str | None) -> bool:
    """Never raise — a probe failure must not take down the Streamlit UI."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        # The runner is our child: if it exited, reap it here. A dead child we
        # never wait() on is a zombie, and the os.kill probe below reports
        # zombies as alive — leaving the UI stuck on "launching" forever.
        try:
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
        state = _proc_state(pid)
        return state is not None and not state.startswith("Z")
    except Exception:
        return False


def _meta_json(store, key: str, default):
    raw = store.get_meta(key)
    if not raw:
        return default
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default
    return val if val is not None else default


def state(store) -> dict:
    status = store.get_meta("run_status") or "idle"
    alive = _pid_alive(store.get_meta("run_pid"))
    if status in ("launching", "running", "paused", "drafting") and not alive:
        status = "crashed"
    progress = _meta_json(store, "run_progress", {})
    args = _meta_json(store, "run_args", {})
    if not isinstance(progress, dict):
        progress = {}
    if not isinstance(args, dict):
        args = {}
    return {
        "status": status,
        "alive": alive,
        "progress": progress,
        "args": args,
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
           "--profile", profile, "--budget", str(budget)]
    if db.is_pg(store.db_path):
        # Pass Postgres via env so the connection URL (with credentials) never
        # shows up in `ps`/the process table; run.py reads $DATABASE_URL.
        env["DATABASE_URL"] = store.db_path
    else:
        cmd += ["--db", store.db_path]
    env["PYTHONUNBUFFERED"] = "1"  # keep the log tail-able mid-run
    env["PYTHONIOENCODING"] = "utf-8"  # log is written under our redirect, not a tty
    root = Path(__file__).resolve().parent
    (root / "data").mkdir(exist_ok=True)
    with open(root / LOG_PATH, "ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=log,
                                stderr=subprocess.STDOUT, env=env)
    store.set_meta("run_pid", str(proc.pid))
    store.set_meta("run_args", json.dumps(
        {"mailbox": mailbox, "pbc": pbc, "profile": profile, "budget": budget}))
    return proc.pid
