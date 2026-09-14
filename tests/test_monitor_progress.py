import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from scripts_evaluation import monitor_progress as monitor


QRELS = {"1": ["a", "b"], "2": ["c"], "3": ["d", "e", "f", "g"]}


def write_json(path: Path, data: dict, age_seconds: float, now: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    stamp = now - age_seconds
    os.utime(path, (stamp, stamp))


class MonitorProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs = self.root / "runs" / "demo"
        self.evals = self.root / "evals" / "demo"
        self.now = time.time()

    def tearDown(self):
        self.temp.cleanup()

    def add_run(self, qid, status="completed", retrieved=(), tools=None, age=60):
        write_json(
            self.runs / f"run_qid_{qid}.json",
            {
                "query_id": str(qid),
                "status": status,
                "retrieved_docids": list(retrieved),
                "tool_call_counts": tools or {"search": 4, "get_document": 1},
            },
            age,
            self.now,
        )

    def add_eval(self, qid, correct, recall_value, legacy=False, completed=True, age=30):
        record = {
            "query_id": str(qid),
            "is_completed": completed,
            "run_status": "completed" if completed else "incomplete_length",
            "tool_call_counts": {"search": 4, "get_document": 1},
            "retrieval": {"recall": recall_value},
        }
        if legacy:
            record["judge_result"] = {"correct": correct}
        else:
            record["final_correct"] = correct
            record["evaluation_status"] = "judge_completed" if completed else "run_incomplete"
        write_json(self.evals / f"run_qid_{qid}_eval.json", record, age, self.now)

    def collect(self, target=10):
        return monitor.collect("demo", self.runs, self.evals, target, QRELS, now=self.now)

    def test_accuracy_and_recall_follow_evaluator_rules(self):
        self.add_run(1, retrieved=["a", "b"])
        self.add_run(2, retrieved=["x"])
        self.add_run(3, status="incomplete_length", retrieved=["d"])
        self.add_run(4, retrieved=["zzz"])  # no qrels: excluded from recall means
        self.add_eval(1, True, 1.0)
        self.add_eval(2, True, 0.0, legacy=True)
        self.add_eval(3, None, 0.25, completed=False)
        self.add_eval(4, False, 0.0)

        snap = self.collect()

        self.assertEqual(snap.runs_done, 4)
        self.assertEqual(snap.run_status_counts, {"completed": 3, "incomplete_length": 1})
        self.assertEqual((snap.judged, snap.correct), (4, 2))
        self.assertEqual(snap.accuracy_pct, 50.0)
        self.assertEqual((snap.completed_judged, snap.completed_correct), (3, 2))
        self.assertEqual(snap.completed_accuracy_pct, 66.67)
        self.assertEqual(snap.accuracy_of_target_pct, 20.0)
        # mean(1.0, 0.0, 0.25) over the three qids that have qrels
        self.assertEqual(snap.judged_recall_pct, 41.67)
        self.assertEqual(snap.live_recall_pct, 41.67)
        self.assertEqual(snap.avg_tool_calls, {"search": 4.0, "get_document": 1.0})
        self.assertEqual(snap.evaluation_status_counts["legacy"], 1)
        self.assertEqual(snap.pending_judgement, 0)

    def test_live_recall_includes_unjudged_runs_and_pending_is_counted(self):
        self.add_run(1, retrieved=["a", "b"], age=600)
        self.add_run(2, retrieved=[], age=10)
        self.add_eval(1, True, 1.0)

        snap = self.collect()

        self.assertEqual(snap.pending_judgement, 1)
        self.assertEqual(snap.judged_recall_pct, 100.0)
        self.assertEqual(snap.live_recall_pct, 50.0)
        self.assertAlmostEqual(snap.oldest_pending_age_seconds, 10, delta=1)

    def test_warns_when_evaluator_or_benchmark_stalls(self):
        self.add_run(1, age=3 * 3600)
        self.add_run(2, age=2 * 3600)
        self.add_eval(1, True, 1.0, age=3 * 3600)

        notes = monitor.warnings(self.collect(), stall_minutes=15)

        self.assertTrue(any("evaluator window" in note for note in notes), notes)
        self.assertTrue(any("benchmark window" in note for note in notes), notes)

    def test_no_warnings_for_healthy_or_finished_run(self):
        self.add_run(1, age=120)
        self.add_run(2, age=20)
        self.add_eval(1, True, 1.0, age=100)
        self.assertEqual(monitor.warnings(self.collect(), stall_minutes=15), [])

        for path in self.runs.glob("*.json"):
            os.utime(path, (self.now - 86400, self.now - 86400))
        self.add_eval(2, False, 0.0, age=86400)
        finished = monitor.collect("demo", self.runs, self.evals, 2, QRELS, now=self.now)
        self.assertEqual(monitor.warnings(finished, stall_minutes=15), [])

    def test_partial_eval_file_is_skipped_not_fatal(self):
        self.add_run(1)
        self.evals.mkdir(parents=True, exist_ok=True)
        (self.evals / "run_qid_1_eval.json").write_text('{"query_id": "1", "fin', encoding="utf-8")

        snap = self.collect()

        self.assertEqual(snap.judged, 0)
        self.assertEqual(snap.unreadable_files, 1)
        self.assertIn("mid-write", " ".join(monitor.warnings(snap, stall_minutes=15)))

    def test_eta_uses_last_hour_throughput(self):
        for qid in range(1, 5):
            self.add_run(qid, age=600)
        self.add_run(5, age=2 * 3600)

        snap = self.collect(target=13)

        self.assertEqual(snap.runs_last_hour, 4)
        self.assertEqual(snap.eta_seconds, 8 / 4 * 3600)

    def test_missing_directories_render_without_crashing(self):
        snap = self.collect()
        text = monitor.render(snap, stall_minutes=15)
        self.assertIn("0/10", text)
        self.assertIn("run directory does not exist yet", text)

    def test_dotenv_and_target_helpers(self):
        env = self.root / ".env"
        env.write_text('# comment\nexport RUN_NAME="qwen-4b"\nQUERY_FILE=topics.tsv\nBROKEN\n', encoding="utf-8")
        self.assertEqual(monitor.read_dotenv(env), {"RUN_NAME": "qwen-4b", "QUERY_FILE": "topics.tsv"})

        queries = self.root / "q.tsv"
        queries.write_text("1\tq one\n2\tq two\n\n", encoding="utf-8")
        self.assertEqual(monitor.count_target(queries), 2)
        self.assertEqual(monitor.count_target(self.root / "missing.tsv"), monitor.DEFAULT_TARGET)

    def test_cli_once_json_snapshot(self):
        self.add_run(1, retrieved=["a"])
        self.add_eval(1, True, 0.5)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = monitor.main(
                ["--runs-dir", str(self.runs), "--evals-dir", str(self.evals), "--target", "5", "--once", "--json"]
            )
        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["run_name"], "demo")
        self.assertEqual(data["accuracy_pct"], 100.0)
        self.assertEqual(data["target"], 5)


if __name__ == "__main__":
    unittest.main()
