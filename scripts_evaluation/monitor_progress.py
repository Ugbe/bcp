"""Live terminal monitor for a BrowseComp-Plus benchmark and its Azure judge.

Reads the run records written by ``search_agent/chat_client.py`` and the eval
records written by ``scripts_evaluation/evaluate_with_azure.py`` straight from
disk, so it needs neither the dashboard nor a finished evaluation summary.
Accuracy follows the evaluator's own rule (``final_correct`` when present,
otherwise the legacy ``judge_result.correct``), and judged recall matches the
evaluator's summary by averaging only queries that have qrel evidence.

Uses the standard library only, so it runs with a bare ``python3`` on a fresh
GPU image:

    python3 scripts_evaluation/monitor_progress.py                 # RUN_NAME from env/.env
    python3 scripts_evaluation/monitor_progress.py --run-name my-run --interval 15
    python3 scripts_evaluation/monitor_progress.py --once --json   # one machine-readable snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = 830
RECENT_WINDOW_SECONDS = 3600
RECENT_RESULTS = 12


@dataclass
class Snapshot:
    run_name: str
    runs_dir: str
    evals_dir: str
    target: int
    runs_done: int = 0
    run_status_counts: Dict[str, int] = field(default_factory=dict)
    judged: int = 0
    pending_judgement: int = 0
    correct: int = 0
    completed_judged: int = 0
    completed_correct: int = 0
    evaluation_status_counts: Dict[str, int] = field(default_factory=dict)
    judged_recall_pct: Optional[float] = None
    live_recall_pct: Optional[float] = None
    avg_tool_calls: Dict[str, float] = field(default_factory=dict)
    runs_last_hour: int = 0
    evals_last_hour: int = 0
    last_run_age_seconds: Optional[float] = None
    last_eval_age_seconds: Optional[float] = None
    oldest_pending_age_seconds: Optional[float] = None
    recent_results: List[Dict[str, object]] = field(default_factory=list)
    unreadable_files: int = 0

    @property
    def accuracy_pct(self) -> Optional[float]:
        return percent(self.correct, self.judged)

    @property
    def accuracy_of_target_pct(self) -> Optional[float]:
        return percent(self.correct, self.target)

    @property
    def completed_accuracy_pct(self) -> Optional[float]:
        return percent(self.completed_correct, self.completed_judged)

    @property
    def eta_seconds(self) -> Optional[float]:
        remaining = max(0, self.target - self.runs_done)
        if remaining == 0:
            return 0.0
        if self.runs_last_hour <= 0:
            return None
        return remaining / self.runs_last_hour * RECENT_WINDOW_SECONDS

    def to_dict(self) -> Dict[str, object]:
        data = dict(self.__dict__)
        data.update(
            accuracy_pct=self.accuracy_pct,
            accuracy_of_target_pct=self.accuracy_of_target_pct,
            completed_accuracy_pct=self.completed_accuracy_pct,
            eta_seconds=self.eta_seconds,
        )
        return data


def percent(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator * 100.0, 2) if denominator > 0 else None


def read_dotenv(path: Path) -> Dict[str, str]:
    """Minimal KEY=VALUE reader; shell-sourced .env files use the same shape."""
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        # The evaluator writes eval files in place, so a poll can race a write.
        return None


def load_qrel_evidence(path: Path) -> Dict[str, List[str]]:
    evidence: Dict[str, List[str]] = {}
    if not path.is_file():
        return evidence
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 4:
            evidence.setdefault(parts[0], []).append(parts[2])
    return evidence


def count_target(query_file: Optional[Path]) -> int:
    if query_file is None or not query_file.is_file():
        return DEFAULT_TARGET
    count = sum(
        1
        for line in query_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and "\t" in line
    )
    return count or DEFAULT_TARGET


def eval_is_correct(record: dict) -> bool:
    """Mirror evaluate_with_azure.build_summary's final_correct rule."""
    if "final_correct" in record:
        return record.get("final_correct") is True
    return bool((record.get("judge_result") or {}).get("correct", False))


def recall(retrieved: Iterable[str], positives: List[str]) -> float:
    positive_set = set(positives)
    return len(set(map(str, retrieved)) & positive_set) / len(positive_set)


