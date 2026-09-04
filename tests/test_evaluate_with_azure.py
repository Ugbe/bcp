import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts_evaluation.evaluate_with_azure import (
    build_summary,
    deterministic_answer_equivalence,
    evaluate_run_file,
    extract_model_answer,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "judge_equivalence_cases.csv"


class _NoNetworkClient:
    """Raises if a deterministic evaluation unexpectedly reaches Azure."""

    @property
    def chat(self):
        raise AssertionError("Azure must not be called for a deterministic decision")


def _judge_text(correct: bool) -> str:
    return (
        "extracted_final_answer: candidate\n"
        "reasoning: fixture decision\n"
        f"correct: {'yes' if correct else 'no'}\n"
        "confidence: 99"
    )


class AnswerEquivalenceTests(unittest.TestCase):
    def test_all_42_reviewed_cases(self):
        with FIXTURE_PATH.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 42)
        observed = {True: 0, False: 0, None: 0}
        for row in rows:
            with self.subTest(query_id=row["query_id"]):
                decision = deterministic_answer_equivalence(
                    row["correct_answer"], row["model_answer"], row["query_id"]
                )
                expected = {
                    "true": True,
                    "false": False,
                    "unresolved": None,
                }[row["expected_decision"]]
                self.assertIs(decision.correct, expected)
                observed[decision.correct] += 1
                if expected is None:
                    self.assertTrue(decision.needs_adjudication)

        self.assertEqual(observed, {True: 21, False: 9, None: 12})

    def test_unreviewed_numeric_differences_are_rejected(self):
        decision = deterministic_answer_equivalence(
            "November 5, 2024", "November 6, 2024"
        )
        self.assertIs(decision.correct, False)
        self.assertEqual(decision.rule, "different_numeric_values")

    def test_surname_only_is_not_accepted_generically(self):
        decision = deterministic_answer_equivalence("Lewis Dunk", "Dunk")
        self.assertIsNone(decision.correct)

    def test_meaningful_title_preposition_is_not_accepted_generically(self):
        decision = deterministic_answer_equivalence(
            "Sunset at Biafra", "Sunset in Biafra"
        )
        self.assertIsNone(decision.correct)

    def test_exact_answer_extraction_supports_plain_and_markdown_labels(self):
        plain = "Explanation: x\nExact Answer: Jadal\nConfidence: 90%"
        markdown = "Explanation: x\n**Exact Answer:** **Jadal**\nConfidence: 90%"
        self.assertEqual(extract_model_answer(plain), ("Jadal", "parsed"))
        self.assertEqual(extract_model_answer(markdown), ("Jadal", "parsed"))


