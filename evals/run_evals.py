"""Eval harness: score a completed run against ground truth + labeled tool sequences.

    python evals/run_evals.py --db data/pbc.db \\
        --groundtruth sample/sample_groundtruth.json --labels evals/labels.json

Reports: per-status precision/recall, insufficiency-detection F1, expected
tool-call-sequence match rate, and measured cost. No LLM calls.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def normalize(status: str, equivalences: dict) -> str:
    return equivalences.get(status, status)


def subsequence_match(required: list[str], actual: list[str]) -> bool:
    """required is an ordered subsequence; 'a|b' matches either tool."""
    i = 0
    for tool in actual:
        if i < len(required) and tool in required[i].split("|"):
            i += 1
    return i == len(required)


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/pbc.db")
    ap.add_argument("--groundtruth", default="sample/sample_groundtruth.json")
    ap.add_argument("--labels", default="evals/labels.json")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    gt = json.loads(Path(args.groundtruth).read_text())["expected_status"]
    labels = json.loads(Path(args.labels).read_text())
    equiv = labels.get("status_equivalences", {})

    # ---------------- status scoring ----------------
    predicted = {r["item_id"]: normalize(r["status"], equiv)
                 for r in conn.execute("SELECT item_id, status FROM items")}
    expected = {k: v["status"] for k, v in gt.items()}

    per_class = defaultdict(Counter)
    correct = 0
    mismatches = []
    for item_id, exp in expected.items():
        pred = predicted.get(item_id, "MISSING")
        if pred == exp:
            correct += 1
            per_class[exp]["tp"] += 1
        else:
            per_class[exp]["fn"] += 1
            per_class[pred]["fp"] += 1
            mismatches.append((item_id, exp, pred))

    print("=" * 64)
    print(f"STATUS ACCURACY: {correct}/{len(expected)} "
          f"({correct / len(expected):.0%})")
    print(f"{'status':<14}{'precision':>10}{'recall':>10}{'f1':>8}")
    for cls in sorted(set(expected.values()) | set(predicted.values())):
        c = per_class[cls]
        p, r, f = prf(c["tp"], c["fp"], c["fn"])
        print(f"{cls:<14}{p:>10.2f}{r:>10.2f}{f:>8.2f}")
    if mismatches:
        print("\nMismatches (item: expected -> predicted):")
        for m in mismatches:
            print(f"  {m[0]}: {m[1]} -> {m[2]}")

    # insufficiency-detection F1 (binary)
    tp = sum(1 for k in expected if expected[k] == "Insufficient" and predicted.get(k) == "Insufficient")
    fp = sum(1 for k in expected if expected[k] != "Insufficient" and predicted.get(k) == "Insufficient")
    fn = sum(1 for k in expected if expected[k] == "Insufficient" and predicted.get(k) != "Insufficient")
    p, r, f = prf(tp, fp, fn)
    print(f"\nINSUFFICIENCY DETECTION: precision={p:.2f} recall={r:.2f} F1={f:.2f}")

    # ---------------- tool-sequence scoring ----------------
    # Episodes in email-chronological order; for escalated emails, judge the
    # final (deepest) episode's tool calls, since that run made the decisions.
    emails = conn.execute("SELECT email_id, subject, date FROM emails ORDER BY date").fetchall()
    seq_labels = labels["expected_tool_sequences"]
    matched = total = 0
    print("\nTOOL-SEQUENCE MATCH:")
    for email_row, label in zip(emails, seq_labels):
        eps = conn.execute(
            "SELECT episode_id FROM episodes WHERE email_id=? ORDER BY episode_id DESC LIMIT 1",
            (email_row["email_id"],)).fetchone()
        if eps is None:
            print(f"  MISS  (no episode) {label['subject']} {label['date']}")
            total += 1
            continue
        calls = [r["name"] for r in conn.execute(
            "SELECT name FROM trace WHERE episode_id=? AND kind IN ('plan','tool_call') ORDER BY seq",
            (eps["episode_id"],))]
        ok = subsequence_match(label["required"], calls)
        for forb in label.get("forbidden", []):
            if forb in calls:
                ok = False
        total += 1
        matched += ok
        dt = datetime.fromtimestamp(email_row["date"]).strftime("%m-%d %H:%M")
        print(f"  {'OK  ' if ok else 'FAIL'}  [{dt}] {label['note'][:60]}"
              + ("" if ok else f"\n        actual: {calls}"))
    print(f"  -> {matched}/{total} ({matched / total:.0%})" if total else "  (none)")

    # ---------------- cost ----------------
    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM api_calls").fetchone()
    by_model = conn.execute(
        "SELECT model, COUNT(*) n, SUM(cost_usd) c FROM api_calls GROUP BY model").fetchall()
    escalations = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE escalated_from IS NOT NULL").fetchone()[0]
    print(f"\nCOST: ${cost['c']:.4f} across {cost['n']} API calls "
          f"({escalations} escalation(s))")
    for m in by_model:
        print(f"  {m['model']}: {m['n']} calls, ${m['c']:.4f}")

    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
