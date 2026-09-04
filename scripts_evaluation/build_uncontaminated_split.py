"""Build reproducible BrowseComp evaluation splits without training overlap.

The multitask dataset contains derived views of BrowseComp research questions.
This script extracts their parent query IDs directly from the final JSONL files
and removes them from the benchmark TSV. Validation parents are also removed
from the strict split because they were used during model development.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def _research_parent_ids(path: Path) -> set[str]:
    parents: set[str] = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc

            metadata = record.get("metadata") or {}
            category = str(metadata.get("category") or "")
            query_id = str(record.get("query_id") or "")
            if not (category.startswith("research_") or query_id.startswith("research:")):
                continue

            parent = metadata.get("parent_query_id")
            if parent is None and query_id.startswith("research:"):
                parts = query_id.split(":")
                parent = parts[1] if len(parts) > 1 else None
            if parent is None or not str(parent).strip():
                raise ValueError(f"{path}:{line_number}: research row lacks parent ID")
            parents.add(str(parent).strip())
    return parents


def _read_queries(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8", newline="") as stream:
        for line_number, row in enumerate(csv.reader(stream, delimiter="\t"), start=1):
            if len(row) != 2:
                raise ValueError(f"{path}:{line_number}: expected exactly two columns")
            query_id, question = row[0].strip(), row[1].strip()
            if not query_id or not question:
                raise ValueError(f"{path}:{line_number}: empty query ID or question")
            if query_id in seen:
                raise ValueError(f"{path}:{line_number}: duplicate query ID {query_id!r}")
            seen.add(query_id)
            rows.append((query_id, question))
    return rows


def _write_tsv(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerows(rows)


def _write_ids(path: Path, ids: set[str]) -> None:
    ordered = sorted(ids, key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value))
    path.write_text("\n".join(ordered) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=Path, default=Path("topics-qrels/queries.tsv"))
    parser.add_argument(
        "--train",
        type=Path,
        default=Path("data/training_multitask_v3/final_multitask_train.jsonl"),
    )
    parser.add_argument(
        "--validation",
        type=Path,
        default=Path("data/training_multitask_v3/final_multitask_validation.jsonl"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("topics-qrels/splits/atom-electron-1.3-9b"),
    )
    args = parser.parse_args()

    queries = _read_queries(args.queries)
    benchmark_ids = {query_id for query_id, _ in queries}
    train_parents = _research_parent_ids(args.train)
    validation_parents = _research_parent_ids(args.validation)

    missing_train = train_parents - benchmark_ids
    missing_validation = validation_parents - benchmark_ids
    overlap = train_parents & validation_parents
    if missing_train or missing_validation or overlap:
        raise ValueError(
            "invalid split inputs: "
            f"missing_train={sorted(missing_train)}, "
            f"missing_validation={sorted(missing_validation)}, "
            f"train_validation_overlap={sorted(overlap)}"
        )

    strict_exclusions = train_parents | validation_parents
    strict_clean = [row for row in queries if row[0] not in strict_exclusions]
    reserved_holdout = [
        row
        for row in queries
        if row[0].isdigit()
        and 769 <= int(row[0]) <= 797
        and row[0] not in strict_exclusions
    ]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    strict_path = args.output_dir / "queries_strict_clean.tsv"
    holdout_path = args.output_dir / "queries_reserved_holdout_769_797.tsv"
    preflight_path = args.output_dir / "queries_preflight_one.tsv"
    train_ids_path = args.output_dir / "excluded_training_parent_ids.txt"
    validation_ids_path = args.output_dir / "excluded_validation_parent_ids.txt"
    manifest_path = args.output_dir / "split_manifest.json"

    _write_tsv(strict_path, strict_clean)
    _write_tsv(holdout_path, reserved_holdout)
    _write_tsv(preflight_path, reserved_holdout[:1])
    _write_ids(train_ids_path, train_parents)
    _write_ids(validation_ids_path, validation_parents)

    manifest = {
        "source_queries": str(args.queries),
        "source_train": str(args.train),
        "source_validation": str(args.validation),
        "benchmark_questions": len(queries),
        "training_parent_questions_excluded": len(train_parents),
        "validation_parent_questions_excluded": len(validation_parents),
        "strict_clean_questions": len(strict_clean),
        "reserved_holdout_769_797_questions": len(reserved_holdout),
        "train_validation_parent_overlap": len(overlap),
        "splits": {
            strict_path.name: {"rows": len(strict_clean), "sha256": _sha256(strict_path)},
            holdout_path.name: {"rows": len(reserved_holdout), "sha256": _sha256(holdout_path)},
            preflight_path.name: {"rows": min(1, len(reserved_holdout)), "sha256": _sha256(preflight_path)},
        },
        "policy": (
            "Strict-clean excludes every BrowseComp parent present in either the "
            "final training or validation JSONL. The reserved holdout is the subset "
            "of strict-clean query IDs numerically between 769 and 797 inclusive."
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
