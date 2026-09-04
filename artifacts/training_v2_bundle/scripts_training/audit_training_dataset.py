"""Stream a training JSONL file and report structural/quality risks.

This intentionally does not import the training stack, so it can run on the
machine preparing the data as well as on the GPU host.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import statistics
from pathlib import Path


THINK_RE = re.compile(r"<think>\s*(.*?)\s*</think>", re.DOTALL | re.IGNORECASE)
REPEATED_WORD_RE = re.compile(r"\b(\w{3,})\b(?:[\s,.;:!?-]+\1\b){3,}", re.IGNORECASE)


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def audit(path: Path) -> dict:
    sources: collections.Counter[str] = collections.Counter()
    role_sequences: collections.Counter[str] = collections.Counter()
    tool_names: collections.Counter[str] = collections.Counter()
    message_key_sets: collections.Counter[str] = collections.Counter()
    user_hashes: collections.Counter[str] = collections.Counter()
    conversation_hashes: collections.Counter[str] = collections.Counter()
    token_counts: list[int] = []

    report = collections.Counter()
    tool_key_sets: collections.Counter[str] = collections.Counter()

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                report["blank_lines"] += 1
                continue
            report["lines"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                report["invalid_json"] += 1
                continue

            messages = record.get("messages")
            if not isinstance(messages, list) or not messages:
                report["invalid_messages"] += 1
                continue

            source = str(record.get("source") or "<missing>")
            sources[source] += 1
            role_sequences[">".join(str(m.get("role")) for m in messages)] += 1
            conversation_hashes[_digest(json.dumps(messages, sort_keys=True))] += 1

            if isinstance(record.get("n_tokens"), int):
                token_counts.append(record["n_tokens"])

            users = [m for m in messages if m.get("role") == "user"]
            if users:
                user_hashes[_digest(str(users[-1].get("content") or ""))] += 1

            for message in messages:
                message_key_sets[",".join(sorted(message))] += 1
                content = str(message.get("content") or "")
                if message.get("role") == "assistant":
                    report["assistant_messages"] += 1
                    if not content.strip():
                        report["empty_assistant"] += 1
                    if THINK_RE.search(content):
                        report["assistant_with_think_tags"] += 1
                    if message.get("reasoning_content"):
                        report["assistant_with_reasoning_content"] += 1
                    if "Variation " in content:
                        report["assistant_with_variation_artifact"] += 1
                    if REPEATED_WORD_RE.search(content):
                        report["assistant_with_repeated_word_run"] += 1
                    if "Exact Answer:" in content and "Confidence:" in content:
                        report["assistant_with_benchmark_final"] += 1
                    if message.get("tool_calls"):
                        report["embedded_message_tool_calls"] += len(message["tool_calls"])

            tools = record.get("tools")
            if isinstance(tools, list) and tools:
                report["records_with_top_level_tool_trace"] += 1
                report["top_level_tool_calls"] += len(tools)
                for tool in tools:
                    tool_key_sets[",".join(sorted(tool))] += 1
                    tool_names[str(tool.get("name") or "<missing>")] += 1
                    if not isinstance(tool.get("arguments"), dict):
                        report["tool_calls_with_invalid_arguments"] += 1
                    if not isinstance(tool.get("output"), str) or not tool.get("output", "").strip():
                        report["tool_calls_with_empty_output"] += 1

            if record.get("answer"):
                report["records_with_top_level_answer"] += 1
            if record.get("query_id") is not None:
                report["records_with_query_id"] += 1

    duplicate_user_rows = sum(count - 1 for count in user_hashes.values() if count > 1)
    duplicate_conversations = sum(
        count - 1 for count in conversation_hashes.values() if count > 1
    )
    output = dict(sorted(report.items()))
    output.update(
        {
            "duplicate_user_rows": duplicate_user_rows,
            "duplicate_exact_conversations": duplicate_conversations,
            "sources": dict(sources.most_common()),
            "role_sequences": dict(role_sequences.most_common()),
            "message_key_sets": dict(message_key_sets.most_common()),
            "tool_names": dict(tool_names.most_common()),
            "tool_key_sets": dict(tool_key_sets.most_common()),
        }
    )
    if token_counts:
        output["declared_tokens"] = {
            "min": min(token_counts),
            "median": statistics.median(token_counts),
            "mean": round(statistics.mean(token_counts), 2),
            "max": max(token_counts),
            "sum": sum(token_counts),
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = audit(args.dataset)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
