"""Triage a treatment run against qrels: where does gold get lost?

Reads runs/<run>/run_qid_*.json, evals/<run>/run_qid_*_eval.json (optional) and
topics-qrels/qrel_evidence.txt. For every run it reports whether gold evidence
appeared in the first search, whether gold was ever opened in full, whether the
planner saw raw text or only evidence notes, evidence-note failures, fresh-final
replacements, compaction events, and the run/judge status. Aggregates the same
counters over correct versus wrong runs so the dominant loss stage is visible.

Usage:
    python scripts_analysis/triage_treatment_run.py runs/<run> [evals/<run>]
        [--qrels topics-qrels/qrel_evidence.txt] [--csv out.csv]
"""

from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import os
import re
from pathlib import Path

DOCID_RE = re.compile(r'"docid":\s*"?(\d+)"?')
SEARCH_TOOLS = {"search", "deep_search"}
OPEN_TOOLS = {"get_document", "get_documents"}


def load_qrels(path: Path) -> dict[str, set[str]]:
    qrels: dict[str, set[str]] = collections.defaultdict(set)
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] != "0":
            qrels[parts[0]].add(parts[2])
    return qrels


def load_evals(eval_dir: Path | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if eval_dir is None:
        return out
    for file in glob.glob(str(eval_dir / "run_qid_*_eval.json")):
        try:
            record = json.loads(Path(file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        qid = str(record.get("qid") or os.path.basename(file).split("_")[2])
        correct = record.get("final_correct")
        if correct is None:
            correct = (record.get("judge_result") or {}).get("correct")
        out[qid] = {"correct": correct}
    return out


def docids_in(text: str) -> list[str]:
    return list(dict.fromkeys(DOCID_RE.findall(text or "")))


def tool_outputs(record: dict) -> list[dict]:
    diagnostics = record.get("diagnostics") or {}
    raw = diagnostics.get("raw_tool_outputs") or []
    if raw:
        return [
            {
                "tool": item.get("tool_name"),
                "output": str(item.get("output") or ""),
            }
            for item in raw
        ]
    out = []
    for item in record.get("result") or []:
        if item.get("type") == "tool_call":
            out.append(
                {"tool": item.get("tool_name"), "output": str(item.get("output") or "")}
            )
    return out


def triage_record(record: dict, gold: set[str], judged: dict | None) -> dict:
    diagnostics = record.get("diagnostics") or {}
    outputs = tool_outputs(record)
    searches = [
        item
        for item in outputs
        if item["tool"] in SEARCH_TOOLS or "search" in str(item["tool"] or "")
    ]
    opens = [item for item in outputs if item["tool"] in OPEN_TOOLS]
    first_search_docids = docids_in(searches[0]["output"]) if searches else []
    first_gold_ranks = [
        index + 1 for index, docid in enumerate(first_search_docids) if docid in gold
    ]
    all_returned: set[str] = set()
    for item in searches:
        all_returned.update(docids_in(item["output"]))
    opened = set(diagnostics.get("opened_docids") or [])
    for item in opens:
        opened.update(docids_in(item["output"]))
    notes = diagnostics.get("evidence_notes") or []
    note_lengths = [len(str(note.get("note") or "")) for note in notes]
    table_updates = sum(
        1 for note in notes if "CANDIDATE_TABLE_JSON" in str(note.get("note") or "")
    )
    return {
        "qid": str(record.get("query_id")),
        "status": record.get("status"),
        "correct": None if judged is None else judged.get("correct"),
        "tool_calls": sum((record.get("tool_call_counts") or {}).values()),
        "productive_calls": diagnostics.get("productive_tool_calls"),
        "gold_count": len(gold),
        "gold_in_first_search": len(first_gold_ranks),
        "first_gold_rank": min(first_gold_ranks) if first_gold_ranks else None,
        "gold_recall_all_searches": (len(all_returned & gold) / len(gold)) if gold else None,
        "gold_opened": len(opened & gold),
        "docs_opened": len(opened),
        "evidence_notes": len(notes),
        "notes_with_table": table_updates,
        "note_max_chars": max(note_lengths) if note_lengths else 0,
        "note_failures": diagnostics.get("evidence_note_failures"),
        "compactions": diagnostics.get("context_compaction_count"),
        "early_final_guard": diagnostics.get("early_final_guard_triggered"),
        "stagnation_hints": diagnostics.get("stagnation_guidance_count"),
        "fresh_final_used": diagnostics.get("fresh_final_used"),
        "fresh_final_status": diagnostics.get("fresh_final_status"),
        "conversation_answer": diagnostics.get("conversation_final_answer"),
        "fresh_answer": diagnostics.get("fresh_final_answer"),
        "planner_saw_raw_text": not notes,
    }


def _pct(values) -> str:
    values = [value for value in values if value is not None]
    return f"{100 * sum(values) / len(values):.1f}%" if values else "n/a"


def _mean(values) -> str:
    values = [value for value in values if value is not None]
    return f"{sum(values) / len(values):.2f}" if values else "n/a"


def summarize(rows: list[dict]) -> None:
    groups = {"all": rows}
    judged = [row for row in rows if row["correct"] is not None]
    if judged:
        groups["correct"] = [row for row in judged if row["correct"]]
        groups["wrong"] = [row for row in judged if not row["correct"]]
    print(f"{'metric':38}" + "".join(f"{name:>12}" for name in groups))
    print("-" * (38 + 12 * len(groups)))
    lines = [
        ("runs", lambda g: str(len(g))),
        ("accuracy (judged)", lambda g: _pct([r["correct"] for r in g])),
        ("status completed", lambda g: _pct([r["status"] == "completed" for r in g])),
        (
            "gold in first search (any)",
            lambda g: _pct([r["gold_in_first_search"] > 0 for r in g if r["gold_count"]]),
        ),
        ("mean first gold rank", lambda g: _mean([r["first_gold_rank"] for r in g])),
        (
            "mean gold recall over searches",
            lambda g: _mean([r["gold_recall_all_searches"] for r in g]),
        ),
        (
            "runs opening >=1 gold doc",
            lambda g: _pct([r["gold_opened"] > 0 for r in g if r["gold_count"]]),
        ),
        ("mean docs opened", lambda g: _mean([r["docs_opened"] for r in g])),
        ("mean tool calls", lambda g: _mean([r["tool_calls"] for r in g])),
        ("mean evidence notes", lambda g: _mean([r["evidence_notes"] for r in g])),
        ("mean notes with table JSON", lambda g: _mean([r["notes_with_table"] for r in g])),
        ("runs with note failures", lambda g: _pct([bool(r["note_failures"]) for r in g])),
        ("runs compacted", lambda g: _pct([bool(r["compactions"]) for r in g])),
        ("early-final guard fired", lambda g: _pct([bool(r["early_final_guard"]) for r in g])),
        ("stagnation hints (mean)", lambda g: _mean([r["stagnation_hints"] for r in g])),
        ("fresh final replaced answer", lambda g: _pct([bool(r["fresh_final_used"]) for r in g])),
    ]
    for label, fn in lines:
        print(f"{label:38}" + "".join(f"{fn(group):>12}" for group in groups.values()))

    flips = [row for row in rows if row["fresh_final_used"] and row["correct"] is not None]
    if flips:
        print(
            f"\nfresh-final replacements judged: {len(flips)}, correct after replacement: "
            f"{sum(1 for row in flips if row['correct'])}"
        )
        for row in flips[:15]:
            mark = "OK " if row["correct"] else "BAD"
            print(
                f"  qid {row['qid']}: {mark} conv={row['conversation_answer']!r} "
                f"-> fresh={row['fresh_answer']!r}"
            )

    lost = [row for row in rows if row["correct"] is False and row["gold_in_first_search"] > 0]
    if lost:
        print(
            f"\nwrong despite gold in first search: {len(lost)} qids: "
            + ", ".join(row["qid"] for row in lost[:40])
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("eval_dir", type=Path, nargs="?")
    parser.add_argument("--qrels", type=Path, default=Path("topics-qrels/qrel_evidence.txt"))
    parser.add_argument("--csv", type=Path)
    args = parser.parse_args()

    qrels = load_qrels(args.qrels)
    evals = load_evals(args.eval_dir)
    rows = []
    for file in sorted(glob.glob(str(args.run_dir / "run_qid_*.json"))):
        try:
            record = json.loads(Path(file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        qid = str(record.get("query_id"))
        rows.append(triage_record(record, qrels.get(qid, set()), evals.get(qid)))
    if not rows:
        raise SystemExit(f"no run records under {args.run_dir}")
    summarize(rows)
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nper-run rows written to {args.csv}")


if __name__ == "__main__":
    main()