def collect(
    run_name: str,
    runs_dir: Path,
    evals_dir: Path,
    target: int,
    qrel_evidence: Dict[str, List[str]],
    now: Optional[float] = None,
) -> Snapshot:
    now = time.time() if now is None else now
    snapshot = Snapshot(
        run_name=run_name,
        runs_dir=str(runs_dir),
        evals_dir=str(evals_dir),
        target=target,
    )

    run_files = sorted(runs_dir.glob("run_*.json")) if runs_dir.is_dir() else []
    eval_files = sorted(evals_dir.glob("run_*_eval.json")) if evals_dir.is_dir() else []
    eval_stems = {path.name[: -len("_eval.json")] for path in eval_files}

    status_counts: Counter = Counter()
    tool_totals: Counter = Counter()
    live_recalls: List[float] = []
    run_mtimes: List[float] = []
    for path in run_files:
        record = load_json(path)
        if record is None:
            snapshot.unreadable_files += 1
            continue
        run_mtimes.append(path.stat().st_mtime)
        status_counts[str(record.get("status") or "missing")] += 1
        for tool, count in (record.get("tool_call_counts") or {}).items():
            if isinstance(count, (int, float)):
                tool_totals[str(tool)] += count
        positives = qrel_evidence.get(str(record.get("query_id") or ""))
        if positives:
            live_recalls.append(recall(record.get("retrieved_docids") or [], positives))

    snapshot.runs_done = sum(status_counts.values())
    snapshot.run_status_counts = dict(status_counts.most_common())
    pending_mtimes = [path.stat().st_mtime for path in run_files if path.stem not in eval_stems]
    snapshot.pending_judgement = len(pending_mtimes)
    if pending_mtimes:
        snapshot.oldest_pending_age_seconds = max(0.0, now - min(pending_mtimes))
    if snapshot.runs_done:
        snapshot.avg_tool_calls = {
            tool: round(total / snapshot.runs_done, 2) for tool, total in tool_totals.most_common()
        }
    if live_recalls:
        snapshot.live_recall_pct = round(sum(live_recalls) / len(live_recalls) * 100.0, 2)
    if run_mtimes:
        snapshot.runs_last_hour = sum(1 for m in run_mtimes if now - m <= RECENT_WINDOW_SECONDS)
        snapshot.last_run_age_seconds = max(0.0, now - max(run_mtimes))

    evaluation_counts: Counter = Counter()
    judged_recalls: List[float] = []
    eval_rows = []
    for path in eval_files:
        record = load_json(path)
        if record is None:
            snapshot.unreadable_files += 1
            continue
        mtime = path.stat().st_mtime
        correct = eval_is_correct(record)
        snapshot.judged += 1
        snapshot.correct += int(correct)
        evaluation_counts[str(record.get("evaluation_status") or "legacy")] += 1
        if record.get("is_completed", True):
            snapshot.completed_judged += 1
            snapshot.completed_correct += int(correct)
        query_id = str(record.get("query_id") or "")
        if qrel_evidence.get(query_id):
            value = (record.get("retrieval") or {}).get("recall")
            judged_recalls.append(float(value) if isinstance(value, (int, float)) else 0.0)
        eval_rows.append((mtime, query_id, correct, record))

    snapshot.evaluation_status_counts = dict(evaluation_counts.most_common())
    if judged_recalls:
        snapshot.judged_recall_pct = round(sum(judged_recalls) / len(judged_recalls) * 100.0, 2)
    if eval_rows:
        mtimes = [row[0] for row in eval_rows]
        snapshot.evals_last_hour = sum(1 for m in mtimes if now - m <= RECENT_WINDOW_SECONDS)
        snapshot.last_eval_age_seconds = max(0.0, now - max(mtimes))
        for _, query_id, correct, record in sorted(eval_rows, key=lambda row: row[0])[-RECENT_RESULTS:]:
            snapshot.recent_results.append(
                {
                    "query_id": query_id,
                    "correct": correct,
                    "run_status": record.get("run_status", "completed"),
                    "tool_calls": sum(
                        v for v in (record.get("tool_call_counts") or {}).values() if isinstance(v, (int, float))
                    ),
                }
            )
    return snapshot


def fmt_pct(value: Optional[float]) -> str:
    return "  n/a" if value is None else f"{value:5.2f}%"


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s ago"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m ago"


def fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a (no runs in the last hour)"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d{hours:02d}h{minutes:02d}m" if days else f"{hours}h{minutes:02d}m"


def bar(done: int, total: int, width: int = 32) -> str:
    filled = min(width, int(width * done / total)) if total > 0 else 0
    return "#" * filled + "." * (width - filled)


def format_counts(counts: Dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items()) or "none"


def warnings(snapshot: Snapshot, stall_minutes: float) -> List[str]:
    notes = []
    stall_seconds = stall_minutes * 60
    if not Path(snapshot.runs_dir).is_dir():
        notes.append(f"run directory does not exist yet: {snapshot.runs_dir}")
    if (
        snapshot.runs_done < snapshot.target
        and snapshot.last_run_age_seconds is not None
        and snapshot.last_run_age_seconds > stall_seconds
    ):
        notes.append(
            f"newest run record is {fmt_age(snapshot.last_run_age_seconds)}; "
            "check the benchmark window and the model server"
        )
    # The evaluator polls every few seconds, so a run left unjudged for the
    # whole stall window means the evaluator is not running (or is failing).
    if (
        snapshot.oldest_pending_age_seconds is not None
        and snapshot.oldest_pending_age_seconds > stall_seconds
    ):
        notes.append(
            f"{snapshot.pending_judgement} run(s) unjudged, oldest written "
            f"{fmt_age(snapshot.oldest_pending_age_seconds)}; check that the evaluator window is alive"
        )
    if snapshot.unreadable_files:
        notes.append(f"{snapshot.unreadable_files} file(s) were mid-write or unreadable this poll")
    return notes


