"""Reproducible forensic audit for the 830-query Atom Electron run.

This script is intentionally read-only with respect to run/evaluation data.  It
writes derived CSV/JSON artifacts into analysis/atom-electron-1.3-9b-830-audit.
"""

from __future__ import annotations

import csv
import difflib
import glob
import json
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = ROOT / "runs" / "atom-electron-1.3-9b-full-830-docs-v2-128k"
EVAL_DIR = ROOT / "evals" / "atom-electron-1.3-9b-full-830-docs-v2-128k"
OUTPUT_DIR = ROOT / "analysis" / "atom-electron-1.3-9b-830-audit"
QUERY_FILE = ROOT / "topics-qrels" / "queries.tsv"

# Manual semantic review of every automatically surfaced near-match.  These
# labels distinguish judge mistakes from genuinely different answers; they are
# deliberately conservative and are explained in the accompanying report.
HIGH_CONFIDENCE_JUDGE_ERRORS = {
    "192", "216", "394", "443", "445", "516", "542", "607", "639",
    "651", "652", "716", "791", "820", "941", "991", "1005", "1138",
    "1150", "1155", "1211",
}
PROBABLE_OR_POLICY_DEPENDENT_JUDGE_ERRORS = {
    "275", "278", "283", "406", "422", "519", "532", "577", "709",
    "749", "952", "1079",
}
MANUAL_REVIEW_NOTES = {
    "192": "Same person; honorific omission and Se-hoon/Se Hoon punctuation are non-semantic.",
    "216": "Same person; adding the correct middle initial does not change identity.",
    "219": "Different title wording: 'for' versus 'of'. Rejection is justified.",
    "275": "Same corporate lineage after a 1957 rename; acceptance depends on whether the historical founding name is required.",
    "278": "MediaLab is a natural short form of MediaLab Group; likely the same company.",
    "283": "Retrieved full document names the protagonist Zimri Eder while the gold says Elder; likely a gold/source inconsistency.",
    "394": "Wing is the modern/common synonym for rugby wing three-quarter.",
    "406": "Retrieved evidence identifies Masato Kato; the gold's Masata appears to be a typo.",
    "422": "Wisconsin 38 uniquely names the requested Nicotiana tabacum variety; genus/species omission is not identity-changing.",
    "425": "Graham alone is not the requested complete dessert topping. Rejection is justified.",
    "443": "Prediction contains the exact product name plus brand/category descriptors.",
    "445": "Adding 'model' completes the name of the statistical model; same answer.",
    "509": "Retrieved evidence spells the mineral Sacrofanite; Sacrofanoite is a real spelling error.",
    "514": "Question explicitly asks for the full name; Maximilian is missing. Rejection is justified.",
    "516": "Prediction is the same school with a disambiguating location.",
    "519": "Glafcos/Glafkos is a common transliteration variant; likely the same book title.",
    "532": "The cited player represented Hartlepools United under that historical name; gold uses the modern singular form.",
    "542": "Same person; adding the middle name does not change identity.",
    "577": "DN AGRAR is the same reporting group, but the legal/group suffix is omitted.",
    "579": "Different day (March 9 versus March 5). Rejection is justified.",
    "593": "Question asks for a date; day is missing. Rejection is justified.",
    "607": "1lb and 1lbs express the same weight; plural grammar is non-semantic.",
    "625": "Episode number is wrong (7 versus 6). Rejection is justified.",
    "639": "Capitalization of 'delos' does not change the person's identity.",
    "651": "Corporate suffix ', Inc.' does not change the company identity.",
    "652": "Full legal name and short brand/company name refer to the same entity.",
    "709": "Surname-only answer is unambiguous in the response, but the question asks which footballer; policy-dependent completeness.",
    "716": "Title differs only in capitalization.",
    "749": "Joe is a standard short form of Joseph; likely the same individual.",
    "791": "Same band; capitalization only. A prior judge accepted the same normalized answer.",
    "820": "Omitting Pvt. Ltd. does not change the identified employer, though the question asks for the company name as of a date.",
    "826": "Different book-title preposition ('in' versus 'at'). Rejection is justified.",
    "941": "Singular/plural form names the same insect.",
    "952": "'Some' is an indefinite determiner; the substantive recommendation is the same John Talabot session.",
    "991": "Gold text is mojibake; Toke Makinwa is the same name/person.",
    "1005": "Middle initial K. correctly abbreviates Karl; same scientist.",
    "1061": "Prediction is the book title, not the requested documentary title. Rejection is justified.",
    "1079": "Uche Jombo is the same director's professional name; Rodriguez is an omitted married surname.",
    "1138": "Legal suffix S.A. does not change company identity.",
    "1150": "Math and Mathematics are synonymous fields.",
    "1155": "Giusewa Pueblo is the same ancient village with a descriptive noun added.",
    "1211": "Spacing-only difference in the same series title.",
}