class EvaluateRunFileTests(unittest.TestCase):
    def _write_run(self, directory: str, query_id, status, response=None) -> Path:
        result = []
        if response is not None:
            result.append({"type": "output_text", "output": response})
        path = Path(directory) / f"run_qid_{query_id}.json"
        path.write_text(
            json.dumps(
                {
                    "query_id": query_id,
                    "status": status,
                    "result": result,
                    "retrieved_docids": ["10"],
                    "tool_call_counts": {"search": 1},
                }
            ),
            encoding="utf-8",
        )
        return path

    def _evaluate(
        self,
        path: Path,
        answer: str,
        question: str = "Which entity?",
        adjudicate: bool = False,
    ) -> dict:
        return evaluate_run_file(
            path,
            {str(json.loads(path.read_text())["query_id"]): {"answer": answer, "question": question}},
            {str(json.loads(path.read_text())["query_id"]): ["10"]},
            _NoNetworkClient(),
            "fixture-judge",
            256,
            0.0,
            adjudicate,
        )

    def test_deterministic_match_skips_judge_and_preserves_compatibility(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(
                tmpdir,
                791,
                "completed",
                "Explanation: evidence\nExact Answer: JadaL\nConfidence: 90%",
            )
            result = self._evaluate(path, "Jadal")

        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(result["model_answer_parse_status"], "parsed")
        self.assertEqual(result["judge_call_status"], "not_called_deterministic")
        self.assertEqual(result["judge_parse_status"], "not_attempted")
        self.assertIsNone(result["raw_judge_correct"])
        self.assertIs(result["normalized_correct"], True)
        self.assertIs(result["final_correct"], True)
        self.assertIs(result["judge_result"]["correct"], True)
        self.assertEqual(result["model_confidence"], 90.0)
        self.assertEqual(result["judge_result"]["confidence"], 90.0)

    def test_incomplete_run_is_not_a_judge_parse_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(tmpdir, 1, "incomplete_length")
            result = self._evaluate(path, "answer")

        self.assertEqual(result["evaluation_status"], "run_incomplete")
        self.assertEqual(result["run_status"], "incomplete_length")
        self.assertEqual(
            result["model_answer_parse_status"], "not_attempted_run_incomplete"
        )
        self.assertEqual(result["judge_call_status"], "not_called_run_incomplete")
        self.assertEqual(result["judge_parse_status"], "not_attempted")
        self.assertFalse(result["judge_result"]["parse_error"])
        self.assertFalse(result["judge_result"]["correct"])

    def test_completed_empty_response_is_model_parse_error_not_run_or_judge_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(tmpdir, 5, "completed")
            result = self._evaluate(path, "answer")

        self.assertEqual(result["run_status"], "completed")
        self.assertEqual(result["evaluation_status"], "model_answer_parse_error")
        self.assertEqual(result["model_answer_parse_status"], "missing_response")
        self.assertEqual(
            result["judge_call_status"], "not_called_model_answer_parse_error"
        )
        self.assertEqual(result["judge_parse_status"], "not_attempted")

    def test_unresolved_answer_records_raw_and_final_judgment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(
                tmpdir,
                2,
                "completed",
                "Explanation: evidence\nExact Answer: Beta\nConfidence: 70%",
            )
            with patch(
                "scripts_evaluation.evaluate_with_azure.call_azure_judge",
                return_value=_judge_text(False),
            ) as judge:
                result = self._evaluate(path, "Alpha")

        judge.assert_called_once()
        self.assertEqual(result["judge_call_status"], "succeeded")
        self.assertEqual(result["judge_parse_status"], "parsed")
        self.assertIs(result["raw_judge_correct"], False)
        self.assertIsNone(result["normalized_correct"])
        self.assertIs(result["final_correct"], False)
        self.assertIs(result["judge_result"]["correct"], False)

    def test_judge_parse_failure_is_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(
                tmpdir,
                3,
                "completed",
                "Explanation: evidence\nExact Answer: Beta\nConfidence: 70%",
            )
            with patch(
                "scripts_evaluation.evaluate_with_azure.call_azure_judge",
                return_value="unparseable judge output",
            ):
                result = self._evaluate(path, "Alpha")

        self.assertEqual(result["evaluation_status"], "judge_parse_error")
        self.assertEqual(result["judge_call_status"], "succeeded")
        self.assertEqual(result["judge_parse_status"], "parse_error")
        self.assertIsNone(result["final_correct"])
        self.assertTrue(result["judge_result"]["parse_error"])

    def test_judge_call_failure_is_separate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(
                tmpdir,
                4,
                "completed",
                "Explanation: evidence\nExact Answer: Beta\nConfidence: 70%",
            )
            with patch(
                "scripts_evaluation.evaluate_with_azure.call_azure_judge",
                side_effect=RuntimeError("offline"),
            ):
                result = self._evaluate(path, "Alpha")

        self.assertEqual(result["evaluation_status"], "judge_call_error")
        self.assertEqual(result["judge_call_status"], "error")
        self.assertEqual(result["judge_parse_status"], "not_attempted")
        self.assertIsNone(result["final_correct"])
        self.assertFalse(result["judge_result"]["parse_error"])

    def test_policy_dependent_case_only_changes_after_opt_in_adjudication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self._write_run(
                tmpdir,
                275,
                "completed",
                "Explanation: evidence\nExact Answer: Union Carbide Corporation\nConfidence: 70%",
            )
            with patch(
                "scripts_evaluation.evaluate_with_azure.call_azure_judge",
                side_effect=[_judge_text(False), _judge_text(True)],
            ) as judge:
                result = self._evaluate(
                    path,
                    "Union Carbide and Carbon Corporation.",
                    adjudicate=True,
                )

        self.assertEqual(judge.call_count, 2)
        self.assertIsNone(result["normalized_correct"])
        self.assertTrue(result["normalization_rule"].startswith("policy_dependent"))
        self.assertIs(result["raw_judge_correct"], False)
        self.assertIs(result["adjudication"]["correct"], True)
        self.assertIs(result["final_correct"], True)
        self.assertIs(result["judge_result"]["correct"], True)

    def test_summary_prefers_final_correct_and_supports_legacy_records(self):
        records = [
            {
                "query_id": 1,
                "final_correct": True,
                "evaluation_status": "normalized_completed",
                "judge_result": {"correct": False, "parse_error": False},
                "retrieval": {"recall": 1.0},
            },
            {
                "query_id": 2,
                "judge_result": {"correct": True, "parse_error": False},
                "retrieval": {"recall": 0.0},
            },
        ]
        summary = build_summary(records, {"1": ["x"], "2": ["y"]}, "fixture")
        self.assertEqual(summary["Accuracy (%)"], 100.0)
        self.assertEqual(
            summary["evaluation_status_counts"],
            {"normalized_completed": 1, "legacy": 1},
        )


if __name__ == "__main__":
    unittest.main()
