"""Derive an identity-renamed variant of the reviewed multitask v3 corpus.

The multitask v3 files were reviewed and frozen for one public model name.
A different base checkpoint size needs the same corpus with only that public
name changed, so this script performs a byte-level substring substitution over
the v3 directory and proves that nothing else changed: every derived JSONL row
must reproduce its source row exactly once the substitution is reversed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


DEFAULT_SOURCE = Path("data/training_multitask_v3")
DEFAULT_OUTPUT = Path("data/training_multitask_v3_4b")
DEFAULT_OLD_NAME = "Atom Electron 1-9B"
DEFAULT_NEW_NAME = "Atom Electron 1-4B"
DEFAULT_TARGET_MODEL = "Qwen/Qwen3.5-4B"
REBUILT_FILES = {"dataset_report.json"}
SUBSTITUTED_SUFFIXES = {".jsonl", ".txt"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace_strings(value, old: str, new: str):
    """Recursively substitute inside every string value of a JSON object."""
    if isinstance(value, str):
        return value.replace(old, new)
    if isinstance(value, list):
        return [replace_strings(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_strings(item, old, new) for key, item in value.items()}
    return value


def read_rows(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def derive_file(source: Path, target: Path, old: str, new: str) -> int:
    text = source.read_bytes().decode("utf-8")
    if new in text:
        raise ValueError(f"{source}: new name {new!r} already present in source")
    count = text.count(old)
    target.write_bytes(text.replace(old, new).encode("utf-8"))
    return count


def verify_round_trip(source: Path, target: Path, old: str, new: str) -> int:
    derived_text = target.read_bytes().decode("utf-8")
    if old in derived_text:
        raise ValueError(f"{target}: old name {old!r} survived substitution")
    source_rows = read_rows(source)
    derived_rows = read_rows(target)
    if len(source_rows) != len(derived_rows):
        raise ValueError(f"{target}: row count changed {len(source_rows)} -> {len(derived_rows)}")
    for number, (source_row, derived_row) in enumerate(zip(source_rows, derived_rows), start=1):
        if replace_strings(derived_row, new, old) != source_row:
            raise ValueError(f"{target}:{number}: derived row differs beyond the identity name")
    return len(derived_rows)


def derive(
    source_dir: Path,
    output_dir: Path,
    old: str,
    new: str,
    target_model: str,
) -> dict:
    if not old or not new or old == new:
        raise ValueError("old and new identity names must be distinct and non-empty")
    source_report_path = source_dir / "dataset_report.json"
    if not source_report_path.exists():
        raise FileNotFoundError(f"missing source report: {source_report_path}")
    output_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict] = {}
    for source in sorted(path for path in source_dir.iterdir() if path.is_file()):
        if source.name in REBUILT_FILES:
            continue
        target = output_dir / source.name
        if source.suffix in SUBSTITUTED_SUFFIXES:
            replacements = derive_file(source, target, old, new)
            rows = verify_round_trip(source, target, old, new) if source.suffix == ".jsonl" else None
            files[source.name] = {"replacements": replacements, "rows": rows, "sha256": sha256(target)}
        else:
            if old in source.read_bytes().decode("utf-8"):
                raise ValueError(f"{source}: contains the identity name but is not a substituted file type")
            shutil.copyfile(source, target)
            files[source.name] = {"replacements": 0, "rows": None, "sha256": sha256(target)}

    report = json.loads(source_report_path.read_text(encoding="utf-8"))
    report["identity_name"] = new
    report["target_model"] = target_model
    report["final_train_sha256"] = files["final_multitask_train.jsonl"]["sha256"]
    report["derivation"] = {
        "source_dir": source_dir.name,
        "source_train_sha256": sha256(source_dir / "final_multitask_train.jsonl"),
        "old_name": old,
        "new_name": new,
        "files": files,
    }
    (output_dir / "dataset_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--old-name", default=DEFAULT_OLD_NAME)
    parser.add_argument("--new-name", default=DEFAULT_NEW_NAME)
    parser.add_argument(
        "--target-model",
        default=DEFAULT_TARGET_MODEL,
        help="Base checkpoint the variant is intended for; recorded in the report only.",
    )
    args = parser.parse_args()
    report = derive(args.source_dir, args.output_dir, args.old_name, args.new_name, args.target_model)
    print(json.dumps(report["derivation"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
