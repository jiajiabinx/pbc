"""Benchmark harness: N fresh sample runs, each scored, then aggregated.

    python evals/benchmark.py --runs 3

Each run executes `run.py` against a throwaway scratch DB (the live tracker DB
is never touched), scores it with `evaluate()`, and streams per-run results
into the report DB's meta table so the UI's Evals tab can poll progress live:

    bench_control   'run' | 'stop'          (UI writes, this loop reads)
    bench_status    'launching'|'running'|'stopped'|'finished'|'error'
    bench_progress  {"done": n, "total": m}
    bench_results   [per-run accuracy/cost dicts]
    bench_summary   mean/stdev/min/max per metric
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.run_evals import evaluate  # noqa: E402
from store import Store  # noqa: E402


def summarize(runs: list[dict]) -> dict:
    def stats(key: str) -> dict:
        vals = [r[key] for r in runs]
        return {"mean": statistics.mean(vals),
                "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
                "min": min(vals), "max": max(vals)}
    return {k: stats(k) for k in ("status_accuracy", "insufficiency_f1",
                                  "sequence_match", "cost_usd")}


def score_run(scratch_db: str, groundtruth: str, labels: str, run_no: int) -> dict:
    r = evaluate(scratch_db, groundtruth, labels)
    s, q, c = r["status"], r["sequences"], r["cost"]
    return {
        "run": run_no,
        "status_accuracy": s["correct"] / s["total"] if s["total"] else 0.0,
        "insufficiency_f1": r["insufficiency"]["f1"],
        "sequence_match": q["matched"] / q["total"] if q["total"] else 0.0,
        "cost_usd": c["total_usd"],
        "escalations": c["escalations"],
        "api_calls": c["calls"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Repeat N fresh runs and aggregate stats")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--mailbox", default="input/sample/sample_mailbox.mbox")
    ap.add_argument("--pbc", default="input/PBC_List_FY2026.pdf")
    ap.add_argument("--profile", default="input/Client_Profile.pdf")
    ap.add_argument("--budget", type=float, default=2.0, help="hard USD cap per run")
    ap.add_argument("--groundtruth", default="input/sample/sample_groundtruth.json")
    ap.add_argument("--labels", default="evals/labels.json")
    ap.add_argument("--report-db", default="data/pbc.db",
                    help="DB whose meta table receives progress/results (the UI's DB)")
    ap.add_argument("--scratch-db", default="data/benchmark.db",
                    help="throwaway DB each run executes against")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    report = Store(str(root / args.report_db))
    report.set_meta("bench_pid", str(os.getpid()))
    report.set_meta("bench_status", "running")
    report.set_meta("bench_error", "")

    results: list[dict] = []
    try:
        for i in range(1, args.runs + 1):
            if report.get_meta("bench_control") == "stop":
                break
            report.set_meta("bench_progress", json.dumps({"done": i - 1, "total": args.runs}))
            scratch = root / args.scratch_db
            # reset in place rather than unlink: keeps the content-hash OCR
            # cache, so runs 2..N don't re-pay vision calls for the same files
            Store(str(scratch)).reset_all()
            print(f"=== benchmark run {i}/{args.runs} ===", flush=True)
            proc = subprocess.run(
                [sys.executable, "run.py", "--mailbox", args.mailbox, "--pbc", args.pbc,
                 "--profile", args.profile, "--db", str(scratch),
                 "--budget", str(args.budget), "--no-drafts"],
                cwd=str(root), env=os.environ | {"PYTHONUNBUFFERED": "1"})
            if proc.returncode != 0:
                raise RuntimeError(f"run {i} exited with code {proc.returncode}")
            results.append(score_run(str(scratch), args.groundtruth, args.labels, i))
            report.set_meta("bench_results", json.dumps(results))
            report.set_meta("bench_progress", json.dumps({"done": i, "total": args.runs}))
        report.set_meta("bench_summary", json.dumps(summarize(results) if results else {}))
        report.set_meta("bench_status",
                        "stopped" if report.get_meta("bench_control") == "stop"
                        else "finished")
    except Exception as e:
        report.set_meta("bench_status", "error")
        report.set_meta("bench_error", f"{type(e).__name__}: {e}")
        raise

    print(f"\n{'run':<5}{'status acc':>11}{'insuff F1':>11}{'tool seq':>10}{'cost $':>9}")
    for r in results:
        print(f"{r['run']:<5}{r['status_accuracy']:>10.0%}{r['insufficiency_f1']:>11.2f}"
              f"{r['sequence_match']:>9.0%}{r['cost_usd']:>9.4f}")
    if results:
        sm = summarize(results)
        print(f"mean {sm['status_accuracy']['mean']:>10.0%}"
              f"{sm['insufficiency_f1']['mean']:>11.2f}"
              f"{sm['sequence_match']['mean']:>9.0%}{sm['cost_usd']['mean']:>9.4f}"
              f"   (± {sm['cost_usd']['stdev']:.4f} cost stdev over {len(results)} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
