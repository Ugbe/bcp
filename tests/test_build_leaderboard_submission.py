import unittest

from scripts_evaluation.build_leaderboard_submission import build_submission, record_confidence


def record(qid, correct, recall, tools, confidence=None, parse_error=False):
    return {
        "query_id": qid,
        "final_correct": correct,
        "retrieval": {"recall": recall},
        "tool_call_counts": tools,
        "judge_result": {"correct": correct, "confidence": confidence, "parse_error": parse_error},
    }


class BuildSubmissionTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("1", True, 1.0, {"search": 4, "get_document": 1}, confidence=90),
            record("2", False, 0.5, {"search": 6}, confidence=80),
            record("3", True, 0.0, {"search": 2}, confidence=None),
        ]
        self.kwargs = dict(
            llm="agent",
            retriever="retriever",
            link="https://example.invalid",
            evaluation_date="2026-09-16",
            search_calls_field=True,
        )

    def test_accuracy_recall_and_search_calls(self):
        submission = build_submission(self.records, **self.kwargs)
        self.assertEqual(submission["Accuracy (%)"], 66.67)
        self.assertEqual(submission["Recall (%)"], 50.0)
        self.assertAlmostEqual(submission["Search Calls"], 4.0)
        self.assertNotIn("avg_tool_stats", submission)

    def test_avg_tool_stats_variant_averages_over_all_runs(self):
        submission = build_submission(self.records, **{**self.kwargs, "search_calls_field": False})
        self.assertAlmostEqual(submission["avg_tool_stats"]["get_document"], 1 / 3)
        self.assertNotIn("Search Calls", submission)

    def test_per_query_metrics_shape(self):
        rows = build_submission(self.records, **self.kwargs)["per_query_metrics"]
        self.assertEqual(rows[0], {"query_id": "1", "correct": True, "recall": 100.0})
        self.assertEqual(len(rows), 3)

    def test_calibration_zero_below_one_hundred_confidences(self):
        self.assertEqual(build_submission(self.records, **self.kwargs)["Calibration Error (%)"], 0.0)

    def test_field_order_matches_leaderboard_schema(self):
        submission = build_submission(self.records, **self.kwargs)
        self.assertEqual(
            list(submission),
            [
                "LLM",
                "Retriever",
                "Accuracy (%)",
                "Recall (%)",
                "Search Calls",
                "Calibration Error (%)",
                "Link",
                "Evaluation Date",
                "per_query_metrics",
            ],
        )

    def test_parse_error_records_contribute_no_confidence(self):
        self.assertIsNone(record_confidence(record("4", True, 1.0, {}, 95, parse_error=True)))
        self.assertEqual(record_confidence(record("5", True, 1.0, {}, 95)), 95.0)

    def test_model_confidence_is_used_when_judge_confidence_missing(self):
        item = record("6", True, 1.0, {})
        item["model_confidence"] = 70
        self.assertEqual(record_confidence(item), 70.0)


if __name__ == "__main__":
    unittest.main()


class SummaryPreferenceTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            record("1", True, 1.0, {"search": 4}, confidence=90),
            record("2", False, 0.5, {"search": 6}, confidence=80),
        ]
        self.kwargs = dict(
            llm="agent",
            retriever="retriever",
            link="https://example.invalid",
            evaluation_date="2026-09-16",
            search_calls_field=True,
        )

    def test_summary_values_win_and_mismatches_are_reported(self):
        mismatches = []
        submission = build_submission(
            self.records,
            **self.kwargs,
            summary={"Accuracy (%)": 50.0, "Calibration Error (%)": 25.76},
            mismatches_out=mismatches,
        )
        self.assertEqual(submission["Accuracy (%)"], 50.0)
        self.assertEqual(submission["Calibration Error (%)"], 25.76)
        self.assertEqual(submission["Recall (%)"], 75.0)
        self.assertEqual(len(mismatches), 1)
        self.assertIn("Calibration Error (%)", mismatches[0])

    def test_summary_tool_stats_are_used_for_search_calls(self):
        submission = build_submission(
            self.records, **self.kwargs, summary={"avg_tool_stats": {"search": 7.5}}
        )
        self.assertEqual(submission["Search Calls"], 7.5)

    def test_missing_summary_falls_back_to_recomputation(self):
        submission = build_submission(self.records, **self.kwargs, summary={})
        self.assertEqual(submission["Accuracy (%)"], 50.0)
        self.assertEqual(submission["Search Calls"], 5.0)
