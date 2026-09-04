"""Build clean, separated SFT datasets from the original mixed JSONL.

The original file stores tool executions in a top-level trace that ordinary
chat-template training ignores. This script reconstructs those executions as
interleaved assistant/tool messages, filters against BrowseComp ground truth,
and keeps identity examples out of the research corpus.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import unicodedata
from pathlib import Path


THINK_RE = re.compile(r"<think>\s*(.*?)\s*</think>", re.DOTALL | re.IGNORECASE)
EXACT_ANSWER_PATTERNS = (
    re.compile(r"(?im)^\s*\*\*Exact Answer:\*\*\s*(.+?)\s*$"),
    re.compile(r"(?im)^\s*\*\*Exact Answer\*\*:\s*(.+?)\s*$"),
    re.compile(r"(?im)^\s*Exact Answer:\s*(.+?)\s*$"),
)
CONFIDENCE_PATTERNS = (
    re.compile(r"(?im)^\s*\*\*Confidence:\*\*\s*(\d+(?:\.\d+)?)\s*%?"),
    re.compile(r"(?im)^\s*\*\*Confidence\*\*:\s*(\d+(?:\.\d+)?)\s*%?"),
    re.compile(r"(?im)^\s*Confidence:\s*(\d+(?:\.\d+)?)\s*%?"),
)
EXPLANATION_RE = re.compile(
    r"(?is)(?:^|\n)\s*(?:\*\*)?Explanation:(?:\*\*)?\s*(.*?)"
    r"(?=\n\s*(?:\*\*)?Exact Answer(?::|\*\*:))",
)
UNCERTAINTY_RE = re.compile(
    r"unable to (?:find|determine|identify)|cannot determine|could not find|"
    r"insufficient (?:information|evidence)|no definitive|not possible to identify",
    re.IGNORECASE,
)
CITATION_RE = re.compile(r"\[\d+\]")

RESEARCH_SYSTEM_PROMPT = """You are a careful research assistant with access to search and document-retrieval tools. Use tools to gather evidence before answering. Do not guess when evidence is insufficient. End with exactly these fields:
Explanation: a concise evidence-based explanation with document citations such as [123]
Exact Answer: the succinct answer
Confidence: a calibrated percentage from 0% to 100%"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the knowledge base for relevant documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": "Retrieve a document by its document ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "docid": {"type": "string", "description": "Document ID"}
                },
                "required": ["docid"],
            },
        },
    },
]
STEP_RE = re.compile(r"(?m)^Step\s+(\d+):\s*")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = re.sub(r"\[\d+\]", " ", value)
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


def extract_exact_answer(content: str) -> str:
    for pattern in EXACT_ANSWER_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group(1).strip().strip("*_`# ")
    return ""


def extract_confidence(content: str) -> float | None:
    for pattern in CONFIDENCE_PATTERNS:
        match = pattern.search(content)
        if match:
            return min(100.0, float(match.group(1)))
    return None


def extract_explanation(content: str) -> str:
    outside_think = THINK_RE.sub("", content).strip()
    match = EXPLANATION_RE.search(outside_think)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def parse_reasoning_steps(reasoning: str, char_limit: int) -> dict[int, str]:
    matches = list(STEP_RE.finditer(reasoning))
    steps: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(reasoning)
        text = reasoning[match.end() : end].strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text[:char_limit].rstrip()
        if text:
            steps[int(match.group(1))] = text
    return steps


def answer_match_kind(predicted: str, gold: str) -> str | None:
    pred_norm = normalize(predicted)
    gold_norm = normalize(gold)
    if not pred_norm or not gold_norm:
        return None
    if pred_norm == gold_norm:
        return "exact"
    if sorted(pred_norm.split()) == sorted(gold_norm.split()):
        return "token_order"
    return None


def load_source(path: Path) -> tuple[list[dict], list[dict]]:
    research: list[dict] = []
    identity: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            record = json.loads(line)
            record["_source_line"] = line_number
            if record.get("source") == "all_batches_1_to_36":
                research.append(record)
            elif record.get("source") == "manual_identity_probes":
                identity.append(record)
    return research, identity


def load_ground_truth(path: Path, wanted_ids: set[str]) -> dict[str, dict]:
    ground_truth: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            query_id = str(record.get("query_id"))
            if query_id in wanted_ids:
                ground_truth[query_id] = {
                    "query": record.get("query", ""),
                    "answer": record.get("answer", ""),
                }
    return ground_truth


