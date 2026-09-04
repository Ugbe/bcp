"""Audit the trace-derived augmentation without modifying any dataset."""

from __future__ import annotations

import collections
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUGMENTATION = ROOT / "data/training_identity_v2/trace_augmentation_500.jsonl"
V3_TRAIN = ROOT / "data/training_multitask_v3/final_multitask_train.jsonl"
V3_VALIDATION = ROOT / "data/training_multitask_v3/final_multitask_validation.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def parent_from_path(path: str) -> str | None:
    match = re.search(r"query_(\d+)_\d+\.md$", path)
    return match.group(1) if match else None


def archive_flags(path: Path) -> dict[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "placeholder_document": bool(re.search(r"\[(?:Full )?Document content for docid", text, re.I)),
        "additional_clue_filler": "Additional clue from the question" in text,
        "unsupported_all_verified": "All material clues are supported" in text,
        "predicted_equals_correct_field": bool(
            (pred := re.search(r"\| Predicted answer \|\s*(.*?)\s*\|", text, re.I))
            and (gold := re.search(r"\| Correct answer \|\s*(.*?)\s*\|", text, re.I))
            and normalized(pred.group(1)) == normalized(gold.group(1))
        ),
    }


TOOL_SECTION_RE = re.compile(
    r"### Step \d+ . Tool call: `([^`]+)`.*?"
    r"\*\*Arguments\*\*\s*```json\s*(.*?)\s*```.*?"
    r"\*\*Output\*\*\s*(.*?)(?=\n### Step \d+|\n## Final answer|\Z)",
    re.S,
)


def grounded_tool_match(path: Path, answer: str) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    answer_norm = normalized(answer)
    for _, _, output in TOOL_SECTION_RE.findall(text):
        if "[Document content for docid" in output or "[Full document content for docid" in output:
            continue
        if answer_norm and answer_norm in normalized(output):
            return True
    return False


def main() -> None:
    rows = load_jsonl(AUGMENTATION)
    train = load_jsonl(V3_TRAIN)
    validation = load_jsonl(V3_VALIDATION)
    parents = [parent_from_path(row["metadata"]["trace_file"]) for row in rows]
    parent_counts = collections.Counter(parents)
    prompts = [normalized(row["messages"][1]["content"]) for row in rows]
    answers = [normalized(row["messages"][-1]["content"].removeprefix("The answer is: ")) for row in rows]
    existing_ids = {
        str(row.get("metadata", {}).get("parent_query_id"))
        for row in train + validation
        if row.get("metadata", {}).get("parent_query_id") is not None
    }
    archive_summary = collections.Counter()
    grounded_rows = 0
    grounded_parents: set[str] = set()
    grounded_non_holdout_parents: set[str] = set()
    missing = 0
    for row in rows:
        trace_path = ROOT / row["metadata"]["trace_file"]
        if not trace_path.exists():
            missing += 1
            continue
        archive_summary.update(key for key, value in archive_flags(trace_path).items() if value)
        answer = row["messages"][-1]["content"].removeprefix("The answer is: ")
        if grounded_tool_match(trace_path, answer):
            grounded_rows += 1
            parent = parent_from_path(row["metadata"]["trace_file"])
            if parent:
                grounded_parents.add(parent)
                if not 769 <= int(parent) <= 797 and parent not in existing_ids:
                    grounded_non_holdout_parents.add(parent)

    report = {
        "rows": len(rows),
        "unique_parent_query_ids": len(parent_counts),
        "rows_beyond_one_per_parent": len(rows) - len(parent_counts),
        "maximum_variants_for_one_parent": max(parent_counts.values()),
        "parents_with_multiple_variants": sum(value > 1 for value in parent_counts.values()),
        "top_parent_counts": parent_counts.most_common(20),
        "exact_normalized_prompt_duplicates": len(prompts) - len(set(prompts)),
        "unique_normalized_answers": len(set(answers)),
        "answer_repetitions": len(answers) - len(set(answers)),
        "parent_overlap_with_existing_v3": sorted(set(parents) & existing_ids, key=int),
        "missing_archive_files": missing,
        "archive_quality_flags": dict(archive_summary),
        "rows_with_gold_literal_in_nonplaceholder_tool_output": grounded_rows,
        "unique_grounded_parents": len(grounded_parents),
        "unique_grounded_parents_after_769_to_797_holdout_and_existing_exclusions": len(grounded_non_holdout_parents),
        "rows_with_tools": sum(bool(row.get("tools")) for row in rows),
        "rows_with_tool_messages": sum(any(message.get("role") == "tool" for message in row["messages"]) for row in rows),
        "assistant_answer_prefixes": collections.Counter(
            row["messages"][-1]["content"].split(":", 1)[0] for row in rows
        ).most_common(),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
