"""
Live judge for BrowseComp-Plus runs using an Azure OpenAI deployment.

Watches an input directory of run_*.json files (as produced by the search_agent
clients), judges each new completion with the Azure deployment (default gpt-4o)
using the same GRADER_TEMPLATE as the other evaluators, and writes one
run_*_eval.json per run plus a continuously refreshed evaluation_summary.json.

Designed to run concurrently with the agent: start it before (or during) the
benchmark and accuracy updates live on the dashboard.

Configuration comes from the environment (or .env at the repo root):

    AZURE_OPENAI_API_KEY
    AZURE_OPENAI_ENDPOINT       e.g. https://preai.openai.azure.com
    AZURE_OPENAI_DEPLOYMENT     e.g. gpt-4o
    AZURE_OPENAI_API_VERSION    e.g. 2024-10-21
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from dotenv import load_dotenv
from openai import AzureOpenAI
from tqdm import tqdm

sys.path.append(str(Path(__file__).parent))
sys.path.append(str(Path(__file__).parent.parent))

from evaluate_with_openai import (
    calculate_calibration_error,
    compute_citation_metrics,
    create_judge_prompt,
    extract_citations_from_response,
    load_ground_truth,
    load_qrel_data,
    mirror_directory_structure,
    parse_judge_response,
)

REPO_ROOT = Path(__file__).parent.parent
load_dotenv(REPO_ROOT / ".env")


# These are the manually reviewed, high-confidence semantic equivalences that
# cannot be established by generic typography/name/legal-suffix rules alone.
# They are deliberately qid-scoped so a generic word such as "wing" or
# "pueblo" is never treated as interchangeable in unrelated questions.
_REVIEWED_EQUIVALENCES = {
    "394": ("wing three-quarter", "Wing"),
    "443": ("Ivapur Hidra", "Ivatherm Ivapur Hidra Hydrating Cream"),
    "445": (
        "multiple-membership, multiple-classification",
        "multiple-membership multiple-classification model",
    ),
    "516": ("Sacred Heart High School", "Sacred Heart High School Hammersmith"),
    "652": ("Auxly", "Auxly Cannabis Group Inc."),
    "991": ("Tóke Makinwa", "Toke Makinwa"),
    "1150": ("Trudy Bronner, Mathematics", "Trudy Bronner, Math"),
    "1155": ("Giusewa", "Giusewa Pueblo"),
}

# These reviewed pairs contain meaningful differences and are safe to reject
# deterministically.  Pair-scoping prevents unrelated answers for the same qid
# from being classified without a judge.
_REVIEWED_REJECTIONS = {
    "219": ("Fall of the Queen Bean", "Fall for the Queen Bean"),
    "425": ("Graham Cracker Crumble", "Graham"),
    "509": ("Sacrofanite", "Sacrofanoite"),
    "514": ("Maximilian Josef Sommer", "Josef Sommer"),
    "579": ("March 5, 2021", "March 9, 2021"),
    "593": ("February 9, 2021", "February 2021"),
    "625": ("6; Lynn Okamoto", "7; Lynn Okamoto"),
    "826": ("Sunset at Biafra", "Sunset in Biafra"),
    "1061": ("I Survived I Kissed Dating Goodbye", "I Kissed Dating Goodbye"),
}

# These pairs need an explicit benchmark policy or human/LLM adjudication.  They
# must not become automatic score increases merely because they are plausible.
_POLICY_DEPENDENT_EQUIVALENCES = {
    "275": ("Union Carbide and Carbon Corporation.", "Union Carbide Corporation"),
    "278": ("MediaLab Group", "Medialab"),
    "283": ("Zimri Elder", "Zimri Eder"),
    "406": ("Masata Kato", "Masato Kato"),
    "422": ("Nicotiana tabacum variety Wisconsin 38", "Wisconsin 38"),
    "519": (
        "Glafkos Clerides: The Path of a Country",
        "Glafcos Clerides: The Path of a Country",
    ),
    "532": ("Hartlepool United", "Hartlepools United"),
    "577": ("DN AGRAR Group", "DN AGRAR"),
    "709": ("Lewis Dunk", "Dunk"),
    "749": ("Joseph Anokye", "Joe Anokye"),
    "952": ("John Talabot session", "Some John Talabot session"),
    "1079": ("Uche Jombo Rodriguez", "Uche Jombo"),
}

_HONORIFICS = {"dr", "doctor", "mr", "mrs", "ms", "miss", "prof", "professor"}
_LEGAL_SUFFIXES: Tuple[Tuple[str, ...], ...] = (
    ("private", "limited"),
    ("pvt", "ltd"),
    ("incorporated",),
    ("limited",),
    ("inc",),
    ("ltd",),
    ("llc",),
    ("plc",),
    ("sa",),
)
_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "ðŸ", "�")


@dataclass(frozen=True)
class EquivalenceDecision:
    """A conservative deterministic answer-comparison result.

    ``correct`` is ``None`` when the normalizer cannot safely decide.  Such
    cases retain the LLM judge path.  ``needs_adjudication`` is only a signal
    for the opt-in second, entity-equivalence judge.
    """

    correct: Optional[bool]
    rule: str
    expected_normalized: str
    predicted_normalized: str
    needs_adjudication: bool = False


def _repair_mojibake(value: str) -> str:
    """Repair the common UTF-8-decoded-as-Latin-1 failure when unambiguous."""

    if not any(marker in value for marker in _MOJIBAKE_MARKERS):
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    old_markers = sum(value.count(marker) for marker in _MOJIBAKE_MARKERS)
    new_markers = sum(repaired.count(marker) for marker in _MOJIBAKE_MARKERS)
    return repaired if new_markers < old_markers else value


def normalize_answer(value: str) -> str:
    """Return a stable, accent-insensitive comparison representation."""

    value = _repair_mojibake(str(value or ""))
    value = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold().replace("&", " and ")
    # Preserve dotted two-letter legal abbreviations (for example, S.A.) as a
    # single token before general punctuation removal.
    value = re.sub(r"\b([a-z])\.\s*([a-z])\.", r"\1\2", value)
    value = re.sub(r"[\u2010-\u2015\u2212-]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _pair_matches(
    expected: str, predicted: str, reviewed_pair: Optional[Tuple[str, str]]
) -> bool:
    if not reviewed_pair:
        return False
    actual = {normalize_answer(expected), normalize_answer(predicted)}
    reviewed = {normalize_answer(reviewed_pair[0]), normalize_answer(reviewed_pair[1])}
    return actual == reviewed


def _strip_leading_honorific(tokens: Sequence[str]) -> List[str]:
    result = list(tokens)
    while result and result[0] in _HONORIFICS:
        result.pop(0)
    return result


def _strip_legal_suffix(tokens: Sequence[str]) -> List[str]:
    result = list(tokens)
    changed = True
    while result and changed:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if len(result) >= len(suffix) and tuple(result[-len(suffix) :]) == suffix:
                result = result[: -len(suffix)]
                changed = True
                break
    return result


def _middle_name_equivalent(left: Sequence[str], right: Sequence[str]) -> bool:
    """Compare full personal names without ever accepting surname-only output."""

    if not (2 <= len(left) <= 4 and 2 <= len(right) <= 4):
        return False
    if left[0] != right[0] or left[-1] != right[-1]:
        return False
    left_middle, right_middle = list(left[1:-1]), list(right[1:-1])
    if not left_middle or not right_middle:
        return True
    if len(left_middle) != len(right_middle):
        return False
    return all(
        a == b or (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b))
        for a, b in zip(left_middle, right_middle)
    )


def _normalize_measurement_units(value: str) -> str:
    value = re.sub(r"(?<=\d)\s*(?:lbs?|pounds?)\b", " lb", value)
    value = re.sub(r"(?<=\d)\s*(?:ozs?|ounces?)\b", " oz", value)
    return " ".join(value.split())


def _singularize_one_word(value: str) -> str:
    if value.endswith("oes") and len(value) > 4:
        return value[:-2]
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith(("ches", "shes", "sses", "xes", "zes")) and len(value) > 4:
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss") and len(value) > 3:
        return value[:-1]
    return value


def deterministic_answer_equivalence(
    correct_answer: str, model_answer: str, query_id: Optional[object] = None
) -> EquivalenceDecision:
    """Classify only equivalences or differences that are safe without an LLM."""

    expected = normalize_answer(correct_answer)
    predicted = normalize_answer(model_answer)
    qid = str(query_id) if query_id is not None else ""

    if not predicted or predicted in {"none", "unknown", "cannot determine"}:
        return EquivalenceDecision(False, "explicit_no_answer", expected, predicted)

    if _pair_matches(correct_answer, model_answer, _REVIEWED_REJECTIONS.get(qid)):
        return EquivalenceDecision(False, f"reviewed_rejection:qid_{qid}", expected, predicted)
    if _pair_matches(
        correct_answer, model_answer, _POLICY_DEPENDENT_EQUIVALENCES.get(qid)
    ):
        return EquivalenceDecision(
            None,
            f"policy_dependent:qid_{qid}",
            expected,
            predicted,
            needs_adjudication=True,
        )
    if _pair_matches(correct_answer, model_answer, _REVIEWED_EQUIVALENCES.get(qid)):
        return EquivalenceDecision(True, f"reviewed_equivalence:qid_{qid}", expected, predicted)

    if expected == predicted:
        return EquivalenceDecision(True, "unicode_case_punctuation", expected, predicted)

    expected_numbers = re.findall(r"\d+(?:\.\d+)?", expected)
    predicted_numbers = re.findall(r"\d+(?:\.\d+)?", predicted)
    if (expected_numbers or predicted_numbers) and expected_numbers != predicted_numbers:
        return EquivalenceDecision(False, "different_numeric_values", expected, predicted)

    measured_expected = _normalize_measurement_units(expected)
    measured_predicted = _normalize_measurement_units(predicted)
    if measured_expected == measured_predicted:
        return EquivalenceDecision(True, "measurement_unit_plural", expected, predicted)

    expected_tokens = expected.split()
    predicted_tokens = predicted.split()
    if _strip_leading_honorific(expected_tokens) == _strip_leading_honorific(
        predicted_tokens
    ):
        return EquivalenceDecision(True, "honorific", expected, predicted)

    if _strip_legal_suffix(expected_tokens) == _strip_legal_suffix(predicted_tokens):
        return EquivalenceDecision(True, "legal_suffix", expected, predicted)

    if _middle_name_equivalent(expected_tokens, predicted_tokens):
        return EquivalenceDecision(True, "middle_name_or_initial", expected, predicted)

    if len(expected_tokens) == len(predicted_tokens) == 1:
        if _singularize_one_word(expected) == _singularize_one_word(predicted):
            return EquivalenceDecision(True, "simple_singular_plural", expected, predicted)

    compact_expected = "".join(expected_tokens)
    compact_predicted = "".join(predicted_tokens)
    if (
        compact_expected == compact_predicted
        and compact_expected
        and any(ch.isdigit() for ch in compact_expected)
    ):
        return EquivalenceDecision(True, "alphanumeric_spacing", expected, predicted)

    similarity = difflib.SequenceMatcher(None, expected, predicted).ratio()
    overlap = set(expected_tokens).intersection(predicted_tokens)
    near_match = similarity >= 0.82 or (
        similarity >= 0.7 and len(overlap) >= 2
    )
    return EquivalenceDecision(
        None,
        "unresolved_near_match" if near_match else "no_safe_rule",
        expected,
        predicted,
        needs_adjudication=near_match,
    )


def extract_model_answer(response: str) -> Tuple[Optional[str], str]:
    """Extract the runner's public ``Exact Answer`` field without an LLM."""

    if not response:
        return None, "missing_response"
    match = re.search(
        r"(?im)^\s*(?:\*\*)?exact\s+answer\s*(?::\*\*|\*\*:|:)\s*(.*?)\s*$",
        response,
    )
    if not match:
        return None, "missing_exact_answer"
    answer = match.group(1).strip().strip("*").strip()
    if not answer:
        return None, "empty_exact_answer"
    return answer, "parsed"


