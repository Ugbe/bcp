"""Parallel research rollouts with pooled evidence and a single scored record.

This is an opt-in treatment harness for roadmap section 4.3.  Each question is
researched independently several times, the raw evidence is pooled, and a
fresh tools-disabled synthesis is given a double vote.  Only the aggregate is
written as ``run_qid_<id>.json`` so the existing evaluator and dashboard see
one benchmark response.  The aggregate records every rollout and sums their
tool calls; no work is hidden from the accounting.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import openai
from dotenv import load_dotenv

from chat_client import (
    FRESH_FINAL_SYSTEM_PROMPT,
    ChatSearchToolHandler,
    _extract_confidence,
    _extract_final_answer,
    _has_valid_final_answer,
    _normalize_chat_messages,
    format_query,
    run_conversation_with_tools,
)
from searcher.searchers.remote_api_searcher import RemoteApiSearcher

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _answer_key(value: str | None) -> str:
    value = value or ""
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _raw_tool_outputs(messages: list[dict]) -> list[dict]:
    return [
        {
            "tool_call_id": item.get("tool_call_id"),
            "tool_name": item.get("_tool_name"),
            "arguments": item.get("_tool_arguments"),
            "output": item.get("_raw_tool_output"),
        }
        for item in messages
        if item.get("role") == "tool" and "_raw_tool_output" in item
    ]


def _pool_evidence(rollouts: list[dict], max_chars: int) -> tuple[str, list[dict]]:
    """Deduplicate compact evidence entries while retaining rollout provenance."""

    documents: dict[str, dict] = {}
    for rollout_index, rollout in enumerate(rollouts):
        for item in rollout.get("raw_tool_outputs", []):
            try:
                payload = json.loads(item.get("output") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            candidates = payload.get("documents", []) if isinstance(payload, dict) else payload
            if isinstance(payload, dict) and payload.get("docid") is not None:
                candidates = [payload]
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("docid") is None:
                    continue
                docid = str(candidate["docid"])
                entry = documents.setdefault(
                    docid,
                    {
                        "docid": docid,
                        "title": str(candidate.get("title") or "")[:400],
                        "preview": " ".join(
                            str(candidate.get("snippet") or candidate.get("text") or "").split()
                        )[:1200],
                        "rollouts": [],
                    },
                )
                if rollout_index not in entry["rollouts"]:
                    entry["rollouts"].append(rollout_index)

    entries = list(documents.values())
    lines = []
    remaining = max(1000, int(max_chars))
    for entry in entries:
        line = (
            f"[{entry['docid']}] {entry['title']}\n"
            f"Rollouts: {','.join(str(i + 1) for i in entry['rollouts'])}\n"
            f"Preview: {entry['preview']}"
        )
        if len(line) + 2 > remaining:
            break
        lines.append(line)
        remaining -= len(line) + 2
    return "\n\n".join(lines), entries[: len(lines)]


def _make_remote_args(args) -> SimpleNamespace:
    return SimpleNamespace(
        retrieval_url=args.retrieval_url,
        retrieval_token=args.retrieval_token,
        retrieval_timeout=args.retrieval_timeout,
        retrieval_retries=args.retrieval_retries,
        retrieval_retry_backoff=args.retrieval_retry_backoff,
    )


def _run_rollout(client, args, qtext: str, temperature: float, seed: int) -> dict:
    searcher = RemoteApiSearcher(_make_remote_args(args))
    handler = ChatSearchToolHandler(
        searcher=searcher,
        snippet_max_tokens=args.snippet_max_tokens,
        k=args.k,
        include_get_document=True,
        tool_name="search",
        tool_param="query",
        document_max_tokens=args.document_max_tokens,
        multi_query_search=True,
        deep_pool_search=True,
        deep_pool_k=args.deep_pool_k,
    )
    diagnostics: dict = {}
    messages, tool_usage, status = run_conversation_with_tools(
        client,
        args.model,
        [{"role": "user", "content": format_query(qtext, args.query_template)}],
        handler.get_chat_tool_definitions(),
        handler,
        max_iterations=args.max_iterations,
        max_tokens=args.max_tokens,
        temperature=temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        repetition_penalty=args.repetition_penalty,
        verbose=args.verbose,
        require_final_answer=True,
        max_tool_calls=args.max_tool_calls,
        early_final_guard=args.early_final_guard,
        early_final_min_remaining=args.early_final_min_remaining,
        early_final_max_recoveries=args.early_final_max_recoveries,
        stagnation_low_novelty_threshold=args.stagnation_low_novelty_threshold,
        stagnation_consecutive_searches=args.stagnation_consecutive_searches,
        retrieval_novelty=args.retrieval_novelty,
        retrieval_seen_anchor_count=args.retrieval_seen_anchor_count,
        emergency_finalizer=True,
        emergency_finalizer_max_tokens=args.emergency_finalizer_max_tokens,
        context_compaction=True,
        context_window_tokens=args.context_window_tokens,
        context_compaction_trigger_tokens=args.context_compaction_trigger_tokens,
        context_compaction_keep_tool_rounds=args.context_compaction_keep_tool_rounds,
        context_compaction_max_tokens=args.context_compaction_max_tokens,
        context_compaction_reserve_tokens=args.context_compaction_reserve_tokens,
        diagnostics_out=diagnostics,
        evidence_notes=True,
        evidence_note_max_tokens=args.evidence_note_max_tokens,
        fresh_final_answer=True,
        fresh_final_max_tokens=args.fresh_final_max_tokens,
        fresh_final_prompt_max_tokens=args.fresh_final_prompt_max_tokens,
        seed=seed,
    )
    normalized = _normalize_chat_messages(messages)
    final_text = next(
        (str(item.get("output") or "") for item in reversed(normalized) if item.get("type") == "output_text"),
        "",
    )
    return {
        "rollout_index": None,
        "seed": seed,
        "temperature": temperature,
        "status": status,
        "tool_call_counts": tool_usage,
        "final_text": final_text,
        "final_answer": _extract_final_answer(final_text),
        "confidence": _extract_confidence(final_text),
        "result": normalized,
        "raw_tool_outputs": _raw_tool_outputs(messages),
        "diagnostics": diagnostics,
    }


def _pooled_final(client, args, qtext: str, evidence: str, seed: int) -> dict:
    prompt = (
        f"QUESTION:\n{qtext}\n\n"
        "POOLED EVIDENCE FROM INDEPENDENT RESEARCH ROLLOUTS:\n"
        f"{evidence}\n\n"
        "Resolve conflicts using the evidence and return the benchmark contract."
    )
    try:
        response = client.chat.completions.create(
            model=args.model,
            messages=[
                {"role": "system", "content": FRESH_FINAL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            tools=[],
            max_tokens=args.pooled_final_max_tokens,
            temperature=0,
            tool_choice="none",
            extra_body={"chat_template_kwargs": {"enable_thinking": True}},
            seed=seed,
        )
        choice = response.choices[0]
        message = choice.message.model_dump(mode="python")
        text = str(message.get("content") or "").strip()
        return {
            "status": "completed" if choice.finish_reason == "stop" and _has_valid_final_answer(text) else "incomplete",
            "seed": seed,
            "final_text": text,
            "final_answer": _extract_final_answer(text),
            "confidence": _extract_confidence(text),
            "result": [{"type": "output_text", "tool_name": None, "arguments": None, "output": text}] if text else [],
        }
    except Exception as exc:
        return {"status": "incomplete_request_error", "seed": seed, "error": str(exc), "result": []}


def decide_ensemble(rollouts: list[dict], pooled: dict) -> dict:
    """Pick by weighted exact-answer vote, using pooled synthesis weight two."""

    candidates = []
    for rollout in rollouts:
        if rollout.get("status") == "completed" and rollout.get("final_answer"):
            candidates.append(
                {
                    "source": f"rollout_{rollout.get('rollout_index')}",
                    "answer": rollout["final_answer"],
                    "key": _answer_key(rollout["final_answer"]),
                    "weight": 1,
                    "confidence": rollout.get("confidence") or 0,
                }
            )
    if pooled.get("status") == "completed" and pooled.get("final_answer"):
        candidates.append(
            {
                "source": "pooled_final",
                "answer": pooled["final_answer"],
                "key": _answer_key(pooled["final_answer"]),
                "weight": 2,
                "confidence": pooled.get("confidence") or 0,
            }
        )
    if not candidates:
        return {"status": "incomplete_no_final_answer", "selected": None, "votes": {}}

    votes: dict[str, dict] = {}
    for candidate in candidates:
        vote = votes.setdefault(
            candidate["key"],
            {"answer": candidate["answer"], "weight": 0, "confidence": 0, "sources": []},
        )
        vote["weight"] += candidate["weight"]
        vote["confidence"] += float(candidate["confidence"] or 0) * candidate["weight"]
        vote["sources"].append(candidate["source"])
    selected_key = max(votes, key=lambda key: (votes[key]["weight"], votes[key]["confidence"]))
    return {"status": "completed", "selected": votes[selected_key], "votes": votes}


def _write_record(out_dir: Path, qid: str, record: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_qid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(qid))
    target = out_dir / f"run_qid_{safe_qid}.json"
    temporary = out_dir / f".{target.name}.{uuid.uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def run_question(client, args, qid: str, qtext: str) -> dict:
    temperatures = list(args.temperatures)
    seeds = [args.seed_base + i for i in range(args.rollouts)]
    if len(temperatures) < args.rollouts:
        temperatures.extend([temperatures[-1]] * (args.rollouts - len(temperatures)))
    temperatures = temperatures[: args.rollouts]

    with ThreadPoolExecutor(max_workers=min(args.parallel_rollouts, args.rollouts)) as executor:
        futures = [
            executor.submit(_run_rollout, client, args, qtext, temperatures[i], seeds[i])
            for i in range(args.rollouts)
        ]
        rollouts = [future.result() for future in futures]
    for index, rollout in enumerate(rollouts, start=1):
        rollout["rollout_index"] = index

    evidence, pool_entries = _pool_evidence(rollouts, args.pool_max_chars)
    pooled = _pooled_final(client, args, qtext, evidence, args.seed_base + 100000)
    decision = decide_ensemble(rollouts, pooled)
    selected_answer = (decision.get("selected") or {}).get("answer")
    selected_result = next(
        (rollout["result"] for rollout in rollouts if rollout.get("final_answer") == selected_answer),
        pooled.get("result", []),
    )
    if pooled.get("final_answer") == selected_answer:
        selected_result = pooled.get("result", [])

    tool_counts: dict[str, int] = {}
    for rollout in rollouts:
        for name, count in (rollout.get("tool_call_counts") or {}).items():
            tool_counts[name] = tool_counts.get(name, 0) + int(count or 0)
    retrieved = []
    for rollout in rollouts:
        for item in rollout.get("raw_tool_outputs", []):
            try:
                payload = json.loads(item.get("output") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            docs = payload.get("documents", []) if isinstance(payload, dict) else payload
            if isinstance(payload, dict) and payload.get("docid") is not None:
                docs = [payload]
            for doc in docs if isinstance(docs, list) else []:
                if isinstance(doc, dict) and doc.get("docid") is not None and str(doc["docid"]) not in retrieved:
                    retrieved.append(str(doc["docid"]))

    return {
        "metadata": {
            "model": args.model,
            "client": "research_ensemble",
            "output_dir": str(args.output_dir),
            "ensemble_rollouts": args.rollouts,
        },
        "query_id": qid,
        "tool_call_counts": tool_counts,
        "status": decision["status"],
        "retrieved_docids": retrieved,
        "result": selected_result,
        "diagnostics": {
            "ensemble": {
                "rollouts": rollouts,
                "pooled_final": pooled,
                "decision": decision,
                "pooled_evidence_documents": pool_entries,
                "pooled_evidence_characters": len(evidence),
                "total_tool_calls": sum(tool_counts.values()),
            }
        },
        "raw_tool_outputs": [item for rollout in rollouts for item in rollout.get("raw_tool_outputs", [])],
    }


def _parse_temperatures(value: str) -> list[float]:
    parsed = [float(item.strip()) for item in value.split(",") if item.strip()]
    if not parsed or any(item < 0 for item in parsed):
        raise argparse.ArgumentTypeError("temperatures must be a non-empty CSV of non-negative numbers")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run independent research rollouts, pool evidence, and write one scored record per question.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--query", default="topics-qrels/queries.tsv", help="Question text or TSV of qid<TAB>question")
    parser.add_argument("--model", default=os.environ.get("MODEL_NAME", "CrowtherLabs/Qwythos-9B-Analyst"))
    parser.add_argument("--model-url", default=os.environ.get("MODEL_BASE_URL", "http://127.0.0.1:10100/v1"))
    parser.add_argument("--model-api-key", default=os.environ.get("MODEL_API_KEY", "EMPTY"))
    parser.add_argument("--output-dir", default="runs/research-ensemble")
    parser.add_argument("--rollouts", type=int, default=4)
    parser.add_argument("--temperatures", type=_parse_temperatures, default=_parse_temperatures("0.2,0.6,0.6,0.8"))
    parser.add_argument("--seed-base", type=int, default=4100)
    parser.add_argument("--parallel-rollouts", type=int, default=4)
    parser.add_argument("--num-threads", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=12000)
    parser.add_argument("--pooled-final-max-tokens", type=int, default=1536)
    parser.add_argument("--max-iterations", type=int, default=64)
    parser.add_argument("--max-tool-calls", type=int, default=32)
    parser.add_argument("--temperature", type=float, default=0.6, help="Unused compatibility option; use --temperatures")
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--repetition-penalty", type=float, default=1.05)
    parser.add_argument("--query-template", default="QUERY_TEMPLATE_RESEARCH_LEDGER")
    parser.add_argument("--snippet-max-tokens", type=int, default=512)
    parser.add_argument("--document-max-tokens", type=int, default=4096)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--deep-pool-k", type=int, default=100)
    parser.add_argument("--pool-max-chars", type=int, default=60000)
    parser.add_argument("--retrieval-url", default=os.environ.get("BCP_RETRIEVAL_URL", "http://127.0.0.1:17070"))
    parser.add_argument("--retrieval-token", default=os.environ.get("BCP_TOKEN"))
    parser.add_argument("--retrieval-timeout", type=float, default=60.0)
    parser.add_argument("--retrieval-retries", type=int, default=3)
    parser.add_argument("--retrieval-retry-backoff", type=float, default=5.0)
    parser.add_argument("--retrieval-novelty", choices=["off", "local", "server"], default="server")
    parser.add_argument("--early-final-guard", choices=["off", "refusal", "conservative"], default="conservative")
    parser.add_argument("--early-final-min-remaining", type=int, default=4)
    parser.add_argument("--early-final-max-recoveries", type=int, default=1)
    parser.add_argument("--stagnation-low-novelty-threshold", type=int, default=3)
    parser.add_argument("--stagnation-consecutive-searches", type=int, default=2)
    parser.add_argument("--retrieval-seen-anchor-count", type=int, default=2)
    parser.add_argument("--evidence-note-max-tokens", type=int, default=700)
    parser.add_argument("--fresh-final-max-tokens", type=int, default=1024)
    parser.add_argument("--fresh-final-prompt-max-tokens", type=int, default=24000)
    parser.add_argument("--emergency-finalizer-max-tokens", type=int, default=384)
    parser.add_argument("--context-window-tokens", type=int, default=131072)
    parser.add_argument("--context-compaction-trigger-tokens", type=int, default=0)
    parser.add_argument("--context-compaction-keep-tool-rounds", type=int, default=2)
    parser.add_argument("--context-compaction-max-tokens", type=int, default=1536)
    parser.add_argument("--context-compaction-reserve-tokens", type=int, default=8192)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.rollouts < 1 or args.parallel_rollouts < 1 or args.num_threads < 1:
        parser.error("rollouts, parallel-rollouts, and num-threads must be positive")
    args.output_dir = Path(args.output_dir).expanduser().resolve()
    client = openai.OpenAI(base_url=args.model_url, api_key=args.model_api_key)

    if Path(args.query).is_file() and Path(args.query).suffix.lower() == ".tsv":
        with Path(args.query).open(newline="", encoding="utf-8") as handle:
            queries = [(row[0].strip(), row[1].strip()) for row in csv.reader(handle, delimiter="\t") if len(row) >= 2]
    else:
        queries = [("single", args.query)]
    remaining = []
    for qid, qtext in queries:
        target = args.output_dir / f"run_qid_{re.sub(r'[^A-Za-z0-9_.-]', '_', qid)}.json"
        if target.exists():
            try:
                if json.loads(target.read_text(encoding="utf-8")).get("status") == "completed":
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        remaining.append((qid, qtext))
    print(f"Processing {len(remaining)} questions; skipping {len(queries) - len(remaining)} completed ensemble records")

    def process(item):
        qid, qtext = item
        record = run_question(client, args, qid, qtext)
        _write_record(args.output_dir, qid, record)
        return qid, record["status"]

    with ThreadPoolExecutor(max_workers=args.num_threads) as executor:
        for future in as_completed([executor.submit(process, item) for item in remaining]):
            qid, status = future.result()
            print(f"qid={qid} status={status}")


if __name__ == "__main__":
    main()
