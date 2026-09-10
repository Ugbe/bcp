"""Compare evaluator summaries for frozen Phase 1 A/B treatments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    candidate = path / "evaluation_summary.json" if path.is_dir() else path
    return json.loads(candidate.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare accuracy, recall, and tool cost across Phase 1 treatments.")
    parser.add_argument("baseline", type=Path)
    parser.add_argument("treatments", nargs="+", type=Path)
    args = parser.parse_args()
    baseline = load(args.baseline)

    def row(label: str, summary: dict) -> dict:
        stats = summary.get("avg_tool_stats") or {}
        return {
            "treatment": label,
            "accuracy_pct": summary.get("Accuracy (%)"),
            "recall_pct": summary.get("Recall (%)"),
            "avg_search_calls": stats.get("search", 0),
            "calibration_error_pct": summary.get("Calibration Error (%)"),
        }

    rows = [row("baseline", baseline)] + [row(path.name, load(path)) for path in args.treatments]
    base = rows[0]
    for item in rows:
        item["accuracy_delta_vs_baseline"] = round((item["accuracy_pct"] or 0) - (base["accuracy_pct"] or 0), 3)
        item["recall_delta_vs_baseline"] = round((item["recall_pct"] or 0) - (base["recall_pct"] or 0), 3)
    print(json.dumps({"baseline": str(args.baseline), "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
