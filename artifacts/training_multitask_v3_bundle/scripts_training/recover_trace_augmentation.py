"""Recover a small, evidence-grounded subset of the 500 answer-only trace rows.

The source rows are not genuine trajectories: they contain only system/user/final
messages. This script reopens the referenced Markdown archives, rejects synthetic
or unsupported evidence, keeps one row per parent query, excludes the benchmark
holdout, and reconstructs a minimal tool/evidence/final-answer conversation.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

try:
    from scripts_training.build_multitask_dataset_v3 import SYSTEM_PROMPT
    from scripts_training.clean_training_dataset import TOOL_SCHEMAS, answer_match_kind, load_ground_truth
except ModuleNotFoundError:
    from build_multitask_dataset_v3 import SYSTEM_PROMPT
    from clean_training_dataset import TOOL_SCHEMAS, answer_match_kind, load_ground_truth


TOOL_SECTION_RE = re.compile(
    r"### Step \d+ . Tool call: `([^`]+)`.*?"
    r"\*\*Arguments\*\*\s*```json\s*(.*?)\s*```.*?"
    r"\*\*Output\*\*\s*(.*?)(?=\n### Step \d+|\n## Final answer|\Z)",
    re.S,
)
DETAIL_RE = re.compile(r"<details.*?</details>", re.S | re.I)
PARENT_RE = re.compile(r"query_(\d+)_\d+\.md$")
HOLDOUT_RANGE = range(769, 798)
# Every structurally grounded candidate was manually checked for whether the
# retained evidence establishes the relation actually asked about (not merely
# whether it happens to contain the answer string).
SEMANTICALLY_REVIEWED_PARENTS = {
    "78", "85", "210", "226", "231", "237", "239", "245", "248", "270",
    "802", "819", "821", "822", "828", "830", "835", "844", "872", "980",
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def normalize(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def parse_parent(trace_file: str) -> str | None:
    match = PARENT_RE.search(trace_file.replace("\\", "/"))
    return match.group(1) if match else None


def canonical_call(name: str, arguments_text: str) -> tuple[str, dict] | None:
    try:
        arguments = json.loads(arguments_text)
    except json.JSONDecodeError:
        return None
    if name in {"search", "local_knowledge_base_retrieval"}:
        query = arguments.get("query") or arguments.get("user_query")
        return ("search", {"query": query.strip()}) if isinstance(query, str) and query.strip() else None
    if name == "get_document" and arguments.get("docid") is not None:
        return "get_document", {"docid": str(arguments["docid"]).strip()}
    return None


def matching_evidence(output: str, answer: str) -> tuple[str, str] | None:
    answer_norm = normalize(answer)
    blocks = DETAIL_RE.findall(output) or [output]
    for block in blocks:
        if re.search(r"\[(?:Full )?Document content for docid", block, re.I):
            continue
        if answer_norm and answer_norm in normalize(block):
            docid = re.search(r"docid\s+(\d+)", block, re.I)
            if docid and len(block) <= 6000:
                return block.strip(), docid.group(1)
    return None


def evidence_title(evidence: str) -> str:
    match = re.search(r"(?im)^title:\s*(.+?)\s*$", evidence)
    return match.group(1).strip() if match else ""


def evidence_excerpt(evidence: str, answer: str) -> str:
    plain = re.sub(r"<[^>]+>|```|---", " ", evidence)
    plain = re.sub(r"\s+", " ", plain).strip()
    index = plain.casefold().find(answer.casefold())
    if index < 0:
        return answer
    start = max(0, index - 100)
    end = min(len(plain), index + len(answer) + 180)
    excerpt = plain[start:end].strip(" ,.;:-")
    if start:
        excerpt = "…" + excerpt
    if end < len(plain):
        excerpt += "…"
    return excerpt


def best_candidate(trace_path: Path, answer: str) -> dict | None:
    text = trace_path.read_text(encoding="utf-8", errors="replace")
    choices = []
    for name, arguments_text, output in TOOL_SECTION_RE.findall(text):
        call = canonical_call(name, arguments_text)
        evidence = matching_evidence(output, answer)
        if call and evidence:
            canonical_name, arguments = call
            evidence_text, docid = evidence
            priority = 0 if canonical_name == "get_document" else 1
            choices.append((priority, len(evidence_text), canonical_name, arguments, evidence_text, docid))
    if not choices:
        return None
    _, _, name, arguments, evidence, docid = min(choices, key=lambda item: (item[0], item[1]))
    if name == "search" and (title := evidence_title(evidence)):
        arguments = {"query": title}
    return {"name": name, "arguments": arguments, "evidence": evidence, "docid": docid}


def stable_rank(parent: str, trace_file: str) -> str:
    return hashlib.sha256(f"{parent}\0{trace_file}".encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--augmentation", type=Path, default=Path("data/training_identity_v2/trace_augmentation_500.jsonl"))
    parser.add_argument("--ground-truth", type=Path, default=Path("data/browsecomp_plus_decrypted.jsonl"))
    parser.add_argument("--existing-train", type=Path, default=Path("data/training_clean_v2/research_train.jsonl"))
    parser.add_argument("--existing-validation", type=Path, default=Path("data/training_clean_v2/research_validation.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/training_multitask_v3"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows = read_jsonl(args.augmentation)
    existing_rows = read_jsonl(args.existing_train) + read_jsonl(args.existing_validation)
    existing_parents = {str(row["query_id"]) for row in existing_rows}
    wanted = {parent for row in rows if (parent := parse_parent(row["metadata"]["trace_file"]))}
    truth = load_ground_truth(args.ground_truth, wanted)

    candidates: dict[str, list[dict]] = collections.defaultdict(list)
    rejected: list[dict] = []
    reason_counts: collections.Counter[str] = collections.Counter()
    for index, row in enumerate(rows, start=1):
        trace_file = row["metadata"]["trace_file"]
        parent = parse_parent(trace_file)
        reasons = []
        if parent is None:
            reasons.append("unparseable_parent_query_id")
        elif int(parent) in HOLDOUT_RANGE:
            reasons.append("reserved_evaluation_holdout_769_to_797")
        elif parent in existing_parents:
            reasons.append("parent_already_present_in_v3_research")
        gold = truth.get(parent or "", {})
        source_answer = row["messages"][-1]["content"].removeprefix("The answer is: ").strip()
        if not gold or not answer_match_kind(source_answer, str(gold.get("answer") or "")):
            reasons.append("source_answer_not_equal_to_benchmark_ground_truth")
        trace_path = Path(trace_file)
        if not trace_path.exists():
            reasons.append("missing_trace_archive_file")
            candidate = None
        else:
            candidate = best_candidate(trace_path, str(gold.get("answer") or source_answer))
            if not candidate:
                reasons.append("gold_answer_not_literal_in_real_nonplaceholder_tool_output")
            elif parent not in SEMANTICALLY_REVIEWED_PARENTS:
                reasons.append("retained_evidence_does_not_establish_the_asked_relation")
        if reasons:
            reason_counts.update(reasons)
            rejected.append({"source_row": index, "parent_query_id": parent, "trace_file": trace_file, "reasons": sorted(set(reasons))})
        else:
            candidates[parent].append({"trace_file": trace_file, "gold": gold, **candidate})

    accepted = []
    duplicate_variants = 0
    for parent, variants in candidates.items():
        variants.sort(key=lambda item: stable_rank(parent, item["trace_file"]))
        chosen = variants[0]
        duplicate_variants += len(variants) - 1
        call_id = f"call_recovered_{parent}_1"
        answer = str(chosen["gold"]["answer"]).strip()
        excerpt = evidence_excerpt(chosen["evidence"], answer)
        accepted.append(
            {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": str(chosen["gold"]["query"]).strip()},
                    {
                        "role": "assistant",
                        "content": "I’ll retrieve evidence for the most distinctive constraints before answering.",
                        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": chosen["name"], "arguments": chosen["arguments"]}}],
                    },
                    {"role": "tool", "name": chosen["name"], "tool_call_id": call_id, "content": chosen["evidence"]},
                    {
                        "role": "assistant",
                        "content": (
                            f"Explanation: The retrieved evidence states: {excerpt} [{chosen['docid']}]\n"
                            f"Exact Answer: {answer}\nConfidence: 90%"
                        ),
                    },
                ],
                "tools": TOOL_SCHEMAS,
                "query_id": f"research:recovered:{parent}",
                "source": "browsecomp_v3_recovered_evidence",
                "metadata": {
                    "category": "research_recovered",
                    "parent_query_id": parent,
                    "derived_view": True,
                    "trace_file": chosen["trace_file"],
                    "evidence_docid": chosen["docid"],
                    "recovery_policy": "one_parent_one_real_evidence_block",
                },
            }
        )
    accepted.sort(key=lambda row: hashlib.sha256(row["query_id"].encode()).hexdigest())
    rejected.extend(
        {"parent_query_id": parent, "trace_file": variant["trace_file"], "reasons": ["duplicate_parent_variant"]}
        for parent, variants in candidates.items()
        for variant in sorted(variants, key=lambda item: stable_rank(parent, item["trace_file"]))[1:]
    )
    reason_counts["duplicate_parent_variant"] += duplicate_variants

    write_jsonl(args.output_dir / "recovered_trace_train.jsonl", accepted)
    write_jsonl(args.output_dir / "recovered_trace_rejected.jsonl", rejected)
    report = {
        "input_rows": len(rows),
        "accepted_rows": len(accepted),
        "accepted_unique_parents": len({row["metadata"]["parent_query_id"] for row in accepted}),
        "rejected_rows": len(rejected),
        "rejection_reasons": dict(reason_counts.most_common()),
        "holdout_parent_ids": "769-797 inclusive",
        "policy": [
            "benchmark answer must exactly match ground truth",
            "one row per parent query",
            "parents in the current evaluation holdout are forbidden",
            "the gold answer must occur literally inside a real non-placeholder tool result",
            "manual review must confirm that the retained evidence establishes the relation asked about",
            "fabricated archive reasoning and final prose are discarded",
        ],
    }
    (args.output_dir / "recovered_trace_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
