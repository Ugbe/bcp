import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts_training.derive_identity_variant import derive, read_rows, replace_strings
from scripts_training.run_training_v2 import check_identity, read_jsonl
from scripts_training.validate_multitask_dataset_v3 import validate


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "data" / "training_multitask_v3"
DERIVED = ROOT / "data" / "training_multitask_v3_4b"
OLD_NAME = "Atom Electron 1-9B"
NEW_NAME = "Atom Electron 1-4B"
TARGET_MODEL = "Qwen/Qwen3.5-4B"
CORPUS_FILES = (
    "final_multitask_train.jsonl",
    "final_multitask_validation.jsonl",
    "identity_control_all.jsonl",
    "identity_control_train.jsonl",
    "identity_control_validation.jsonl",
    "recovered_trace_train.jsonl",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class IdentityVariantTests(unittest.TestCase):
    def test_committed_variant_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "variant"
            derive(SOURCE, output, OLD_NAME, NEW_NAME, TARGET_MODEL)
            expected = {path.name: sha256(path) for path in DERIVED.iterdir() if path.is_file()}
            actual = {path.name: sha256(path) for path in output.iterdir() if path.is_file()}
        self.assertEqual(actual, expected)
        self.assertEqual(set(expected), {path.name for path in SOURCE.iterdir() if path.is_file()})

    def test_only_identity_name_changes(self):
        for name in CORPUS_FILES:
            source_text = (SOURCE / name).read_text(encoding="utf-8")
            derived_text = (DERIVED / name).read_text(encoding="utf-8")
            self.assertNotIn(OLD_NAME, derived_text, name)
            self.assertNotIn(NEW_NAME, source_text, name)
            self.assertEqual(derived_text.count(NEW_NAME), source_text.count(OLD_NAME), name)
            source_rows = read_rows(SOURCE / name)
            derived_rows = read_rows(DERIVED / name)
            self.assertEqual(len(source_rows), len(derived_rows), name)
            for source_row, derived_row in zip(source_rows, derived_rows):
                self.assertEqual(replace_strings(derived_row, NEW_NAME, OLD_NAME), source_row)
        prompt = (DERIVED / "production_system_prompt.txt").read_text(encoding="utf-8")
        self.assertTrue(prompt.startswith(f"You are {NEW_NAME}, "))

    def test_derived_corpus_passes_validator(self):
        args = SimpleNamespace(
            train=DERIVED / "final_multitask_train.jsonl",
            validation=DERIVED / "final_multitask_validation.jsonl",
            controls=DERIVED / "identity_control_all.jsonl",
            prompt=DERIVED / "production_system_prompt.txt",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            report = validate(args)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["train_rows"], 710)
        self.assertEqual(report["validation_rows"], 32)

    def test_trainer_identity_guard(self):
        train = read_jsonl(DERIVED / "final_multitask_train.jsonl")
        validation = read_jsonl(DERIVED / "final_multitask_validation.jsonl")
        check_identity(train, validation, NEW_NAME)
        with self.assertRaises(ValueError):
            check_identity(train, validation, OLD_NAME)

    def test_report_records_derivation(self):
        report = json.loads((DERIVED / "dataset_report.json").read_text(encoding="utf-8"))
        source_report = json.loads((SOURCE / "dataset_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["identity_name"], NEW_NAME)
        self.assertEqual(report["target_model"], TARGET_MODEL)
        self.assertEqual(report["final_train_sha256"], sha256(DERIVED / "final_multitask_train.jsonl"))
        self.assertEqual(report["derivation"]["source_train_sha256"], source_report["final_train_sha256"])
        self.assertEqual(report["derivation"]["old_name"], OLD_NAME)
        self.assertEqual(report["derivation"]["new_name"], NEW_NAME)
        self.assertEqual(report["counts_by_category"], source_report["counts_by_category"])
        self.assertEqual(report["derivation"]["files"]["final_multitask_train.jsonl"]["rows"], 710)

    def test_rejects_source_that_already_contains_new_name(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                derive(DERIVED, Path(temp) / "again", OLD_NAME, NEW_NAME, TARGET_MODEL)


if __name__ == "__main__":
    unittest.main()