def canonicalize_tools(
    query_id: str,
    tools: list[dict],
    output_limit: int,
    reasoning_steps: dict[int, str],
) -> tuple[list[dict], list[str], set[int]]:
    messages: list[dict] = []
    problems: list[str] = []
    seen_calls: set[tuple[str, str]] = set()
    used_reasoning_steps: set[int] = set()

    for index, event in enumerate(tools, start=1):
        original_name = event.get("name")
        arguments = event.get("arguments")
        output = event.get("output")
        if not isinstance(arguments, dict):
            problems.append("invalid_tool_arguments")
            continue
        if not isinstance(output, str) or not output.strip():
            problems.append("empty_tool_output")
            continue

        if original_name in {"search", "local_knowledge_base_retrieval"}:
            name = "search"
            query = arguments.get("query") or arguments.get("user_query")
            if not isinstance(query, str) or not query.strip():
                problems.append("missing_search_query")
                continue
            canonical_arguments = {"query": query.strip()}
        elif original_name == "get_document":
            name = "get_document"
            docid = arguments.get("docid")
            if docid is None or not str(docid).strip():
                problems.append("missing_docid")
                continue
            canonical_arguments = {"docid": str(docid).strip()}
        else:
            problems.append("unknown_tool_name")
            continue

        signature = (name, json.dumps(canonical_arguments, sort_keys=True))
        if signature in seen_calls:
            continue
        seen_calls.add(signature)

        call_id = f"call_{query_id}_{index}"
        event_step = event.get("step")
        eligible_steps = [
            step
            for step in reasoning_steps
            if step not in used_reasoning_steps
            and isinstance(event_step, int)
            and step <= event_step
        ]
        rationale = ""
        if eligible_steps:
            rationale_step = max(eligible_steps)
            used_reasoning_steps.add(rationale_step)
            rationale = reasoning_steps[rationale_step]
        messages.append(
            {
                "role": "assistant",
                "content": f"<think>\n{rationale}\n</think>" if rationale else "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": canonical_arguments,
                        },
                    }
                ],
            }
        )
        truncated = output.strip()[:output_limit]
        if len(output.strip()) > output_limit:
            truncated += "\n[tool output truncated during dataset preparation]"
        messages.append(
            {
                "role": "tool",
                "name": name,
                "tool_call_id": call_id,
                "content": truncated,
            }
        )
    return messages, problems, used_reasoning_steps


