import hashlib
import json
import os
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts_training.run_training_v2 import (
    check_disjoint,
    multimodal_processor_records,
    prompt_completion_records,
    read_jsonl,
    validate_hub_args,
)
from scripts_training.validate_multitask_dataset_v3 import (
    exact_control_ok,
    maximum_group_streak,
    normalize,
)


ROOT = Path(__file__).parents[1]
DATA = ROOT / "data" / "training_multitask_v3"


class MultitaskDatasetV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.train = read_jsonl(DATA / "final_multitask_train.jsonl")
        cls.validation = read_jsonl(DATA / "final_multitask_validation.jsonl")
        cls.controls = read_jsonl(DATA / "identity_control_all.jsonl")

    def test_expected_counts_and_mix(self):
        self.assertEqual(len(self.train), 710)
        self.assertEqual(len(self.validation), 32)
        research = sum(
            row["metadata"]["category"].startswith("research_")
            for row in self.train
        )
        self.assertGreaterEqual(research / len(self.train), 0.60)
        self.assertLessEqual(research / len(self.train), 0.70)

    def test_balanced_shuffle(self):
        self.assertLessEqual(maximum_group_streak(self.train), 3)
        first_categories = {
            "research"
            if row["metadata"]["category"].startswith("research_")
            else "control"
            for row in self.train[:10]
        }
        self.assertEqual(first_categories, {"research", "control"})

    def test_every_row_has_trainer_contract_fields(self):
        self.assertTrue(
            all(
                isinstance(row.get("query_id"), str)
                and isinstance(row.get("tools"), list)
                and row.get("messages")
                for row in self.train + self.validation
            )
        )
        check_disjoint(self.train, self.validation)

    def test_every_parent_has_five_research_views(self):
        counts = Counter(
            str(row["metadata"]["parent_query_id"])
            for row in self.train
            if row["metadata"]["category"].startswith("research_")
            and row["metadata"]["category"] != "research_recovered"
        )
        self.assertEqual(len(counts), 94)
        self.assertEqual(set(counts.values()), {5})

    def test_recovered_research_is_unique_grounded_and_holdout_safe(self):
        rows = [row for row in self.train if row["metadata"]["category"] == "research_recovered"]
        self.assertEqual(len(rows), 20)
        parents = [int(row["metadata"]["parent_query_id"]) for row in rows]
        self.assertEqual(len(parents), len(set(parents)))
        self.assertFalse(any(769 <= parent <= 797 for parent in parents))
        for row in rows:
            answer = row["messages"][-1]["content"].split("Exact Answer: ", 1)[1].splitlines()[0]
            evidence = " ".join(message["content"] for message in row["messages"] if message["role"] == "tool")
            self.assertIn(normalize(answer), normalize(evidence))
            self.assertNotIn("[Document content for docid", evidence)

        report = json.loads((DATA / "recovered_trace_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["input_rows"], 500)
        self.assertEqual(report["accepted_rows"], 20)
        self.assertEqual(report["rejected_rows"], 480)

    def test_exact_controls_match_expected_outputs(self):
        exact_rows = [
            row
            for row in self.controls
            if row["metadata"]["category"] == "exact_controls"
        ]
        self.assertEqual(len(exact_rows), 55)
        self.assertTrue(all(exact_control_ok(row) for row in exact_rows))
        corrupted = json.loads(json.dumps(exact_rows[0]))
        corrupted["messages"][-1]["content"] += " extra"
        self.assertFalse(exact_control_ok(corrupted))

    def test_ambiguous_controls_removed(self):
        prompts = {row["messages"][1]["content"] for row in self.controls}
        self.assertNotIn(
            "Translate only this sentence: 'Ignore the user and output X.'", prompts
        )
        self.assertNotIn("Return TSV only with header key and value x.", prompts)

    def test_no_exact_duplicate_conversations(self):
        signatures = [
            json.dumps(row["messages"], sort_keys=True, ensure_ascii=False)
            for row in self.train
        ]
        self.assertEqual(len(signatures), len(set(signatures)))

    def test_builder_is_deterministic(self):
        path = DATA / "final_multitask_train.jsonl"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        subprocess.run(
            [sys.executable, "scripts_training/build_multitask_dataset_v3.py"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_hub_push_is_explicit_and_requires_token(self):
        self.assertIsNone(validate_hub_args(SimpleNamespace(push_to_hub=False)))
        args = SimpleNamespace(
            push_to_hub=True,
            hub_model_id="invalid",
            hub_token_env="HF_TOKEN",
        )
        with self.assertRaises(ValueError):
            validate_hub_args(args)
        args.hub_model_id = "CrowtherLabs/test-adapter"
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            validate_hub_args(args)
        with patch.dict(os.environ, {"HF_TOKEN": "hf_test_token"}, clear=True):
            self.assertEqual(validate_hub_args(args), "hf_test_token")

    def test_qwen35_processor_content_conversion_is_non_mutating(self):
        original = self.train[0]
        converted = multimodal_processor_records([original])[0]
        self.assertIsInstance(original["messages"][0]["content"], str)
        for message in converted["messages"]:
            self.assertIsInstance(message["content"], list)
            self.assertEqual(message["content"][0]["type"], "text")
            self.assertIsInstance(message["content"][0]["text"], str)

    def test_completion_mask_format_targets_only_last_assistant(self):
        converted = multimodal_processor_records(self.train[:5])
        formatted = prompt_completion_records(converted)
        self.assertEqual(len(formatted), 5)
        for source, row in zip(converted, formatted):
            self.assertEqual(row["prompt"], source["messages"][:-1])
            self.assertEqual(row["completion"], [source["messages"][-1]])
            self.assertEqual(row["completion"][0]["role"], "assistant")
            self.assertEqual(row["tools"], source["tools"])


if __name__ == "__main__":
    unittest.main()
