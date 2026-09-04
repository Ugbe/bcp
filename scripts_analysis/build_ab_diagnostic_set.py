"""Build a frozen, failure-stratified BrowseComp diagnostic query set.

The output TSV contains only qid and question, so it can be passed directly to
the benchmark runner without leaking gold answers.  The companion manifest
records the frozen stratum and pre-treatment metrics used for selection.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METRICS = ROOT / "analysis" / "atom-electron-1.3-9b-830-audit" / "per_run_metrics.csv"
DEFAULT_QUERIES = ROOT / "topics-qrels" / "queries.tsv"
DEFAULT_OUTPUT = ROOT / "topics-qrels" / "splits" / "atom_electron_failure_stratified_100.tsv"

QUOTAS = [
    ("early_explicit_refusal", 20),
    ("wrong_caveated_candidate", 20),
    ("high_recall_wrong", 15),
    ("duplicate_or_stagnation_failure", 15),
    ("zero_recall_failure", 10),
    ("incomplete_run", 10),
    ("correct_caveated_control", 10),
]


def as_bool(value: object) -> bool:
    return str(value).strip().casefold() == "true"


def as_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def stable_rank(seed: str, category: str, qid: str) -> str:
    return hashlib.sha256(f"{seed}:{category}:{qid}".encode()).hexdigest()


def category_match(category: str, row: dict[str, str]) -> bool:
    completed = as_bool(row.get("is_completed"))
    correct = as_bool(row.get("judge_correct"))
    recall = as_float(row.get("retrieval_recall"))
    if category == "early_explicit_refusal":
        return as_bool(row.get("early_completed_before_budget")) and as_bool(
            row.get("explicit_cannot_determine")
        )
    if category == "wrong_caveated_candidate":
        return (
            as_bool(row.get("caveated_candidate_before_budget"))
            and completed
            and not correct
        )
    if category == "high_recall_wrong":
        return completed and not correct and recall >= 0.5
    if category == "duplicate_or_stagnation_failure":
        return not correct and (
            int(row.get("blocked_exact_duplicate_searches") or 0) > 0
            or int(row.get("high_overlap_searches_ge_7_repeats") or 0) >= 3
        )
    if category == "zero_recall_failure":
        return not correct and recall == 0.0
    if category == "incomplete_run":
        return not completed
    if category == "correct_caveated_control":
        return as_bool(row.get("caveated_candidate_before_budget")) and correct
    raise ValueError(f"Unknown category: {category}")


def selection_priority(category: str, row: dict[str, str], seed: str) -> tuple:
    """Prefer information-rich examples, then use a stable hash for diversity."""
    qid = str(row["query_id"])
    gold_seen = as_bool(row.get("gold_answer_literal_found_in_tool_output"))
    recall = as_float(row.get("retrieval_recall"))
    duplicates = int(row.get("blocked_exact_duplicate_searches") or 0)
    if category in {"early_explicit_refusal", "wrong_caveated_candidate"}:
        return (not gold_seen, -recall, stable_rank(seed, category, qid))
    if category == "duplicate_or_stagnation_failure":
        return (-duplicates, stable_rank(seed, category, qid))
    return (stable_rank(seed, category, qid),)


def build_set(metrics_path: Path, query_path: Path, seed: str) -> list[dict]:
    with metrics_path.open(encoding="utf-8-sig", newline="") as handle:
        metrics = list(csv.DictReader(handle))
    questions: dict[str, str] = {}
    with query_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            if len(row) >= 2:
                questions[row[0].strip()] = row[1]

    selected: list[dict] = []
    used: set[str] = set()
    for category, quota in QUOTAS:
        candidates = [
            row
            for row in metrics
            if row["query_id"] not in used and category_match(category, row)
        ]
        candidates.sort(key=lambda row: selection_priority(category, row, seed))
        chosen = candidates[:quota]
        if len(chosen) != quota:
            raise RuntimeError(
                f"Stratum {category!r} requires {quota} rows but only "
                f"{len(chosen)} non-overlapping candidates are available"
            )
        for row in chosen:
            qid = row["query_id"]
            if qid not in questions:
                raise KeyError(f"Question text missing for qid {qid}")
            used.add(qid)
            selected.append(
                {
                    "query_id": qid,
                    "question": questions[qid],
                    "stratum": category,
                    "baseline_status": row.get("status"),
                    "baseline_correct": as_bool(row.get("judge_correct")),
                    "baseline_total_tool_calls": int(row.get("total_tool_calls") or 0),
                    "baseline_retrieval_recall": as_float(row.get("retrieval_recall")),
                    "baseline_gold_literal_seen": as_bool(
                        row.get("gold_answer_literal_found_in_tool_output")
                    ),
                }
            )
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", default="atom-electron-failure-v1")
    args = parser.parse_args()

    selected = build_set(args.metrics, args.queries, args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerows((row["query_id"], row["question"]) for row in selected)

    manifest_path = args.output.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "seed": args.seed,
                "source_metrics": str(args.metrics),
                "source_queries": str(args.queries),
                "query_tsv": str(args.output),
                "counts": {
                    category: sum(row["stratum"] == category for row in selected)
                    for category, _ in QUOTAS
                },
                "rows": selected,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(selected)} diagnostic queries to {args.output}")
    print(f"Wrote selection manifest to {manifest_path}")


if __name__ == "__main__":
    main()
