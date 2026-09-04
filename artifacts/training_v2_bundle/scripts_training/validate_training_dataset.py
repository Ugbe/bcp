"""Validate the structural contract of cleaned SFT JSONL datasets."""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path


FINAL_RE = re.compile(
    r"(?s)^Explanation:\s*.+\nExact Answer:\s*.+\nConfidence:\s*\d+(?:\.\d+)?%\s*$"
)
CITATION_RE = re.compile(r"\[\d+\]")
THINK_BLOCK_RE = re.compile(r"(?s)<think>\s*(.*?)\s*</think>")
REPEATED_WORD_RE = re.compile(
    r"\b(\w{3,})\b(?:[\s,.;:!?-]+\1\b){3,}", re.IGNORECASE
)


def validate_research(path: Path) -> dict:
    errors: list[str] = []
    query_ids: set[str] = set()
    tool_counts: collections.Counter[str] = collections.Counter()
    estimated_tokens: list[int] = []
    assistant_turns = 0
    reasoning_turns = 0

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON: {exc}")
                continue
            query_id = str(record.get("query_id"))
            if query_id in query_ids:
                errors.append(f"line {line_number}: duplicate query_id {query_id}")
            query_ids.add(query_id)

            schemas = record.get("tools")
            schema_names = {
                schema.get("function", {}).get("name")
                for schema in schemas or []
                if isinstance(schema, dict)
            }
            if schema_names != {"search", "get_document"}:
                errors.append(f"line {line_number}: unexpected tool schemas {schema_names}")

            messages = record.get("messages")
            if not isinstance(messages, list) or len(messages) < 5:
                errors.append(f"line {line_number}: invalid/short messages")
                continue
            if [messages[0].get("role"), messages[1].get("role")] != ["system", "user"]:
                errors.append(f"line {line_number}: conversation must start system,user")
            if messages[-1].get("role") != "assistant":
                errors.append(f"line {line_number}: conversation must end with assistant")

            serialized = json.dumps(messages, ensure_ascii=False)
            if "reasoning_content" in serialized:
                errors.append(f"line {line_number}: duplicated reasoning_content field")
            if "Variation " in serialized:
                errors.append(f"line {line_number}: leaked synthetic variation marker")
            if serialized.lower().count("<think>") != serialized.lower().count("</think>"):
                errors.append(f"line {line_number}: unbalanced think tags")
            if REPEATED_WORD_RE.search(serialized):
                errors.append(f"line {line_number}: repeated-word degeneration")

            middle = messages[2:-1]
            if len(middle) % 2:
                errors.append(f"line {line_number}: unpaired tool messages")
            for offset in range(0, len(middle), 2):
                if offset + 1 >= len(middle):
                    break
                assistant, tool = middle[offset], middle[offset + 1]
                assistant_turns += 1
                if THINK_BLOCK_RE.search(str(assistant.get("content") or "")):
                    reasoning_turns += 1
                calls = assistant.get("tool_calls") or []
                if assistant.get("role") != "assistant" or len(calls) != 1:
                    errors.append(f"line {line_number}: invalid assistant tool-call turn")
                    continue
                call = calls[0]
                function = call.get("function", {})
                name = function.get("name")
                arguments = function.get("arguments")
                if name not in schema_names or not isinstance(arguments, dict):
                    errors.append(f"line {line_number}: invalid tool call {name}/{arguments}")
                if tool.get("role") != "tool":
                    errors.append(f"line {line_number}: missing tool response")
                if tool.get("tool_call_id") != call.get("id") or tool.get("name") != name:
                    errors.append(f"line {line_number}: mismatched tool response")
                if not str(tool.get("content") or "").strip():
                    errors.append(f"line {line_number}: empty tool response")
                tool_counts[str(name)] += 1

            assistant_turns += 1
            final = str(messages[-1].get("content") or "")
            final_think = THINK_BLOCK_RE.search(final)
            if final_think:
                reasoning_turns += 1
                if len(final_think.group(1)) > 1200:
                    errors.append(f"line {line_number}: final reasoning exceeds cap")
            visible_final = THINK_BLOCK_RE.sub("", final).strip()
            if not FINAL_RE.match(visible_final):
                errors.append(f"line {line_number}: malformed final response")
            explanation = visible_final.split("\nExact Answer:", 1)[0]
            if not CITATION_RE.search(explanation):
                errors.append(f"line {line_number}: explanation has no document citation")
            estimated_tokens.append(sum(len(str(m.get("content") or "")) for m in messages) // 4)

    return {
        "path": str(path),
        "records": len(query_ids),
        "tool_calls": dict(tool_counts),
        "estimated_tokens_min": min(estimated_tokens) if estimated_tokens else 0,
        "estimated_tokens_max": max(estimated_tokens) if estimated_tokens else 0,
        "assistant_turns": assistant_turns,
        "reasoning_turns": reasoning_turns,
        "reasoning_turn_percent": round(100 * reasoning_turns / assistant_turns, 2)
        if assistant_turns
        else 0,
        "errors": errors,
    }


def validate_identity(path: Path) -> dict:
    errors: list[str] = []
    signatures: set[str] = set()
    records = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            records += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {line_number}: invalid JSON: {exc}")
                continue
            messages = record.get("messages") or []
            signature = json.dumps(messages, sort_keys=True, ensure_ascii=False)
            if signature in signatures:
                errors.append(f"line {line_number}: exact duplicate")
            signatures.add(signature)
            if any(message.get("role") == "tool" or message.get("tool_calls") for message in messages):
                errors.append(f"line {line_number}: tool content in identity corpus")
            if "<think>" in signature.lower() or "reasoning_content" in signature:
                errors.append(f"line {line_number}: leaked hidden reasoning")
            if "Variation " in signature:
                errors.append(f"line {line_number}: leaked synthetic variation marker")
    return {"path": str(path), "records": records, "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research", required=True, type=Path)
    parser.add_argument("--research-validation", type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    research_report = validate_research(args.research)
    report = {
        "research": research_report,
        "identity": validate_identity(args.identity),
    }
    if args.research_validation:
        validation_report = validate_research(args.research_validation)
        report["research_validation"] = validation_report
        train_ids = {
            str(json.loads(line)["query_id"])
            for line in args.research.open("r", encoding="utf-8")
        }
        validation_ids = {
            str(json.loads(line)["query_id"])
            for line in args.research_validation.open("r", encoding="utf-8")
        }
        overlap = sorted(train_ids & validation_ids)
        report["train_validation_overlap"] = overlap
        if overlap:
            research_report["errors"].append(
                f"train/validation query overlap: {overlap}"
            )
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    validation_errors = report.get("research_validation", {}).get("errors", [])
    if report["research"]["errors"] or report["identity"]["errors"] or validation_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