def clean_research_record(
    record: dict,
    truth: dict,
    min_confidence: float,
    max_tool_calls: int,
    output_limit: int,
    max_estimated_tokens: int,
    reasoning_chars: int,
) -> tuple[dict | None, list[str], dict]:
    reasons: list[str] = []
    query_id = str(record.get("query_id"))
    messages = record.get("messages") or []
    assistants = [m for m in messages if m.get("role") == "assistant"]
    users = [m for m in messages if m.get("role") == "user"]
    tools = record.get("tools") or []
    if not assistants or not users:
        return None, ["missing_user_or_assistant"], {}

    content = str(assistants[-1].get("content") or "")
    predicted = extract_exact_answer(content)
    confidence = extract_confidence(content)
    explanation = extract_explanation(content)
    reasoning = str(assistants[-1].get("reasoning_content") or "")
    reasoning_steps = parse_reasoning_steps(reasoning, reasoning_chars)
    gold_answer = str(truth.get("answer") or "")
    match_kind = answer_match_kind(predicted, gold_answer)

    if not match_kind:
        reasons.append("answer_not_strictly_equal_to_ground_truth")
    if confidence is None or confidence < min_confidence:
        reasons.append("low_or_missing_confidence")
    if not explanation:
        reasons.append("missing_parseable_explanation")
    elif not CITATION_RE.search(explanation):
        reasons.append("missing_document_citation")
    if UNCERTAINTY_RE.search(content):
        reasons.append("uncertain_or_failed_research_language")
    if not tools:
        reasons.append("missing_tool_trace")

    tool_messages, tool_problems, used_reasoning_steps = canonicalize_tools(
        query_id, tools, output_limit, reasoning_steps
    )
    reasons.extend(tool_problems)
    canonical_call_count = len(tool_messages) // 2
    if canonical_call_count > max_tool_calls:
        reasons.append("too_many_tool_calls")

    combined_outputs = " ".join(
        message["content"] for message in tool_messages if message["role"] == "tool"
    )
    gold_norm = normalize(gold_answer)
    evidence_norm = normalize(combined_outputs)
    if gold_norm and gold_norm not in evidence_norm:
        reasons.append("gold_answer_not_literal_in_retained_tool_evidence")

    unused_reasoning = [
        reasoning_steps[step]
        for step in sorted(reasoning_steps)
        if step not in used_reasoning_steps
    ]
    final_reasoning = unused_reasoning[-1] if unused_reasoning else ""
    if not final_reasoning:
        reasons.append("missing_final_reasoning")
    final_content = (
        (f"<think>\n{final_reasoning}\n</think>\n\n" if final_reasoning else "")
        + f"Explanation: {explanation}\n"
        f"Exact Answer: {gold_answer.strip()}\n"
        f"Confidence: {int(round(confidence or 0))}%"
    )
    clean_messages = [
        {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
        {"role": "user", "content": str(truth.get("query") or users[-1].get("content") or "").strip()},
        *tool_messages,
        {"role": "assistant", "content": final_content},
    ]
    estimated_tokens = sum(
        len(str(message.get("content") or "")) for message in clean_messages
    ) // 4
    if estimated_tokens > max_estimated_tokens:
        reasons.append("estimated_context_too_long")

    metadata = {
        "query_id": query_id,
        "source_line": record.get("_source_line"),
        "predicted_answer": predicted,
        "gold_answer": gold_answer,
        "match_kind": match_kind,
        "original_confidence": confidence,
        "original_tool_calls": len(tools),
        "canonical_tool_calls": canonical_call_count,
        "estimated_tokens": estimated_tokens,
        "reasoning_tool_turns": len(used_reasoning_steps),
        "has_final_reasoning": bool(final_reasoning),
    }
    if reasons:
        return None, sorted(set(reasons)), metadata

    clean = {
        "messages": clean_messages,
        "tools": TOOL_SCHEMAS,
        "query_id": query_id,
        "source": "browsecomp_grounded_tool_trajectory",
        "metadata": {
            "match_kind": match_kind,
            "original_confidence": confidence,
            "canonical_tool_calls": canonical_call_count,
            "estimated_tokens": estimated_tokens,
            "reasoning_tool_turns": len(used_reasoning_steps),
        },
    }
    return clean, [], metadata


def clean_identity_records(records: list[dict]) -> tuple[list[dict], int]:
    clean: list[dict] = []
    seen: set[str] = set()
    dropped = 0
    for record in records:
        messages = record.get("messages") or []
        normalized_messages: list[dict] = []
        for message in messages:
            role = message.get("role")
            content = THINK_RE.sub("", str(message.get("content") or "")).strip()
            if role in {"system", "user", "assistant"} and content:
                normalized_messages.append({"role": role, "content": content})
        signature = json.dumps(normalized_messages, sort_keys=True, ensure_ascii=False)
        if not normalized_messages or signature in seen:
            dropped += 1
            continue
        seen.add(signature)
        clean.append(
            {
                "messages": normalized_messages,
                "source": "manual_identity_probes_clean",
            }
        )
    return clean, dropped


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def validation_bucket(query_id: str) -> int:
    digest = hashlib.sha256(query_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--min-confidence", type=float, default=70.0)
    parser.add_argument("--max-tool-calls", type=int, default=24)
    parser.add_argument("--tool-output-chars", type=int, default=5000)
    parser.add_argument("--max-estimated-tokens", type=int, default=30000)
    parser.add_argument("--reasoning-chars-per-turn", type=int, default=1200)
    parser.add_argument("--validation-percent", type=int, default=10)
    args = parser.parse_args()

    research, identity = load_source(args.input)
    truth = load_ground_truth(
        args.ground_truth, {str(record.get("query_id")) for record in research}
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    accepted: list[dict] = []
    rejected: list[dict] = []
    reason_counts: collections.Counter[str] = collections.Counter()
    for record in research:
        query_id = str(record.get("query_id"))
        cleaned, reasons, metadata = clean_research_record(
            record,
            truth.get(query_id, {}),
            args.min_confidence,
            args.max_tool_calls,
            args.tool_output_chars,
            args.max_estimated_tokens,
            args.reasoning_chars_per_turn,
        )
        if cleaned:
            accepted.append(cleaned)
        else:
            reason_counts.update(reasons)
            rejected.append({**metadata, "reasons": reasons})

    if not 0 <= args.validation_percent < 100:
        parser.error("--validation-percent must be between 0 and 99")
    validation = [
        row
        for row in accepted
        if validation_bucket(str(row["query_id"])) < args.validation_percent
    ]
    train = [row for row in accepted if row not in validation]

    clean_identity, identity_dropped = clean_identity_records(identity)
    write_jsonl(args.output_dir / "research_train.jsonl", train)
    write_jsonl(args.output_dir / "research_validation.jsonl", validation)
    write_jsonl(args.output_dir / "research_rejected.jsonl", rejected)
    write_jsonl(args.output_dir / "identity_train.jsonl", clean_identity)

    report = {
        "input": str(args.input),
        "ground_truth": str(args.ground_truth),
        "thresholds": {
            "min_confidence": args.min_confidence,
            "max_tool_calls": args.max_tool_calls,
            "tool_output_chars": args.tool_output_chars,
            "max_estimated_tokens": args.max_estimated_tokens,
            "reasoning_chars_per_turn": args.reasoning_chars_per_turn,
            "validation_percent": args.validation_percent,
        },
        "research_input": len(research),
        "research_accepted": len(accepted),
        "research_train": len(train),
        "research_validation": len(validation),
        "research_rejected": len(rejected),
        "rejection_reasons": dict(reason_counts.most_common()),
        "identity_input_manual_only": len(identity),
        "identity_accepted": len(clean_identity),
        "identity_duplicates_dropped": identity_dropped,
        "excluded_crowther_persona_rows": 1500,
    }
    report_path = args.output_dir / "cleaning_report.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
