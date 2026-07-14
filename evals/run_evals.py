"""Eval harness: score a completed run against ground truth + labeled tool sequences.

    python evals/run_evals.py --db data/pbc.db \\
        --groundtruth input/sample/sample_groundtruth.json --labels evals/labels.json

Reports: per-status precision/recall, insufficiency-detection F1, expected
tool-call-sequence match rate, and measured cost. No LLM calls. The scoring
logic lives in `evaluate()` so the Streamlit UI can run it in-process.
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


def evaluate(db: str = "data/pbc.db",
             groundtruth: str = "input/sample/sample_groundtruth.json",
             labels_path: str = "evals/labels.json") -> dict:
    """Score a run. Returns a plain dict (JSON-safe) for CLI or UI rendering."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    gt = json.loads(Path(groundtruth).read_text())["expected_status"]
    labels = json.loads(Path(labels_path).read_text())
    equiv = labels.get("status_equivalences", {})

    # ---------------- status scoring ----------------
    predicted = {r["item_id"]: normalize(r["status"], equiv)
                 for r in conn.execute("SELECT item_id, status FROM items")}
    expected = {k: v["status"] for k, v in gt.items()}

    per_class: dict[str, Counter] = defaultdict(Counter)
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
            mismatches.append({"item_id": item_id, "expected": exp, "predicted": pred})

    classes = {}
    for cls in sorted(set(expected.values()) | set(predicted.values())):
        c = per_class[cls]
        p, r, f = prf(c["tp"], c["fp"], c["fn"])
        classes[cls] = {"precision": p, "recall": r, "f1": f,
                        "support": c["tp"] + c["fn"]}

    tp = sum(1 for k in expected
             if expected[k] == "Insufficient" and predicted.get(k) == "Insufficient")
    fp = sum(1 for k in expected
             if expected[k] != "Insufficient" and predicted.get(k) == "Insufficient")
    fn = sum(1 for k in expected
             if expected[k] == "Insufficient" and predicted.get(k) != "Insufficient")
    ip, ir, if1 = prf(tp, fp, fn)

    # ---------------- tool-sequence scoring ----------------
    # Episodes in email-chronological order; for escalated emails, judge the
    # final (deepest) episode's tool calls, since that run made the decisions.
    emails = conn.execute("SELECT email_id, subject, date FROM emails ORDER BY date").fetchall()
    sequences = []
    matched = 0
    for email_row, label in zip(emails, labels["expected_tool_sequences"]):
        eps = conn.execute(
            "SELECT episode_id FROM episodes WHERE email_id=? ORDER BY episode_id DESC LIMIT 1",
            (email_row["email_id"],)).fetchone()
        calls = [] if eps is None else [r["name"] for r in conn.execute(
            "SELECT name FROM trace WHERE episode_id=? AND kind IN ('plan','tool_call') ORDER BY seq",
            (eps["episode_id"],))]
        ok = eps is not None and subsequence_match(label["required"], calls)
        violated = [f for f in label.get("forbidden", []) if f in calls]
        if violated:
            ok = False
        if ok:
            matched += 1
        sequences.append({
            "email_id": email_row["email_id"],
            "date": datetime.fromtimestamp(email_row["date"]).strftime("%m-%d %H:%M"),
            "subject": email_row["subject"], "note": label["note"], "ok": ok,
            "required": label["required"], "forbidden_hit": violated, "actual": calls,
            "no_episode": eps is None,
        })

    # ---------------- cost ----------------
    cost = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) c, COUNT(*) n FROM api_calls").fetchone()
    by_model = [dict(r) for r in conn.execute(
        "SELECT model, COUNT(*) n, SUM(cost_usd) c FROM api_calls GROUP BY model")]
    escalations = conn.execute(
        "SELECT COUNT(*) FROM episodes WHERE escalated_from IS NOT NULL").fetchone()[0]

    return {
        "status": {"correct": correct, "total": len(expected), "classes": classes,
                   "mismatches": mismatches},
        "insufficiency": {"precision": ip, "recall": ir, "f1": if1,
                          "tp": tp, "fp": fp, "fn": fn},
        "sequences": {"matched": matched, "total": len(sequences), "rows": sequences},
        "cost": {"total_usd": cost["c"], "calls": cost["n"],
                 "escalations": escalations, "by_model": by_model},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/pbc.db")
    ap.add_argument("--groundtruth", default="input/sample/sample_groundtruth.json")
    ap.add_argument("--labels", default="evals/labels.json")
    args = ap.parse_args()

    r = evaluate(args.db, args.groundtruth, args.labels)

    print("=" * 64)
    s = r["status"]
    print(f"STATUS ACCURACY: {s['correct']}/{s['total']} ({s['correct'] / s['total']:.0%})")
    print(f"{'status':<14}{'precision':>10}{'recall':>10}{'f1':>8}")
    for cls, m in s["classes"].items():
        print(f"{cls:<14}{m['precision']:>10.2f}{m['recall']:>10.2f}{m['f1']:>8.2f}")
    if s["mismatches"]:
        print("\nMismatches (item: expected -> predicted):")
        for m in s["mismatches"]:
            print(f"  {m['item_id']}: {m['expected']} -> {m['predicted']}")

    i = r["insufficiency"]
    print(f"\nINSUFFICIENCY DETECTION: precision={i['precision']:.2f} "
          f"recall={i['recall']:.2f} F1={i['f1']:.2f}")

    q = r["sequences"]
    print("\nTOOL-SEQUENCE MATCH:")
    for row in q["rows"]:
        print(f"  {'OK  ' if row['ok'] else 'FAIL'}  [{row['date']}] {row['note'][:60]}"
              + ("" if row["ok"] else f"\n        actual: {row['actual']}"))
    if q["total"]:
        print(f"  -> {q['matched']}/{q['total']} ({q['matched'] / q['total']:.0%})")

    c = r["cost"]
    print(f"\nCOST: ${c['total_usd']:.4f} across {c['calls']} API calls "
          f"({c['escalations']} escalation(s))")
    for m in c["by_model"]:
        print(f"  {m['model']}: {m['n']} calls, ${m['c']:.4f}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
