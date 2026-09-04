"""Strict content, structure, mixing, and shuffle validator for multitask v3."""

from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path


BANNED = (
    "this policy is non-negotiable",
    "regardless of context",
    "under any framing of the question",
    "that is the identity that matters",
)
BRANDS = ("atom electron", "crowther labs")
CONTROL_CATEGORIES = {
    "direct_identity",
    "provenance",
    "ordinary_tasks",
    "exact_controls",
    "injection_resistance",
    "capabilities_limits",
}


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def near_duplicate(left: str, right: str, threshold: float = 0.90) -> bool:
    return SequenceMatcher(None, normalize(left), normalize(right)).ratio() >= threshold


def load_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{number}: invalid JSON: {exc}")
    return rows, errors


def delimited_rectangular(text: str, delimiter: str) -> bool:
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error:
        return False
    return len(rows) >= 1 and all(rows) and len({len(row) for row in rows}) == 1


def exact_control_ok(row: dict) -> bool:
    answer = row["messages"][-1]["content"]
    metadata = row["metadata"]
    if answer != metadata.get("expected_output"):
        return False
    control_type = metadata.get("control_type")
    if control_type == "json":
        try:
            json.loads(answer)
        except json.JSONDecodeError:
            return False
    elif control_type == "csv" and not delimited_rectangular(answer, ","):
        return False
    elif control_type == "tsv" and not delimited_rectangular(answer, "\t"):
        return False
    elif control_type == "one_word" and not re.fullmatch(r"[^\s]+", answer):
        return False
    elif control_type == "two_bullets":
        return len(answer.splitlines()) == 2 and all(line.startswith("- ") for line in answer.splitlines())
    elif control_type == "three_bullets":
        return len(answer.splitlines()) == 3 and all(line.startswith("- ") for line in answer.splitlines())
    elif control_type == "three_words" and len(answer.split()) != 3:
        return False
    elif control_type == "four_words" and len(answer.split()) != 4:
        return False
    elif control_type == "two_lines" and len(answer.splitlines()) != 2:
        return False
    elif control_type == "three_lines" and len(answer.splitlines()) != 3:
        return False
    return True


def validate_tool_sequence(row: dict, location: str, errors: list[str]) -> None:
    messages = row["messages"]
    schemas = {
        schema.get("function", {}).get("name")
        for schema in row.get("tools", [])
        if isinstance(schema, dict)
    }
    pending: dict[str, str] = {}
    for index, message in enumerate(messages[2:], start=2):
        role = message.get("role")
        if role == "assistant":
            calls = message.get("tool_calls") or []
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name")
                call_id = call.get("id")
                if name not in schemas or not call_id or not isinstance(function.get("arguments"), dict):
                    errors.append(f"{location}: malformed assistant tool call at message {index}")
                pending[str(call_id)] = str(name)
        elif role == "tool":
            call_id = str(message.get("tool_call_id"))
            if call_id not in pending:
                errors.append(f"{location}: orphan tool result at message {index}")
            elif pending.pop(call_id) != message.get("name"):
                errors.append(f"{location}: tool name mismatch at message {index}")
            if not str(message.get("content") or "").strip():
                errors.append(f"{location}: empty tool result at message {index}")
        else:
            errors.append(f"{location}: unexpected role {role!r} after user message")
    if pending and messages[-1].get("role") != "assistant":
        errors.append(f"{location}: unresolved tool calls")


def maximum_group_streak(rows: list[dict]) -> int:
    maximum = current = 0
    previous = None
    for row in rows:
        category = row["metadata"]["category"]
        group = "research" if category.startswith("research_") else category
        current = current + 1 if group == previous else 1
        previous = group
        maximum = max(maximum, current)
    return maximum