# Heuristic used to surface *possible* premature finals for manual review.  It
# is intentionally broader than the refusal detector: these phrases often
# appear when the model still supplies a candidate answer, despite admitting
# that evidence or one of the question's constraints remains unresolved.
EVIDENCE_GAP_RE = re.compile(
    r"(?:"
    r"(?:could(?:\s+not|n't)|was\s+unable\s+to|have\s+not|haven't|did\s+not|didn't|"
    r"cannot|can't|unable\s+to|not\s+able\s+to)\s+"
    r"(?:find|locate|verify|confirm|identify|establish|determine|retrieve|access|validate)"
    r"|(?:insufficient|incomplete|limited|not\s+enough)\s+"
    r"(?:information|evidence|details|documentation|data|sources|results)"
    r"|(?:do\s+not|don't|did\s+not|didn't)\s+have\s+(?:enough|sufficient|complete)\s+"
    r"(?:information|evidence|details|documentation|data|sources|results)"
    r"|not\s+(?:fully|definitively|directly)\s+(?:verified|confirmed|established|supported)"
    r"|(?:cannot|can't|unable\s+to)\s+(?:definitively|fully|reliably|conclusively)\s+answer"
    r")",
    re.IGNORECASE,
)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def scalar_normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def parse_tool_output(raw: object) -> object | None:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def answer_text(run: dict) -> str:
    items = run.get("result") or []
    if items and items[-1].get("type") == "output_text":
        return str(items[-1].get("output") or "")
    return ""


def last_reasoning_text(run: dict) -> str:
    """Return the final reasoning event immediately preceding the final text."""
    for item in reversed(run.get("result") or []):
        if item.get("type") != "reasoning":
            continue
        output = item.get("output")
        if isinstance(output, list):
            return "\n".join(str(part) for part in output).strip()
        return str(output or "").strip()
    return ""


