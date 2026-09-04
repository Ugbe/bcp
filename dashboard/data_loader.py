import os
import json
import csv
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


def get_available_models(workspace_root: Path) -> List[str]:
    models: Dict[str, float] = {}
    runs_dir = workspace_root / "runs"
    evals_dir = workspace_root / "evals"

    for root in (runs_dir, evals_dir):
        if not root.exists():
            continue
        for path in root.iterdir():
            if not path.is_dir():
                continue
            latest = path.stat().st_mtime
            for child in path.glob("run_*.json"):
                latest = max(latest, child.stat().st_mtime)
            models[path.name] = max(models.get(path.name, 0.0), latest)

    return (
        sorted(models, key=lambda name: (-models[name], name))
        if models
        else ["qwen3.8-27b-lora_custom"]
    )


def get_total_queries_target(workspace_root: Path) -> int:
    configured_target = os.environ.get("BCP_TARGET_QUERIES")
    if configured_target:
        try:
            target = int(configured_target)
            if target > 0:
                return target
        except ValueError:
            pass
    queries_file = workspace_root / "topics-qrels" / "queries.tsv"
    if queries_file.exists():
        try:
            with queries_file.open("r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except Exception:
            pass
    return 830


def load_qrel_evidence(workspace_root: Path) -> Dict[str, List[str]]:
    """qid -> list of evidence docids, from topics-qrels/qrel_evidence.txt."""
    qrel_file = workspace_root / "topics-qrels" / "qrel_evidence.txt"
    qrel: Dict[str, List[str]] = {}
    if not qrel_file.exists():
        return qrel
    try:
        with qrel_file.open("r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) == 4:
                    qrel.setdefault(parts[0], []).append(parts[2])
    except Exception:
        pass
    return qrel


def compute_live_retrieval_recall(
    run_files: List[Path], qrel_evidence: Dict[str, List[str]]
) -> Optional[float]:
    """Mean evidence recall of retrieved_docids across run files, in percent."""
    recalls = []
    for rf in run_files:
        try:
            with rf.open("r", encoding="utf-8") as f:
                rdata = json.load(f)
            qid = str(rdata.get("query_id") or "")
            positives = qrel_evidence.get(qid)
            if not positives:
                continue
            retrieved = set(rdata.get("retrieved_docids") or [])
            recalls.append(len(retrieved & set(positives)) / len(positives))
        except Exception:
            pass
    return round(sum(recalls) / len(recalls) * 100.0, 2) if recalls else None


def get_active_processes() -> Dict[str, Any]:
    active_procs = []
    if HAS_PSUTIL:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'cpu_percent', 'memory_info']):
            try:
                cmdline = proc.info.get('cmdline') or []
                cmd_str = " ".join(cmdline)
                proc_name = (proc.info.get('name') or '').lower()
                
                # Exclude shell wrappers (bash, powershell, cmd)
                if any(sh in proc_name for sh in ['bash', 'powershell', 'cmd', 'conhost']):
                    continue

                # Check for relevant python benchmark / eval scripts only
                is_benchmark = any(kw in cmd_str for kw in [
                    "oss_client.py", "chat_client.py", "evaluate_with_openai.py", 
                    "evaluate_run.py", "evaluate_with_azure.py"
                ])
                # Ignore self if python command matches
                if is_benchmark and "data_loader.py" not in cmd_str and "dashboard" not in cmd_str:
                    create_time = proc.info.get('create_time')
                    uptime_str = ""
                    if create_time:
                        elapsed_sec = int(time.time() - create_time)
                        m, s = divmod(elapsed_sec, 60)
                        h, m = divmod(m, 60)
                        uptime_str = f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

                    if "oss_client" in cmd_str:
                        proc_type = "Benchmarking (oss_client)"
                    elif "chat_client" in cmd_str:
                        proc_type = "Benchmarking (chat_client)"
                    elif "evaluate_with_azure" in cmd_str:
                        proc_type = "Evaluating (azure_judge)"
                    else:
                        proc_type = "Evaluating (openai_judge)"

                    active_procs.append({
                        "pid": proc.info['pid'],
                        "name": proc.info['name'],
                        "type": proc_type,
                        "cmd": cmd_str,
                        "uptime": uptime_str,
                        "memory_mb": round((proc.info.get('memory_info').rss if proc.info.get('memory_info') else 0) / (1024 * 1024), 1)
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

    return {
        "is_running": len(active_procs) > 0,
        "active_processes": active_procs,
        "status_label": "Running" if len(active_procs) > 0 else "Idle"
    }


def get_model_summary_stats(workspace_root: Path, model_name: str) -> Dict[str, Any]:
    total_target = get_total_queries_target(workspace_root)
    runs_dir = workspace_root / "runs" / model_name
    evals_dir = workspace_root / "evals" / model_name

    run_files = list(runs_dir.glob("run_*.json")) if runs_dir.exists() else []
    eval_files = list(evals_dir.glob("run_*_eval.json")) if evals_dir.exists() else []

    runs_completed = len(run_files)
    evals_completed = len(eval_files)

    run_progress_pct = round((runs_completed / total_target) * 100, 2) if total_target > 0 else 0
    eval_progress_pct = round((evals_completed / total_target) * 100, 2) if total_target > 0 else 0

    # Summary metrics from evaluation_summary.json
    summary_file = evals_dir / "evaluation_summary.json" if evals_dir.exists() else None
    summary_data = {}
    if summary_file and summary_file.exists():
        try:
            with summary_file.open("r", encoding="utf-8") as f:
                summary_data = json.load(f)
        except Exception:
            pass

    # Read detailed CSV if exists or compute from eval files
    correct_count = 0
    incorrect_count = 0
    parse_error_count = 0
    total_recalls = []
    total_precisions = []
    tool_counts = []

    for ef in eval_files:
        try:
            with ef.open("r", encoding="utf-8") as f:
                data = json.load(f)
                jr = data.get("judge_result") or {}
                if jr.get("parse_error"):
                    parse_error_count += 1
                elif jr.get("correct") is True:
                    correct_count += 1
                elif jr.get("correct") is False:
                    incorrect_count += 1

                cit = data.get("citations") or {}
                metrics = cit.get("metrics") or cit.get("metrics_positives") or {}
                if "recall" in metrics:
                    total_recalls.append(metrics["recall"] * 100)
                if "precision" in metrics:
                    total_precisions.append(metrics["precision"] * 100)

                tc = data.get("tool_call_counts") or {}
                if "search" in tc:
                    tool_counts.append(tc["search"])
        except Exception:
            pass

    avg_accuracy = round((correct_count / evals_completed * 100), 2) if evals_completed > 0 else summary_data.get("Accuracy (%)", 0.0)

    # Live retrieval recall straight from run files (no judge needed)
    live_recall = compute_live_retrieval_recall(run_files, load_qrel_evidence(workspace_root))
    avg_recall = live_recall if live_recall is not None else summary_data.get("Recall (%)", 0.0)

    avg_precision = round(sum(total_precisions) / len(total_precisions), 2) if total_precisions else 0.0
    avg_tool_calls = round(sum(tool_counts) / len(tool_counts), 2) if tool_counts else summary_data.get("avg_tool_stats", {}).get("search", 0.0)

    # Process status
    proc_info = get_active_processes()

    # Find latest file mtime
    latest_mtime = None
    all_files = run_files + eval_files
    if all_files:
        latest_mtime = max(f.stat().st_mtime for f in all_files)
        latest_mtime_str = datetime.fromtimestamp(latest_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    else:
        latest_mtime_str = "N/A"

    return {
        "model_name": model_name,
        "total_target_queries": total_target,
        "runs_completed": runs_completed,
        "evals_completed": evals_completed,
        "runs_progress_pct": run_progress_pct,
        "evals_progress_pct": eval_progress_pct,
        "accuracy_pct": avg_accuracy,
        "recall_pct": avg_recall,
        "precision_pct": avg_precision,
        "avg_tool_calls": avg_tool_calls,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "parse_error_count": parse_error_count,
        "pending_eval_count": max(0, runs_completed - evals_completed),
        "process_status": proc_info,
        "last_activity": latest_mtime_str,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    }


def get_query_results_list(
    workspace_root: Path, 
    model_name: str, 
    filter_status: Optional[str] = None,
    search_query: Optional[str] = None
) -> List[Dict[str, Any]]:
    runs_dir = workspace_root / "runs" / model_name
    evals_dir = workspace_root / "evals" / model_name

    if not runs_dir.exists():
        return []

    # Map query_id -> run data & eval data
    queries_map: Dict[str, Dict[str, Any]] = {}

    for rf in runs_dir.glob("run_*.json"):
        try:
            mtime = rf.stat().st_mtime
            with rf.open("r", encoding="utf-8") as f:
                rdata = json.load(f)
                qid = str(rdata.get("query_id") or "")
                if not qid:
                    continue
                queries_map[qid] = {
                    "query_id": qid,
                    "run_file": rf.name,
                    "run_status": rdata.get("status", "completed"),
                    "tool_calls": rdata.get("tool_call_counts", {}).get("search", 0),
                    "model": rdata.get("metadata", {}).get("model", model_name),
                    "timestamp": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "mtime": mtime,
                    "eval_status": "pending_eval",
                    "question": "",
                    "predicted_answer": "",
                    "correct_answer": "",
                    "judge_correct": None,
                    "confidence": None,
                    "recall": None,
                    "precision": None
                }
        except Exception:
            pass

    if evals_dir.exists():
        for ef in evals_dir.glob("run_*_eval.json"):
            try:
                with ef.open("r", encoding="utf-8") as f:
                    edata = json.load(f)
                    qid = str(edata.get("query_id") or "")
                    if not qid:
                        continue
                    if qid not in queries_map:
                        mtime = ef.stat().st_mtime
                        queries_map[qid] = {
                            "query_id": qid,
                            "run_file": "",
                            "run_status": "completed",
                            "tool_calls": edata.get("tool_call_counts", {}).get("search", 0),
                            "model": model_name,
                            "timestamp": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            "mtime": mtime
                        }

                    item = queries_map[qid]
                    item["eval_status"] = "evaluated"
                    item["question"] = edata.get("question", "")
                    item["correct_answer"] = edata.get("correct_answer", "")
                    
                    jr = edata.get("judge_result") or {}
                    item["predicted_answer"] = jr.get("extracted_final_answer") or (
                        edata.get("response", "")[:120] + "..." if edata.get("response") else ""
                    )
                    item["judge_correct"] = jr.get("correct")
                    item["confidence"] = jr.get("confidence")
                    item["parse_error"] = jr.get("parse_error", False)

                    cit = edata.get("citations") or {}
                    metrics = cit.get("metrics") or cit.get("metrics_positives") or {}
                    if "recall" in metrics:
                        item["recall"] = round(metrics["recall"] * 100, 1)
                    if "precision" in metrics:
                        item["precision"] = round(metrics["precision"] * 100, 1)
            except Exception:
                pass

    results = list(queries_map.values())

    # Apply filters
    if filter_status:
        filter_status = filter_status.lower()
        if filter_status == "correct":
            results = [r for r in results if r.get("judge_correct") is True]
        elif filter_status == "incorrect":
            results = [r for r in results if r.get("judge_correct") is False]
        elif filter_status == "pending_eval":
            results = [r for r in results if r.get("eval_status") == "pending_eval"]
        elif filter_status == "parse_error":
            results = [r for r in results if r.get("parse_error") is True]

    if search_query:
        sq = search_query.lower()
        results = [
            r for r in results if (
                sq in str(r.get("query_id", "")).lower() or
                sq in str(r.get("question", "")).lower() or
                sq in str(r.get("predicted_answer", "")).lower() or
                sq in str(r.get("correct_answer", "")).lower()
            )
        ]

    # Sort by query_id numerical if possible, else string or mtime
    def sort_key(x):
        try:
            return (0, int(x["query_id"]))
        except ValueError:
            return (1, x["query_id"])

    return sorted(results, key=sort_key)


def get_single_query_details(workspace_root: Path, model_name: str, query_id: str) -> Dict[str, Any]:
    runs_dir = workspace_root / "runs" / model_name
    evals_dir = workspace_root / "evals" / model_name

    run_data = None
    eval_data = None

    if runs_dir.exists():
        for rf in runs_dir.glob("run_*.json"):
            try:
                with rf.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if str(data.get("query_id")) == str(query_id):
                        run_data = data
                        break
            except Exception:
                pass

    if evals_dir.exists():
        for ef in evals_dir.glob("run_*_eval.json"):
            try:
                with ef.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if str(data.get("query_id")) == str(query_id):
                        eval_data = data
                        break
            except Exception:
                pass

    return {
        "query_id": query_id,
        "model_name": model_name,
        "run_data": run_data,
        "eval_data": eval_data
    }


def get_latest_logs(workspace_root: Path, max_lines: int = 150) -> Dict[str, Any]:
    runs_dir = workspace_root / "runs"
    log_files = list(runs_dir.glob("*.log")) if runs_dir.exists() else []

    if not log_files:
        return {"filename": None, "logs": ["No log files found in runs/"]}

    # Pick the log file modified most recently
    log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    target_log = log_files[0]

    try:
        with target_log.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            tail_lines = lines[-max_lines:] if len(lines) > max_lines else lines
            return {
                "filename": target_log.name,
                "path": str(target_log),
                "total_lines": len(lines),
                "logs": [l.rstrip() for l in tail_lines]
            }
    except Exception as err:
        return {"filename": target_log.name, "logs": [f"Error reading log file: {err}"]}
