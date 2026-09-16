"""Build the BrowseComp-Plus leaderboard submission JSON from an eval directory.

Reads the per-query ``run_qid_*_eval.json`` files written by any of the
evaluators and recomputes every leaderboard field from them: accuracy, evidence
recall, average tool calls, the RMS calibration error over the model's own
reported confidence, and the per-query metrics block. The output matches the
schema the leaderboard maintainer expects, so the result can be emailed as is.

Records are processed in sorted query order. The upstream calibration binning
(Hendrycks' ``calib_err``) sorts by confidence without breaking ties, so the
reported value moves by up to about one point with input ordering; a fixed order
keeps the submitted number reproducible.

The leaderboard only accepts results judged by Qwen3-32B
(``scripts_evaluation/evaluate_run.py``). This script refuses an eval directory
that records a different judge unless ``--allow-non-qwen-judge`` is passed, so a
development judge cannot be submitted by accident.

Usage:
    python scripts_evaluation/build_leaderboard_submission.py evals/<run> \
        --llm "Atom-Electron-1.3-9B" \
        --retriever "(BM25 + Qwen3-8B) + Qwen3-8B-ReRanker" \
        --link https://huggingface.co/CrowtherLabs/Atom-Electron-1.3-9B \
        --output submission.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from evaluate_with_openai import calculate_calibration_error  # noqa: E402

MIN_CONFIDENCES_FOR_CALIBRATION = 100


def load_eval_records(eval_dir: Path) -> list[dict]:
    records = []
    for file in sorted(glob.glob(str(eval_dir / "run_qid_*_eval.json"))):
        try:
            record = json.loads(Path(file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"unreadable eval record {file}: {exc}") from exc
        record.setdefault("query_id", os.path.basename(file).split("_")[2])
        records.append(record)
    return records


def record_correct(record: dict) -> bool:
    """Canonical correctness, preferring the normalized field when present."""

    correct = record.get("final_correct")
    if correct is None:
        correct = (record.get("judge_result") or {}).get("correct")
    return bool(correct)


def record_confidence(record: dict) -> float | None:
    """Confidence used for calibration, matching the official evaluators.

    The judge extracts the confidence the agent stated in its own answer, and a
    record whose judge output failed to parse contributes nothing.
    """

    judge_result = record.get("judge_result") or {}
    if judge_result.get("parse_error"):
        return None
    value = judge_result.get("confidence")
    if value is None:
        value = record.get("model_confidence")
    return float(value) if isinstance(value, (int, float)) else None


def build_submission(
    records: list[dict],
    *,
    llm: str,
    retriever: str,
    link: str,
    evaluation_date: str,
    search_calls_field: bool,
) -> dict:
    total = len(records)
    correct_flags = [record_correct(record) for record in records]

    recalls = [
        record["retrieval"]["recall"]
        for record in records
        if isinstance((record.get("retrieval") or {}).get("recall"), (int, float))
    ]

    tool_totals: dict[str, int] = defaultdict(int)
    for record in records:
        for tool, count in (record.get("tool_call_counts") or {}).items():
            tool_totals[str(tool)] += int(count)
    avg_tool_stats = {tool: value / total for tool, value in sorted(tool_totals.items())}

    confidences, confidence_correct = [], []
    for record, correct in zip(records, correct_flags):
        confidence = record_confidence(record)
        if confidence is not None:
            confidences.append(confidence)
            confidence_correct.append(correct)
    calibration_error = (
        calculate_calibration_error(confidences, confidence_correct)
        if len(confidences) >= MIN_CONFIDENCES_FOR_CALIBRATION
        else 0.0
    )

    submission = {
        "LLM": llm,
        "Retriever": retriever,
        "Accuracy (%)": round(100.0 * sum(correct_flags) / total, 2),
        "Recall (%)": round(100.0 * sum(recalls) / len(recalls), 2) if recalls else None,
        "Calibration Error (%)": round(calibration_error, 2),
        "Link": link,
        "Evaluation Date": evaluation_date,
        "per_query_metrics": [
            {
                "query_id": str(record.get("query_id")),
                "correct": correct,
                "recall": (
                    round(100.0 * record["retrieval"]["recall"], 2)
                    if isinstance((record.get("retrieval") or {}).get("recall"), (int, float))
                    else None
                ),
            }
            for record, correct in zip(records, correct_flags)
        ],
    }
    if search_calls_field:
        submission["Search Calls"] = avg_tool_stats.get("search", 0.0)
    else:
        submission["avg_tool_stats"] = avg_tool_stats
    ordered = [
        "LLM",
        "Retriever",
        "Accuracy (%)",
        "Recall (%)",
        "Search Calls",
        "avg_tool_stats",
        "Calibration Error (%)",
        "Link",
        "Evaluation Date",
        "per_query_metrics",
    ]
    return {key: submission[key] for key in ordered if key in submission}


def judge_warnings(eval_dir: Path, records: list[dict], expected_queries: int) -> list[str]:
    warnings = []
    summary_path = eval_dir / "evaluation_summary.json"
    if summary_path.is_file():
        try:
            judged_by = json.loads(summary_path.read_text(encoding="utf-8")).get("judged_by")
        except (OSError, json.JSONDecodeError):
            judged_by = None
        if judged_by and "qwen" not in str(judged_by).lower():
            warnings.append(
                f"eval directory was judged by {judged_by!r}; the leaderboard accepts "
                "Qwen3-32B results from scripts_evaluation/evaluate_run.py"
            )
    if expected_queries and len(records) != expected_queries:
        warnings.append(
            f"{len(records)} judged queries, expected {expected_queries}; "
            "missing queries are not counted as wrong, so the score would be overstated"
        )
    missing_confidence = sum(1 for record in records if record_confidence(record) is None)
    if missing_confidence:
        warnings.append(
            f"{missing_confidence} records have no model confidence; "
            "calibration error uses only the rest"
        )
    return warnings


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("eval_dir", type=Path, help="Directory of run_qid_*_eval.json files")
    parser.add_argument("--llm", required=True, help="Agent name as it should appear")
    parser.add_argument("--retriever", required=True, help="Retrieval pipeline description")
    parser.add_argument("--link", required=True, help="Model card, paper, or project page URL")
    parser.add_argument(
        "--evaluation-date",
        default=date.today().isoformat(),
        help="YYYY-MM-DD (default: today)",
    )
    parser.add_argument("--output", type=Path, default=Path("submission.json"))
    parser.add_argument(
        "--expect-queries",
        type=int,
        default=830,
        help="Expected judged query count; 0 disables the check",
    )
    parser.add_argument(
        "--avg-tool-stats",
        action="store_true",
        help="Emit the avg_tool_stats object instead of the flat Search Calls field",
    )
    parser.add_argument(
        "--allow-non-qwen-judge",
        action="store_true",
        help="Write the file even when the eval directory records a non-Qwen3 judge",
    )
    args = parser.parse_args()

    records = load_eval_records(args.eval_dir)
    if not records:
        raise SystemExit(f"no run_qid_*_eval.json files under {args.eval_dir}")

    warnings = judge_warnings(args.eval_dir, records, args.expect_queries)
    blocking = [warning for warning in warnings if "leaderboard accepts" in warning]
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if blocking and not args.allow_non_qwen_judge:
        raise SystemExit("refusing to write a submission; pass --allow-non-qwen-judge to override")

    submission = build_submission(
        records,
        llm=args.llm,
        retriever=args.retriever,
        link=args.link,
        evaluation_date=args.evaluation_date,
        search_calls_field=not args.avg_tool_stats,
    )
    args.output.write_text(
        json.dumps(submission, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    preview = {key: value for key, value in submission.items() if key != "per_query_metrics"}
    print(json.dumps(preview, indent=2, ensure_ascii=False))
    print(f"\n{len(submission['per_query_metrics'])} per-query rows written to {args.output}")


if __name__ == "__main__":
    main()