def mean(rows: list[dict], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return statistics.fmean(values) if values else None


def pct(numerator: int | float, denominator: int | float) -> float | None:
    return round(100.0 * numerator / denominator, 4) if denominator else None


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    query_rows = list(csv.reader(QUERY_FILE.open(encoding="utf-8"), delimiter="\t"))
    query_ids = [row[0].strip() for row in query_rows if len(row) >= 2]

    runs = {
        str(data["query_id"]): data
        for path in RUN_DIR.glob("run_qid_*.json")
        if (data := load_json(path))
    }
    evals = {
        str(data["query_id"]): data
        for path in EVAL_DIR.glob("run_qid_*_eval.json")
        if (data := load_json(path))
    }

    previous_evals: dict[str, list[dict]] = defaultdict(list)
    for path_text in glob.glob(str(ROOT / "evals" / "**" / "*_eval.json"), recursive=True):
        path = Path(path_text)
        if path.parent == EVAL_DIR:
            continue
        try:
            data = load_json(path)
        except Exception:
            continue
        qid = data.get("query_id")
        if qid is not None:
            previous_evals[str(qid)].append(data)

    per_run: list[dict] = []
    overlap_calls: list[dict] = []
    incomplete: list[dict] = []
    judge_candidates: list[dict] = []
    early_stop_review: list[dict] = []

    for qid in query_ids:
        run = runs[qid]
        evaluation = evals[qid]
        judge = evaluation.get("judge_result") or {}
        correct = judge.get("correct") is True
        completed = run.get("status") == "completed"
        tool_counts = run.get("tool_call_counts") or {}
        total_tool_calls = sum(int(value or 0) for value in tool_counts.values())

        seen_docs: set[str] = set()
        result_slots = 0
        repeated_slots = 0
        actual_searches = 0
        duplicate_search_calls = 0
        unparsable_search_outputs = 0
        no_new_doc_searches = 0
        low_novelty_searches = 0
        high_overlap_searches = 0
        exact_reasoning_counts: Counter[str] = Counter()

        for item in run.get("result") or []:
            if item.get("type") == "reasoning":
                output = item.get("output")
                if isinstance(output, list):
                    reasoning = "\n".join(str(part) for part in output).strip()
                else:
                    reasoning = str(output or "").strip()
                if reasoning:
                    exact_reasoning_counts[reasoning] += 1

            if item.get("type") != "tool_call" or item.get("tool_name") != "search":
                continue
            parsed = parse_tool_output(item.get("output"))
            retrieval_state = {}
            try:
                arguments = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            query = arguments.get("query") or arguments.get("user_query") or ""

            if isinstance(parsed, dict) and parsed.get("duplicate_search"):
                duplicate_search_calls += 1
                overlap_calls.append(
                    {
                        "query_id": qid,
                        "search_index": actual_searches + duplicate_search_calls,
                        "search_query": query,
                        "kind": "exact_duplicate_query_blocked",
                        "returned_docs": 0,
                        "new_docs": 0,
                        "repeated_docs": 0,
                        "overlap_percent": None,
                        "exclusions_applied": None,
                        "fallback_applied": None,
                        "candidate_pool_exhausted": None,
                    }
                )
                continue
            if isinstance(parsed, dict):
                retrieval_state = dict(parsed.get("retrieval_state") or {})
                wrapped_documents = None
                for key in ("documents", "results", "result"):
                    if isinstance(parsed.get(key), list):
                        wrapped_documents = parsed[key]
                        break
                parsed = wrapped_documents
            if not isinstance(parsed, list):
                unparsable_search_outputs += 1
                continue

            actual_searches += 1
            docids = [str(hit.get("docid")) for hit in parsed if isinstance(hit, dict) and hit.get("docid") is not None]
            new_docs = [docid for docid in docids if docid not in seen_docs]
            repeated_docs = len(docids) - len(new_docs)
            result_slots += len(docids)
            repeated_slots += repeated_docs
            if not new_docs:
                no_new_doc_searches += 1
            if len(new_docs) <= 3:
                low_novelty_searches += 1
            if len(docids) and repeated_docs >= 7:
                high_overlap_searches += 1
            overlap_calls.append(
                {
                    "query_id": qid,
                    "search_index": actual_searches + duplicate_search_calls,
                    "search_query": query,
                    "kind": "retrieval_results",
                    "returned_docs": len(docids),
                    "new_docs": len(new_docs),
                    "repeated_docs": repeated_docs,
                    "overlap_percent": round(100.0 * repeated_docs / len(docids), 4) if docids else None,
                    "exclusions_applied": retrieval_state.get("exclusions_applied"),
                    "fallback_applied": retrieval_state.get("fallback_applied"),
                    "candidate_pool_exhausted": retrieval_state.get(
                        "candidate_pool_exhausted"
                    ),
                }
            )
            seen_docs.update(docids)

        repeated_reasoning_events = sum(count - 1 for count in exact_reasoning_counts.values() if count > 1)
        max_reasoning_repeat = max(exact_reasoning_counts.values(), default=0)
        response = answer_text(run)
        final_reasoning = last_reasoning_text(run)
        gold = str(evaluation.get("correct_answer") or "")
        extracted = str(judge.get("extracted_final_answer") or "")
        normalized_gold = scalar_normalize(gold)
        normalized_extracted = scalar_normalize(extracted)
        gold_in_tool_output = False
        extracted_in_tool_output = False
        for item in run.get("result") or []:
            if item.get("type") != "tool_call":
                continue
            normalized_output = scalar_normalize(item.get("output"))
            if len(normalized_gold) >= 5 and normalized_gold in normalized_output:
                gold_in_tool_output = True
            if len(normalized_extracted) >= 5 and normalized_extracted in normalized_output:
                extracted_in_tool_output = True
        # The judge writes the literal string "None" when the model's Exact
        # Answer is a refusal. Inspect the extracted answer rather than the
        # explanation, which may mention one unresolved clue before still
        # giving a valid final answer.
        refusal = normalized_extracted in {
            "none",
            "cannotdetermine",
            "cannotdeterminewithconfidence",
            "unabletodetermine",
            "unabletodeterminewithconfidence",
            "insufficientinformation",
            "unknown",
        }
        evidence_gap_match = EVIDENCE_GAP_RE.search(final_reasoning + "\n" + response)
        early_completed = completed and total_tool_calls < 24
        caveated_candidate = bool(early_completed and not refusal and evidence_gap_match)
        recall = evaluation.get("retrieval", {}).get("recall")
        cited_metrics = (evaluation.get("citations") or {}).get("metrics") or {}
        diagnostics = run.get("diagnostics") or {}
        row = {
            "query_id": qid,
            "status": run.get("status"),
            "is_completed": completed,
            "judge_correct": judge.get("correct"),
            "judge_parse_error": judge.get("parse_error"),
            "search_calls": int(tool_counts.get("search", 0) or 0),
            "get_document_calls": int(tool_counts.get("get_document", 0) or 0),
            "total_tool_calls": total_tool_calls,
            "budget_exhausted": total_tool_calls >= 24,
            "actual_retrieval_searches": actual_searches,
            "blocked_exact_duplicate_searches": duplicate_search_calls,
            "unparsable_search_outputs": unparsable_search_outputs,
            "result_slots": result_slots,
            "unique_docids_seen": len(seen_docs),
            "repeated_result_slots": repeated_slots,
            "repeated_result_slot_percent": pct(repeated_slots, result_slots),
            "zero_novelty_searches": no_new_doc_searches,
            "low_novelty_searches_le_3_new": low_novelty_searches,
            "high_overlap_searches_ge_7_repeats": high_overlap_searches,
            "repeated_reasoning_events": repeated_reasoning_events,
            "max_exact_reasoning_repeat": max_reasoning_repeat,
            "retrieval_recall": recall,
            "citation_precision": cited_metrics.get("precision"),
            "citation_recall": cited_metrics.get("recall"),
            "explicit_cannot_determine": refusal,
            "early_completed_before_budget": early_completed,
            "final_evidence_gap_signal": bool(evidence_gap_match),
            "caveated_candidate_before_budget": caveated_candidate,
            "productive_tool_calls": int(
                diagnostics.get("productive_tool_calls", total_tool_calls) or 0
            ),
            "rejected_duplicate_calls": int(
                diagnostics.get("rejected_duplicate_calls", duplicate_search_calls)
                or 0
            ),
            "early_final_guard_triggered": bool(
                diagnostics.get("early_final_guard_triggered", False)
            ),
            "early_final_recoveries": int(
                diagnostics.get("early_final_recoveries", 0) or 0
            ),
            "emergency_finalizer_attempted": bool(
                diagnostics.get("emergency_finalizer_attempted", False)
            ),
            "emergency_finalizer_succeeded": bool(
                diagnostics.get("emergency_finalizer_succeeded", False)
            ),
            "retrieval_exclusions_applied": bool(
                diagnostics.get("retrieval_exclusions_applied", False)
            ),
            "retrieval_fallback_applied": bool(
                diagnostics.get("retrieval_fallback_applied", False)
            ),
            "stagnation_guidance_count": int(
                diagnostics.get("stagnation_guidance_count", 0) or 0
            ),
            "model_confidence": judge.get("confidence"),
            "gold_answer_literal_found_in_tool_output": gold_in_tool_output,
            "extracted_answer_literal_found_in_tool_output": extracted_in_tool_output,
            "correct_answer": gold,
            "extracted_answer": judge.get("extracted_final_answer"),
            "run_file": str(RUN_DIR / f"run_qid_{qid}.json"),
            "eval_file": str(EVAL_DIR / f"run_qid_{qid}_eval.json"),
        }
        per_run.append(row)

        if early_completed and (refusal or evidence_gap_match):
            matched_text = evidence_gap_match.group(0) if evidence_gap_match else ""
            early_stop_review.append(
                {
                    "query_id": qid,
                    "category": "explicit_no_answer" if refusal else "caveated_candidate",
                    "judge_correct": judge.get("correct"),
                    "search_calls": row["search_calls"],
                    "get_document_calls": row["get_document_calls"],
                    "total_tool_calls": total_tool_calls,
                    "remaining_tool_budget": 24 - total_tool_calls,
                    "retrieval_recall": recall,
                    "model_confidence": judge.get("confidence"),
                    "matched_evidence_gap_phrase": matched_text,
                    "correct_answer": gold,
                    "extracted_answer": judge.get("extracted_final_answer"),
                    "final_reasoning_preview": re.sub(r"\s+", " ", final_reasoning)[:1000],
                    "final_response_preview": re.sub(r"\s+", " ", response)[:1000],
                    "run_file": row["run_file"],
                }
            )

        if not completed:
            items = run.get("result") or []
            last = items[-1] if items else {}
            last_output = str(last.get("output") or "")
            incomplete.append(
                {
                    "query_id": qid,
                    "status": run.get("status"),
                    "search_calls": row["search_calls"],
                    "get_document_calls": row["get_document_calls"],
                    "total_tool_calls": total_tool_calls,
                    "blocked_exact_duplicate_searches": duplicate_search_calls,
                    "last_result_type": last.get("type"),
                    "last_output_preview": re.sub(r"\s+", " ", last_output)[:500],
                    "run_file": row["run_file"],
                }
            )

        if completed and judge.get("correct") is False and judge.get("extracted_final_answer"):
            extracted = str(judge["extracted_final_answer"])
            similarity = difflib.SequenceMatcher(None, normalized_extracted, normalized_gold).ratio()
            substring = bool(
                normalized_extracted
                and normalized_gold
                and (normalized_extracted in normalized_gold or normalized_gold in normalized_extracted)
            )
            prior_true_same_normalized = []
            for previous in previous_evals.get(qid, []):
                previous_judge = previous.get("judge_result") or {}
                if previous_judge.get("correct") is True and scalar_normalize(previous_judge.get("extracted_final_answer")) == normalized_extracted:
                    prior_true_same_normalized.append(previous.get("json_path") or "unknown")
            if normalized_extracted == normalized_gold or similarity >= 0.78 or substring or prior_true_same_normalized:
                judge_candidates.append(
                    {
                        "query_id": qid,
                        "correct_answer": gold,
                        "extracted_answer": extracted,
                        "normalized_equal": normalized_extracted == normalized_gold,
                        "similarity": round(similarity, 4),
                        "one_contains_other": substring,
                        "prior_judge_marked_same_answer_correct": bool(prior_true_same_normalized),
                        "prior_true_paths": " | ".join(prior_true_same_normalized),
                        "judge_reasoning": judge.get("reasoning"),
                        "question": evaluation.get("question"),
                        "run_file": row["run_file"],
                        "eval_file": row["eval_file"],
                    }
                )

    write_csv(OUTPUT_DIR / "per_run_metrics.csv", per_run)
    write_csv(OUTPUT_DIR / "retrieval_overlap_by_search.csv", overlap_calls)
    write_csv(OUTPUT_DIR / "incomplete_runs.csv", incomplete)
    write_csv(OUTPUT_DIR / "judge_equivalence_candidates.csv", judge_candidates)
    manual_judge_review = []
    for row in sorted(judge_candidates, key=lambda item: int(item["query_id"])):
        qid = row["query_id"]
        if qid in HIGH_CONFIDENCE_JUDGE_ERRORS:
            classification = "high_confidence_judge_error"
        elif qid in PROBABLE_OR_POLICY_DEPENDENT_JUDGE_ERRORS:
            classification = "probable_or_policy_dependent"
        else:
            classification = "judge_rejection_justified"
        manual_judge_review.append(
            {
                "query_id": qid,
                "classification": classification,
                "correct_answer": row["correct_answer"],
                "extracted_answer": row["extracted_answer"],
                "review_note": MANUAL_REVIEW_NOTES.get(qid, ""),
                "prior_judge_marked_same_answer_correct": row["prior_judge_marked_same_answer_correct"],
                "run_file": row["run_file"],
                "eval_file": row["eval_file"],
            }
        )
    write_csv(OUTPUT_DIR / "manual_judge_review.csv", manual_judge_review)
    write_csv(OUTPUT_DIR / "early_stop_review.csv", early_stop_review)

    budget_all = [row for row in per_run if row["budget_exhausted"]]
    budget_failures = [row for row in budget_all if row["judge_correct"] is not True]
    write_csv(OUTPUT_DIR / "budget_exhausted_all.csv", budget_all)
    write_csv(OUTPUT_DIR / "budget_exhausted_not_correct.csv", budget_failures)

    completed = [row for row in per_run if row["is_completed"]]
    correct = [row for row in per_run if row["judge_correct"] is True]
    not_correct = [row for row in per_run if row["judge_correct"] is not True]
    completed_wrong = [row for row in completed if row["judge_correct"] is False]
    parse_errors = [row for row in per_run if row["judge_parse_error"] is True]
    early_completed = [row for row in per_run if row["early_completed_before_budget"]]
    early_refusals = [row for row in early_completed if row["explicit_cannot_determine"]]
    early_caveated_candidates = [row for row in early_completed if row["caveated_candidate_before_budget"]]

    def cohort(label: str, rows: list[dict]) -> dict:
        return {
            "label": label,
            "count": len(rows),
            "accuracy_percent": pct(sum(row["judge_correct"] is True for row in rows), len(rows)),
            "avg_total_tool_calls": mean(rows, "total_tool_calls"),
            "avg_search_calls": mean(rows, "search_calls"),
            "avg_get_document_calls": mean(rows, "get_document_calls"),
            "avg_unique_docids_seen": mean(rows, "unique_docids_seen"),
            "avg_repeated_result_slot_percent": mean(rows, "repeated_result_slot_percent"),
            "avg_retrieval_recall_percent": 100.0 * mean(rows, "retrieval_recall") if mean(rows, "retrieval_recall") is not None else None,
        }

    recall_buckets = []
    for label, predicate in [
        ("0%", lambda value: value == 0),
        (">0-<50%", lambda value: 0 < value < 0.5),
        ("50-<100%", lambda value: 0.5 <= value < 1),
        ("100%", lambda value: value == 1),
    ]:
        rows = [row for row in per_run if row["retrieval_recall"] is not None and predicate(float(row["retrieval_recall"]))]
        recall_buckets.append(cohort(label, rows))

    tool_buckets = []
    for label, low, high in [("0-4", 0, 4), ("5-9", 5, 9), ("10-14", 10, 14), ("15-19", 15, 19), ("20-23", 20, 23), ("24", 24, 24)]:
        rows = [row for row in per_run if low <= row["total_tool_calls"] <= high]
        tool_buckets.append(cohort(label, rows))

    actual_result_calls = [row for row in overlap_calls if row["kind"] == "retrieval_results"]
    summary = {
        "source": {
            "query_rows": len(query_ids),
            "query_id_min": min(map(int, query_ids)),
            "query_id_max": max(map(int, query_ids)),
            "unique_query_ids": len(set(query_ids)),
            "active_run_records": len(runs),
            "active_eval_records": len(evals),
            "eval_json_files_including_summary": len(list(EVAL_DIR.glob("*.json"))),
            "recursive_run_record_files_all_runs_and_quarantines": len(glob.glob(str(ROOT / "runs" / "**" / "run_qid_*.json"), recursive=True)),
        },
        "outcomes": {
            "status_counts": dict(Counter(row["status"] for row in per_run)),
            "correct": len(correct),
            "completed_wrong_including_no_judge_parse_errors": len(completed_wrong),
            "not_correct_including_incomplete": len(not_correct),
            "accuracy_all_830_percent": pct(len(correct), len(per_run)),
            "accuracy_completed_only_percent": pct(len(correct), len(completed)),
            "judge_parse_error_flag_count": len(parse_errors),
            "judge_parse_errors_on_completed_runs": sum(row["judge_parse_error"] is True and row["is_completed"] for row in per_run),
        },
        "tool_budget": {
            "runs_at_24_calls": len(budget_all),
            "correct_at_24": sum(row["judge_correct"] is True for row in budget_all),
            "not_correct_at_24": len(budget_failures),
            "incomplete_at_24": sum(not row["is_completed"] for row in budget_all),
            "explicit_cannot_determine_at_24": sum(row["explicit_cannot_determine"] for row in budget_all),
            "blocked_duplicate_search_calls_at_24": sum(row["blocked_exact_duplicate_searches"] for row in budget_all),
        },
        "early_stopping": {
            "completed_before_24_calls": len(early_completed),
            "correct_before_24_calls": sum(row["judge_correct"] is True for row in early_completed),
            "explicit_no_answer_before_24_calls": len(early_refusals),
            "caveated_candidate_before_24_calls": len(early_caveated_candidates),
            "caveated_candidate_correct": sum(row["judge_correct"] is True for row in early_caveated_candidates),
            "caveated_candidate_wrong": sum(row["judge_correct"] is False for row in early_caveated_candidates),
            "avg_calls_explicit_no_answer": mean(early_refusals, "total_tool_calls"),
            "avg_calls_caveated_candidate": mean(early_caveated_candidates, "total_tool_calls"),
            "remaining_budget_explicit_no_answer": sum(24 - row["total_tool_calls"] for row in early_refusals),
            "remaining_budget_caveated_candidate": sum(24 - row["total_tool_calls"] for row in early_caveated_candidates),
        },
        "retrieval_overlap": {
            "actual_search_result_calls": len(actual_result_calls),
            "total_result_slots": sum(row["returned_docs"] for row in actual_result_calls),
            "repeated_result_slots": sum(row["repeated_docs"] for row in actual_result_calls),
            "repeated_result_slot_percent": pct(sum(row["repeated_docs"] for row in actual_result_calls), sum(row["returned_docs"] for row in actual_result_calls)),
            "blocked_exact_duplicate_query_calls": sum(row["blocked_exact_duplicate_searches"] for row in per_run),
            "calls_with_zero_new_docs": sum(row["new_docs"] == 0 for row in actual_result_calls),
            "calls_with_at_most_3_new_docs": sum(row["new_docs"] <= 3 for row in actual_result_calls),
            "calls_with_at_least_7_repeated_docs": sum(row["repeated_docs"] >= 7 for row in actual_result_calls),
            "runs_with_any_repeated_result": sum(row["repeated_result_slots"] > 0 for row in per_run),
            "runs_with_high_overlap_call": sum(row["high_overlap_searches_ge_7_repeats"] > 0 for row in per_run),
        },
        "model_loops": {
            "runs_with_blocked_exact_duplicate_search": sum(row["blocked_exact_duplicate_searches"] > 0 for row in per_run),
            "runs_with_exact_repeated_reasoning": sum(row["repeated_reasoning_events"] > 0 for row in per_run),
            "total_blocked_exact_duplicate_search_calls": sum(row["blocked_exact_duplicate_searches"] for row in per_run),
            "total_repeated_reasoning_events": sum(row["repeated_reasoning_events"] for row in per_run),
        },
        "controller_diagnostics": {
            "runs_with_early_final_guard_trigger": sum(
                row["early_final_guard_triggered"] for row in per_run
            ),
            "total_early_final_recoveries": sum(
                row["early_final_recoveries"] for row in per_run
            ),
            "emergency_finalizer_attempts": sum(
                row["emergency_finalizer_attempted"] for row in per_run
            ),
            "emergency_finalizer_successes": sum(
                row["emergency_finalizer_succeeded"] for row in per_run
            ),
            "runs_with_server_exclusions": sum(
                row["retrieval_exclusions_applied"] for row in per_run
            ),
            "runs_with_retrieval_fallback": sum(
                row["retrieval_fallback_applied"] for row in per_run
            ),
            "total_stagnation_guidance_events": sum(
                row["stagnation_guidance_count"] for row in per_run
            ),
        },
        "judge_equivalence_candidate_count": len(judge_candidates),
        "manual_judge_review": {
            "high_confidence_judge_errors": len(HIGH_CONFIDENCE_JUDGE_ERRORS),
            "probable_or_policy_dependent": len(PROBABLE_OR_POLICY_DEPENDENT_JUDGE_ERRORS),
            "judge_rejection_justified": len(judge_candidates) - len(HIGH_CONFIDENCE_JUDGE_ERRORS) - len(PROBABLE_OR_POLICY_DEPENDENT_JUDGE_ERRORS),
            "accuracy_if_high_confidence_errors_corrected_percent": pct(len(correct) + len(HIGH_CONFIDENCE_JUDGE_ERRORS), len(per_run)),
            "accuracy_if_all_probable_cases_also_accepted_percent": pct(len(correct) + len(HIGH_CONFIDENCE_JUDGE_ERRORS) + len(PROBABLE_OR_POLICY_DEPENDENT_JUDGE_ERRORS), len(per_run)),
        },
        "evidence_and_confidence": {
            "completed_wrong_with_recall_at_least_50_percent": sum(row["is_completed"] and row["judge_correct"] is False and float(row["retrieval_recall"] or 0) >= 0.5 for row in per_run),
            "completed_wrong_with_100_percent_recall": sum(row["is_completed"] and row["judge_correct"] is False and float(row["retrieval_recall"] or 0) == 1.0 for row in per_run),
            "completed_wrong_with_gold_literal_in_tool_output": sum(row["is_completed"] and row["judge_correct"] is False and row["gold_answer_literal_found_in_tool_output"] for row in per_run),
            "completed_wrong_at_confidence_ge_80": sum(row["is_completed"] and row["judge_correct"] is False and float(row["model_confidence"] or 0) >= 80 for row in per_run),
            "completed_wrong_at_confidence_ge_90": sum(row["is_completed"] and row["judge_correct"] is False and float(row["model_confidence"] or 0) >= 90 for row in per_run),
            "completed_wrong_at_confidence_100": sum(row["is_completed"] and row["judge_correct"] is False and float(row["model_confidence"] or 0) == 100 for row in per_run),
            "explicit_cannot_determine_all_runs": sum(row["explicit_cannot_determine"] for row in per_run),
        },
        "cohorts": [
            cohort("all", per_run),
            cohort("completed", completed),
            cohort("correct", correct),
            cohort("completed_wrong", completed_wrong),
            cohort("not_correct_including_incomplete", not_correct),
            cohort("used_get_document", [row for row in per_run if row["get_document_calls"] > 0]),
            cohort("never_used_get_document", [row for row in per_run if row["get_document_calls"] == 0]),
            cohort("budget_24", budget_all),
            cohort("not_budget_24", [row for row in per_run if not row["budget_exhausted"]]),
            cohort("has_retrieval_overlap", [row for row in per_run if row["repeated_result_slots"] > 0]),
            cohort("no_retrieval_overlap", [row for row in per_run if row["repeated_result_slots"] == 0]),
        ],
        "recall_buckets": recall_buckets,
        "tool_call_buckets": tool_buckets,
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