def render(snapshot: Snapshot, stall_minutes: float) -> str:
    target = snapshot.target
    lines = [
        f"BrowseComp-Plus monitor  |  {snapshot.run_name}  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 78,
        f"Runs      [{bar(snapshot.runs_done, target)}] {snapshot.runs_done}/{target} "
        f"({fmt_pct(percent(snapshot.runs_done, target)).strip()})",
        f"          status: {format_counts(snapshot.run_status_counts)}",
        f"          last hour: {snapshot.runs_last_hour} runs  |  last record {fmt_age(snapshot.last_run_age_seconds)}  "
        f"|  ETA {fmt_duration(snapshot.eta_seconds)}",
        f"Judged    [{bar(snapshot.judged, target)}] {snapshot.judged}/{target} "
        f"({fmt_pct(percent(snapshot.judged, target)).strip()})  |  awaiting judge: {snapshot.pending_judgement}",
        f"          last hour: {snapshot.evals_last_hour} evals  |  last eval {fmt_age(snapshot.last_eval_age_seconds)}",
        f"          status: {format_counts(snapshot.evaluation_status_counts)}",
        "-" * 78,
        f"Accuracy  {fmt_pct(snapshot.accuracy_pct)}  ({snapshot.correct}/{snapshot.judged} judged)",
        f"          {fmt_pct(snapshot.completed_accuracy_pct)}  completed runs only "
        f"({snapshot.completed_correct}/{snapshot.completed_judged})",
        f"          {fmt_pct(snapshot.accuracy_of_target_pct)}  of full target ({snapshot.correct}/{target}, unjudged count as wrong)",
        f"Recall    {fmt_pct(snapshot.judged_recall_pct)}  judged queries (matches evaluation_summary.json)",
        f"          {fmt_pct(snapshot.live_recall_pct)}  all finished runs, including not yet judged",
        f"Tools/run {format_counts(snapshot.avg_tool_calls) if snapshot.avg_tool_calls else 'n/a'}",
    ]
    if snapshot.recent_results:
        marks = []
        for item in snapshot.recent_results:
            mark = "OK" if item["correct"] else ("--" if item["run_status"] == "completed" else "..")
            marks.append(f"{item['query_id']}:{mark}")
        lines += ["-" * 78, "Latest judged (qid:OK correct, -- wrong, .. unfinished run):", "  " + "  ".join(marks)]
    notes = warnings(snapshot, stall_minutes)
    if notes:
        lines += ["-" * 78] + [f"WARNING: {note}" for note in notes]
    lines += ["=" * 78, f"runs:  {snapshot.runs_dir}", f"evals: {snapshot.evals_dir}"]
    return "\n".join(lines)


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    dotenv = read_dotenv(REPO_ROOT / ".env")

    def setting(name: str, fallback: Optional[str] = None) -> Optional[str]:
        return os.environ.get(name) or dotenv.get(name) or fallback

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-name", default=setting("RUN_NAME"), help="Defaults to $RUN_NAME or RUN_NAME in .env")
    parser.add_argument("--runs-dir", help="Override runs/<run-name>")
    parser.add_argument("--evals-dir", help="Override evals/<run-name>")
    parser.add_argument(
        "--query-file",
        default=setting("QUERY_FILE", "topics-qrels/queries.tsv"),
        help="Query TSV that defines the target count (default: $QUERY_FILE or topics-qrels/queries.tsv)",
    )
    parser.add_argument("--target", type=int, help="Override the target query count")
    parser.add_argument("--qrel-evidence", default="topics-qrels/qrel_evidence.txt")
    parser.add_argument("--interval", type=float, default=30.0, help="Seconds between refreshes (default 30)")
    parser.add_argument(
        "--stall-minutes", type=float, default=15.0, help="Warn when no new run/eval appears for this long"
    )
    parser.add_argument("--once", action="store_true", help="Print one snapshot and exit")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the text view")
    args = parser.parse_args(argv)
    if not args.run_name and not (args.runs_dir and args.evals_dir):
        parser.error("set RUN_NAME (env or .env), pass --run-name, or pass both --runs-dir and --evals-dir")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    runs_dir = resolve_path(args.runs_dir) if args.runs_dir else REPO_ROOT / "runs" / args.run_name
    evals_dir = resolve_path(args.evals_dir) if args.evals_dir else REPO_ROOT / "evals" / args.run_name
    run_name = args.run_name or runs_dir.name
    qrel_evidence = load_qrel_evidence(resolve_path(args.qrel_evidence))
    target = args.target or count_target(resolve_path(args.query_file))
    interactive = sys.stdout.isatty() and not args.once and not args.json

    try:
        while True:
            snapshot = collect(run_name, runs_dir, evals_dir, target, qrel_evidence)
            if args.json:
                print(json.dumps(snapshot.to_dict(), ensure_ascii=False), flush=True)
            else:
                text = render(snapshot, args.stall_minutes)
                if interactive:
                    sys.stdout.write("\x1b[2J\x1b[H")
                    text += f"\nrefresh every {args.interval:g}s  |  Ctrl+C to quit (runs are unaffected)"
                print(text, flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