def extract_model_confidence(response: str) -> Optional[float]:
    """Extract the model's public confidence even when Azure is not called."""

    match = re.search(
        r"(?im)^\s*(?:\*\*)?confidence\s*(?::\*\*|\*\*:|:)\s*"
        r"(100(?:\.0+)?|[0-9]{1,2}(?:\.\d+)?)\s*%?\s*$",
        response or "",
    )
    return float(match.group(1)) if match else None


def create_equivalence_adjudication_prompt(
    question: str, correct_answer: str, model_answer: str
) -> str:
    return f"""You are adjudicating answer-string equivalence, not solving the question.

Question: {question}
Reference answer: {correct_answer}
Candidate answer: {model_answer}

Decide whether the two answers identify exactly the same requested entity. Treat
typography, harmless legal suffixes, and established aliases as equivalent. Reject
different dates, quantities, incomplete full names, historical/current names when
the requested time makes the distinction material, and merely related titles.

Return exactly:
reasoning: <one concise sentence>
correct: <yes or no>
confidence: <0-100>
"""


def call_azure_judge(
    client: AzureOpenAI,
    prompt: str,
    deployment: str,
    max_output_tokens: int,
    temperature: float,
    retries: int = 4,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=deployment,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_output_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            last_error = e
            wait = min(30, 5 * attempt)
            print(
                f"Azure judge call failed (attempt {attempt}/{retries}): {e} "
                f"-- retrying in {wait}s"
            )
            time.sleep(wait)
    raise RuntimeError(f"Azure judge call failed after {retries} attempts: {last_error}")


def evaluate_run_file(
    run_path: Path,
    ground_truth: Dict[str, Dict[str, str]],
    qrel_evidence: Dict[str, List[str]],
    client: AzureOpenAI,
    deployment: str,
    max_output_tokens: int,
    temperature: float,
    adjudicate_near_matches: bool = False,
) -> Optional[dict]:
    """Judge a single run file. Returns the eval result dict, or None if the
    run file should be skipped (no ground truth / unloadable)."""

    try:
        with run_path.open("r", encoding="utf-8") as f:
            run_data = json.load(f)
    except Exception as e:
        print(f"Error loading {run_path}: {e}")
        return None

    query_id = run_data.get("query_id")
    if not query_id or str(query_id) not in ground_truth:
        return None

    correct_answer = ground_truth[str(query_id)]["answer"]
    gt_question = ground_truth[str(query_id)]["question"]
    run_status = str(run_data.get("status") or "missing")
    is_completed = run_status == "completed"

    retrieved_docids_set = set(run_data.get("retrieved_docids", []))
    positives_for_query = qrel_evidence.get(str(query_id), [])
    retrieval_recall = (
        len(retrieved_docids_set.intersection(set(positives_for_query)))
        / float(len(positives_for_query))
        if positives_for_query
        else 0.0
    )

    result_items = run_data.get("result", [])
    response = ""
    if result_items and result_items[-1].get("type") == "output_text":
        response = result_items[-1].get("output", "")

    model_answer, model_answer_parse_status = extract_model_answer(response)
    model_confidence = extract_model_confidence(response)
    if not is_completed:
        model_answer_parse_status = "not_attempted_run_incomplete"

    base = {
        "json_path": str(run_path),
        "query_id": query_id,
        "correct_answer": correct_answer,
        "is_completed": is_completed,
        "run_status": run_status,
        "model_answer": model_answer,
        "model_confidence": model_confidence,
        "model_answer_parse_status": model_answer_parse_status,
        "tool_call_counts": run_data.get("tool_call_counts", {}),
        "retrieval": {
            "retrieved_docids": sorted(list(retrieved_docids_set)),
            "recall": retrieval_recall,
        },
    }

    if not is_completed:
        return {
            **base,
            "response": response,
            "judge_prompt": None,
            "judge_response": None,
            "evaluation_status": "run_incomplete",
            "judge_call_status": "not_called_run_incomplete",
            "judge_parse_status": "not_attempted",
            "raw_judge_correct": None,
            "normalized_correct": None,
            "final_correct": None,
            "normalization_rule": None,
            "judge_result": {
                "extracted_final_answer": model_answer,
                "reasoning": None,
                "correct": False,
                "confidence": None,
                "parse_error": False,
                "not_judged": True,
                "error": "Run incomplete; judge was not called",
            },
            "citations": None,
            "model_info": {"judge_model": deployment},
        }

    if response == "":
        return {
            **base,
            "response": response,
            "judge_prompt": None,
            "judge_response": None,
            "evaluation_status": "model_answer_parse_error",
            "judge_call_status": "not_called_model_answer_parse_error",
            "judge_parse_status": "not_attempted",
            "raw_judge_correct": None,
            "normalized_correct": None,
            "final_correct": None,
            "normalization_rule": None,
            "judge_result": {
                "extracted_final_answer": None,
                "reasoning": None,
                "correct": False,
                "confidence": model_confidence,
                "parse_error": False,
                "not_judged": True,
                "error": "Completed run contains no final output text",
            },
            "citations": None,
            "model_info": {"judge_model": deployment},
        }

    cited_docids = extract_citations_from_response(response)
    normalization = (
        deterministic_answer_equivalence(correct_answer, model_answer, query_id)
        if model_answer is not None
        else EquivalenceDecision(
            None,
            "answer_not_extracted",
            normalize_answer(correct_answer),
            "",
        )
    )

    judge_prompt = None
    judge_text = None
    raw_judge_result = None
    judge_call_status = "not_called_deterministic"
    judge_parse_status = "not_attempted"
    raw_judge_correct = None
    adjudication = None

    if normalization.correct is None:
        judge_prompt = create_judge_prompt(gt_question, response, correct_answer)
        try:
            judge_text = call_azure_judge(
                client, judge_prompt, deployment, max_output_tokens, temperature
            )
            judge_call_status = "succeeded"
            raw_judge_result = parse_judge_response(judge_text)
            raw_judge_correct = raw_judge_result.get("correct")
            judge_parse_status = (
                "parse_error" if raw_judge_result.get("parse_error") else "parsed"
            )
        except Exception as exc:
            judge_call_status = "error"
            raw_judge_result = {
                "extracted_final_answer": model_answer,
                "reasoning": None,
                "correct": None,
                "confidence": None,
                "parse_error": False,
                "error": str(exc),
            }

    final_correct: Optional[bool]
    if normalization.correct is not None:
        final_correct = normalization.correct
    elif judge_parse_status == "parsed":
        final_correct = raw_judge_correct
    else:
        final_correct = None

    if (
        adjudicate_near_matches
        and normalization.correct is None
        and normalization.needs_adjudication
        and raw_judge_correct is False
    ):
        adjudication_prompt = create_equivalence_adjudication_prompt(
            gt_question, correct_answer, model_answer or ""
        )
        adjudication = {
            "prompt": adjudication_prompt,
            "response": None,
            "call_status": "not_called",
            "parse_status": "not_attempted",
            "correct": None,
        }
        try:
            adjudication_text = call_azure_judge(
                client,
                adjudication_prompt,
                deployment,
                min(max_output_tokens, 384),
                0.0,
            )
            adjudication_result = parse_judge_response(adjudication_text)
            adjudication.update(
                {
                    "response": adjudication_text,
                    "call_status": "succeeded",
                    "parse_status": (
                        "parse_error"
                        if adjudication_result.get("parse_error")
                        else "parsed"
                    ),
                    "correct": adjudication_result.get("correct"),
                    "reasoning": adjudication_result.get("reasoning"),
                    "confidence": adjudication_result.get("confidence"),
                }
            )
            if not adjudication_result.get("parse_error"):
                final_correct = adjudication_result.get("correct")
        except Exception as exc:
            adjudication.update({"call_status": "error", "error": str(exc)})

    if normalization.correct is not None:
        evaluation_status = "normalized_completed"
    elif judge_call_status == "error":
        evaluation_status = "judge_call_error"
    elif judge_parse_status == "parse_error":
        evaluation_status = "judge_parse_error"
    else:
        evaluation_status = "judge_completed"

    judge_result = dict(raw_judge_result or {})
    judge_result.update(
        {
            "extracted_final_answer": judge_result.get(
                "extracted_final_answer", model_answer
            ),
            "reasoning": judge_result.get("reasoning"),
            # Compatibility field used by the existing dashboard and summaries.
            "correct": bool(final_correct) if final_correct is not None else False,
            "confidence": (
                judge_result.get("confidence")
                if judge_result.get("confidence") is not None
                else model_confidence
            ),
            "parse_error": judge_parse_status == "parse_error",
            "raw_correct": raw_judge_correct,
            "normalized_correct": normalization.correct,
            "normalization_rule": normalization.rule,
        }
    )

    return {
        **base,
        "question": gt_question,
        "response": response,
        "judge_prompt": judge_prompt,
        "judge_response": judge_text,
        "evaluation_status": evaluation_status,
        "judge_call_status": judge_call_status,
        "judge_parse_status": judge_parse_status,
        "raw_judge_correct": raw_judge_correct,
        "normalized_correct": normalization.correct,
        "final_correct": final_correct,
        "normalization_rule": normalization.rule,
        "normalization": {
            "expected": normalization.expected_normalized,
            "predicted": normalization.predicted_normalized,
            "needs_adjudication": normalization.needs_adjudication,
        },
        "adjudication": adjudication,
        "judge_result": judge_result,
        "citations": {
            "cited_docids": cited_docids,
            "metrics": compute_citation_metrics(cited_docids, positives_for_query),
        },
        "model_info": {"judge_model": deployment},
    }


def build_summary(
    eval_results: List[dict], qrel_evidence: Dict[str, List[str]], deployment: str
) -> dict:
    total = len(eval_results)
    if not total:
        return {}

    detected_model = None
    for r in eval_results:
        meta_model = None
        try:
            with open(r.get("json_path", ""), "r", encoding="utf-8") as f:
                meta_model = json.load(f).get("metadata", {}).get("model")
        except Exception:
            pass
        if meta_model:
            detected_model = str(meta_model)
            break

    all_tool_counts: Dict[str, float] = defaultdict(float)
    for r in eval_results:
        for tool_name, count in (r.get("tool_call_counts") or {}).items():
            all_tool_counts[tool_name] += count
    for tool_name in all_tool_counts:
        all_tool_counts[tool_name] /= total

    def final_correct(result: dict) -> bool:
        if "final_correct" in result:
            return result.get("final_correct") is True
        return bool((result.get("judge_result") or {}).get("correct", False))

    confidences, correctness = [], []
    for r in eval_results:
        jr = r.get("judge_result") or {}
        effective_correct = final_correct(r)
        if not jr.get("parse_error", False):
            if jr.get("confidence") is not None:
                confidences.append(jr["confidence"])
                correctness.append(effective_correct)

    calibration_error = 0.0
    if len(confidences) >= 100:
        calibration_error = calculate_calibration_error(confidences, correctness)

    retrieval_recalls = [
        r.get("retrieval", {}).get("recall", 0.0)
        for r in eval_results
        if qrel_evidence.get(str(r.get("query_id")), [])
    ]

    correct_count = sum(1 for r in eval_results if final_correct(r))
    evaluation_status_counts: Dict[str, int] = defaultdict(int)
    for result in eval_results:
        evaluation_status_counts[str(result.get("evaluation_status") or "legacy")] += 1

    per_query_metrics = [
        {
            "query_id": r.get("query_id"),
            "correct": final_correct(r),
            "evaluation_status": r.get("evaluation_status", "legacy"),
            "recall": (
                round(r.get("retrieval", {}).get("recall", 0.0) * 100.0, 2)
                if isinstance(r.get("retrieval", {}).get("recall"), (int, float))
                else None
            ),
        }
        for r in eval_results
    ]

    return {
        "LLM": detected_model or "change me when submitting",
        "Accuracy (%)": round(correct_count / total * 100.0, 2),
        "Recall (%)": (
            round(float(np.mean(retrieval_recalls)) * 100.0, 2)
            if retrieval_recalls
            else None
        ),
        "avg_tool_stats": dict(all_tool_counts),
        "Calibration Error (%)": round(calibration_error, 2),
        "Retriever": "hybrid-remote (BM25+dense+RRF+rerank)",
        "Link": "change me when submitting",
        "Evaluation Date": datetime.now().date().isoformat(),
        "judged_by": f"azure:{deployment}",
        "evaluation_status_counts": dict(evaluation_status_counts),
        "per_query_metrics": per_query_metrics,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Live-judge BrowseComp-Plus runs with an Azure OpenAI deployment."
    )
    parser.add_argument(
        "--input_dir", required=True, help="Directory containing run JSON files"
    )
    parser.add_argument(
        "--ground_truth",
        default=str(REPO_ROOT / "data" / "browsecomp_plus_decrypted.jsonl"),
        help="Path to decrypted JSONL dataset used as ground truth",
    )
    parser.add_argument(
        "--query_file",
        type=Path,
        help="Optional TSV defining the query IDs expected for this run",
    )
    parser.add_argument(
        "--eval_dir", default=str(REPO_ROOT / "evals"), help="Directory for eval results"
    )
    parser.add_argument(
        "--deployment",
        default=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        help="Azure OpenAI deployment name (default: $AZURE_OPENAI_DEPLOYMENT or gpt-4o)",
    )
    parser.add_argument(
        "--temperature", type=float, default=0.0, help="Judge decoding temperature"
    )
    parser.add_argument(
        "--max_output_tokens", type=int, default=1024, help="Judge max output tokens"
    )
    parser.add_argument(
        "--poll_interval",
        type=float,
        default=15.0,
        help="Seconds between directory scans in watch mode (default: 15)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep polling for new run files instead of exiting after one sweep",
    )
    parser.add_argument(
        "--exit_when_complete",
        action="store_true",
        help="With --watch, exit once every query in the ground truth has an eval",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-evaluate files that already have evals"
    )
    parser.add_argument(
        "--adjudicate_near_matches",
        action="store_true",
        help=(
            "For unresolved near matches rejected by the primary judge, make one "
            "additional temperature-zero entity-equivalence adjudication call"
        ),
    )
    parser.add_argument(
        "--qrel_evidence",
        default=str(REPO_ROOT / "topics-qrels" / "qrel_evidence.txt"),
        help="Path to qrel positives file",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    gt_path = Path(args.ground_truth)
    if not input_dir.is_dir():
        raise ValueError(f"Input directory {input_dir} does not exist")
    if not gt_path.is_file():
        raise ValueError(f"Ground truth JSONL file {gt_path} does not exist")

    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    if not api_key or not endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set "
            "(env or .env)"
        )

    client = AzureOpenAI(
        api_key=api_key, azure_endpoint=endpoint, api_version=api_version
    )

    ground_truth = load_ground_truth(gt_path)
    if args.query_file:
        if not args.query_file.is_file():
            raise ValueError(f"Query TSV {args.query_file} does not exist")
        expected_ids = {
            line.split("\t", 1)[0].strip()
            for line in args.query_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and "\t" in line
        }
        unknown_ids = expected_ids - set(ground_truth)
        if not expected_ids or unknown_ids:
            raise ValueError(
                f"Invalid expected query IDs: count={len(expected_ids)}, "
                f"unknown={sorted(unknown_ids)}"
            )
    else:
        expected_ids = set(ground_truth)
    qrel_evidence = load_qrel_data(Path(args.qrel_evidence))
    output_dir = mirror_directory_structure(input_dir, Path(args.eval_dir))
    print(f"Watching {input_dir}")
    print(f"Evals will be saved to {output_dir}")
    print(f"Judge: azure:{args.deployment} @ {endpoint} (api {api_version})")

    def sweep() -> List[dict]:
        """Evaluate every run file that lacks an eval; return all eval results."""
        eval_results: List[dict] = []
        newly_judged = 0

        for eval_path in sorted(output_dir.glob("run_*_eval.json")):
            try:
                with eval_path.open("r", encoding="utf-8") as f:
                    eval_results.append(json.load(f))
            except Exception:
                continue

        run_paths = sorted(input_dir.glob("run_*.json"))
        pending = [
            p
            for p in run_paths
            if args.force or not (output_dir / f"{p.stem}_eval.json").exists()
        ]

        for run_path in tqdm(pending, desc="Judging", unit="file"):
            result = evaluate_run_file(
                run_path,
                ground_truth,
                qrel_evidence,
                client,
                args.deployment,
                args.max_output_tokens,
                args.temperature,
                args.adjudicate_near_matches,
            )
            if result is None:
                continue
            eval_path = output_dir / f"{run_path.stem}_eval.json"
            with eval_path.open("w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            eval_results.append(result)
            newly_judged += 1

        if eval_results:
            summary = build_summary(eval_results, qrel_evidence, args.deployment)
            summary_path = output_dir / "evaluation_summary.json"
            with summary_path.open("w", encoding="utf-8") as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)

        judged_ids = {str(r.get("query_id")) for r in eval_results}
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] "
            f"{len(run_paths)} runs, {len(judged_ids & expected_ids)}/"
            f"{len(expected_ids)} queries judged"
            + (f" (+{newly_judged} new)" if newly_judged else "")
        )
        return eval_results

    while True:
        eval_results = sweep()
        if not args.watch:
            break
        if args.exit_when_complete and eval_results:
            judged_ids = {str(r.get("query_id")) for r in eval_results}
            if expected_ids <= judged_ids:
                print("All queries judged -- exiting.")
                break
        time.sleep(args.poll_interval)

    if eval_results:
        summary = build_summary(eval_results, qrel_evidence, args.deployment)
        print(f"Accuracy: {summary.get('Accuracy (%)')}%")
        print(f"Recall: {summary.get('Recall (%)')}%")
        print(f"Avg tool stats: {summary.get('avg_tool_stats')}")
        print(f"Summary saved to {output_dir / 'evaluation_summary.json'}")


if __name__ == "__main__":
    main()