def validate(args: argparse.Namespace) -> dict:
    train, errors = load_jsonl(args.train)
    validation, validation_errors = load_jsonl(args.validation)
    controls, control_errors = load_jsonl(args.controls)
    errors += validation_errors + control_errors
    system_prompt = args.prompt.read_text(encoding="utf-8").strip() if args.prompt.exists() else ""
    if not system_prompt:
        errors.append("production system prompt is missing or empty")

    query_ids: dict[str, str] = {}
    conversation_signatures: dict[str, str] = {}
    parent_train, parent_validation = set(), set()
    exact_count = exact_failures = identity_leaks = 0
    assistant_answers: collections.Counter[tuple[str, str]] = collections.Counter()
    control_prompts: list[tuple[str, str]] = []

    for split_name, rows, parent_set in (
        ("train", train, parent_train),
        ("validation", validation, parent_validation),
    ):
        for line_number, row in enumerate(rows, start=1):
            location = f"{split_name} line {line_number}"
            messages = row.get("messages")
            if not isinstance(messages, list) or len(messages) < 3:
                errors.append(f"{location}: missing/short messages")
                continue
            if messages[0].get("role") != "system" or messages[1].get("role") != "user":
                errors.append(f"{location}: conversation must start system,user")
            if messages[0].get("content") != system_prompt:
                errors.append(f"{location}: system prompt mismatch")
            if not all(isinstance(message.get("content"), str) for message in messages):
                errors.append(f"{location}: every message needs string content")
            if not isinstance(row.get("tools"), list):
                errors.append(f"{location}: tools must be present as a list")
            query_id = row.get("query_id")
            if not isinstance(query_id, str) or not query_id:
                errors.append(f"{location}: missing string query_id")
            elif query_id in query_ids:
                errors.append(f"{location}: duplicate query_id also at {query_ids[query_id]}")
            else:
                query_ids[query_id] = location
            metadata = row.get("metadata") or {}
            category = metadata.get("category")
            if not isinstance(category, str):
                errors.append(f"{location}: missing category")
                continue
            parent = metadata.get("parent_query_id")
            if parent is not None:
                parent_set.add(str(parent))

            signature = json.dumps(messages, sort_keys=True, ensure_ascii=False)
            if signature in conversation_signatures:
                errors.append(f"{location}: exact duplicate conversation also at {conversation_signatures[signature]}")
            else:
                conversation_signatures[signature] = location

            if category.startswith("research_"):
                if not row.get("tools"):
                    errors.append(f"{location}: research row has no tool schemas")
                validate_tool_sequence(row, location, errors)
                if category == "research_recovered":
                    final = messages[-1].get("content") or ""
                    exact = re.search(r"(?im)^Exact Answer:\s*(.+?)\s*$", final)
                    tool_text = " ".join(
                        message.get("content") or "" for message in messages if message.get("role") == "tool"
                    )
                    if len(messages) != 5:
                        errors.append(f"{location}: recovered research must contain exactly one tool exchange")
                    if not exact or normalize(exact.group(1)) not in normalize(tool_text):
                        errors.append(f"{location}: recovered answer is not literal in retained evidence")
                    if re.search(r"\[(?:Full )?Document content for docid", tool_text, re.I):
                        errors.append(f"{location}: placeholder document content in recovered evidence")
                    evidence_docid = str(metadata.get("evidence_docid") or "")
                    if not evidence_docid or f"[{evidence_docid}]" not in final:
                        errors.append(f"{location}: recovered final does not cite its evidence docid")
            elif category in CONTROL_CATEGORIES:
                if len(messages) != 3 or row.get("tools") != []:
                    errors.append(f"{location}: control row must be three messages with tools=[]")
                serialized = json.dumps(row, ensure_ascii=False).casefold()
                if "<think>" in serialized or "reasoning_content" in serialized:
                    errors.append(f"{location}: hidden reasoning in control row")
                if re.search(r"variation\s*\d+", serialized):
                    errors.append(f"{location}: synthetic Variation artifact")
                if any(phrase in serialized for phrase in BANNED):
                    errors.append(f"{location}: banned absolute-policy phrase")
                user = messages[1]["content"]
                answer = messages[2]["content"]
                control_prompts.append((location, user))
                assistant_answers[(category, answer)] += 1
                if category in {"ordinary_tasks", "exact_controls", "injection_resistance"}:
                    if any(brand in f"{user}\n{answer}".casefold() for brand in BRANDS):
                        identity_leaks += 1
                        errors.append(f"{location}: identity leakage into non-identity task")
                if category == "provenance" and len(answer.split()) > 35:
                    errors.append(f"{location}: provenance answer is too long")
                if category == "exact_controls":
                    exact_count += 1
                    if not exact_control_ok(row):
                        exact_failures += 1
                        errors.append(f"{location}: exact output does not match metadata contract")
            else:
                errors.append(f"{location}: unknown category {category!r}")

    for index, (left_location, left_prompt) in enumerate(control_prompts):
        for right_location, right_prompt in control_prompts[:index]:
            if near_duplicate(left_prompt, right_prompt):
                errors.append(f"near-duplicate control prompts: {right_location} and {left_location}")

    for (category, answer), count in assistant_answers.items():
        if category == "provenance" and count > 2:
            errors.append(f"provenance response repeated {count} times: {answer!r}")

    overlap = sorted(parent_train & parent_validation)
    if overlap:
        errors.append(f"parent query IDs cross train/validation: {overlap}")
    research_train = sum(row["metadata"]["category"].startswith("research_") for row in train)
    research_fraction = research_train / len(train) if train else 0
    if not 0.60 <= research_fraction <= 0.70:
        errors.append(f"research row fraction outside 60-70%: {research_fraction:.3f}")
    streak = maximum_group_streak(train) if train else 0
    if streak > 20:
        errors.append(f"dataset appears insufficiently shuffled; maximum group streak={streak}")

    control_ids = {row.get("query_id") for row in controls}
    combined_control_ids = {
        row.get("query_id")
        for row in train + validation
        if (row.get("metadata") or {}).get("category") in CONTROL_CATEGORIES
    }
    if control_ids != combined_control_ids:
        errors.append("control all-file does not equal train+validation controls")

    research_parent_counts = collections.Counter(
        str(row["metadata"].get("parent_query_id"))
        for row in train
        if row["metadata"]["category"].startswith("research_")
        and row["metadata"]["category"] != "research_recovered"
    )
    bad_parent_counts = {parent: count for parent, count in research_parent_counts.items() if count != 5}
    if bad_parent_counts:
        errors.append(f"research parents do not have five distinct views: {bad_parent_counts}")
    recovered_parent_counts = collections.Counter(
        str(row["metadata"].get("parent_query_id"))
        for row in train
        if row["metadata"]["category"] == "research_recovered"
    )
    bad_recovered_counts = {parent: count for parent, count in recovered_parent_counts.items() if count != 1}
    if bad_recovered_counts:
        errors.append(f"recovered research parents are not unique: {bad_recovered_counts}")
    holdout_leaks = sorted(parent for parent in parent_train if parent.isdigit() and 769 <= int(parent) <= 797)
    if holdout_leaks:
        errors.append(f"evaluation holdout parents leaked into training: {holdout_leaks}")

    report = {
        "errors": errors,
        "train_rows": len(train),
        "validation_rows": len(validation),
        "control_rows": len(controls),
        "research_train_rows": research_train,
        "research_train_fraction": round(research_fraction, 4),
        "exact_controls_checked": exact_count,
        "exact_control_failures": exact_failures,
        "identity_leakage_count": identity_leaks,
        "parent_query_overlap": overlap,
        "recovered_research_rows": sum(recovered_parent_counts.values()),
        "evaluation_holdout_leaks": holdout_leaks,
        "maximum_group_streak": streak,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", required=True, type=Path)
    parser.add_argument("--validation", required=True, type=Path)
    parser.add_argument("--controls", required=True, type=Path)
    parser.add_argument("--prompt", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(1 if validate(args)["errors"] else 0)


if __name__ == "__main__":
    main()
