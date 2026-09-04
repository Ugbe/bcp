import os
import sys
from pathlib import Path
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = Path(__file__).parent.parent.resolve()
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from dashboard.data_loader import (
    get_available_models,
    get_model_summary_stats,
    get_query_results_list,
    get_single_query_details,
    get_latest_logs,
    get_active_processes
)

app = FastAPI(
    title="BrowseComp-Plus Benchmark Monitor",
    description="Live process dashboard and evaluation monitor for BrowseComp-Plus runs",
    version="1.0.0"
)

# Enable CORS for external access / cross-origin dashboard clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def read_root():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Dashboard index.html not found.")
    return HTMLResponse(content=index_file.read_text(encoding="utf-8"))


@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "active_processes": get_active_processes(),
        "workspace": str(WORKSPACE_ROOT)
    }


@app.get("/api/models")
def list_models():
    models = get_available_models(WORKSPACE_ROOT)
    return {
        "models": models,
        "default": models[0] if models else "qwen3.8-27b-lora_custom"
    }


@app.get("/api/status")
def get_status(model: str = Query(None)):
    available_models = get_available_models(WORKSPACE_ROOT)
    if not model or model not in available_models:
        model = available_models[0] if available_models else "qwen3.8-27b-lora_custom"

    stats = get_model_summary_stats(WORKSPACE_ROOT, model)
    return stats


@app.get("/api/runs")
def get_runs(
    model: str = Query(None),
    status: str = Query(None),
    search: str = Query(None)
):
    available_models = get_available_models(WORKSPACE_ROOT)
    if not model or model not in available_models:
        model = available_models[0] if available_models else "qwen3.8-27b-lora_custom"

    results = get_query_results_list(
        workspace_root=WORKSPACE_ROOT,
        model_name=model,
        filter_status=status,
        search_query=search
    )
    return {
        "model": model,
        "count": len(results),
        "results": results
    }


@app.get("/api/run/{model}/{query_id}")
def get_run_detail(model: str, query_id: str):
    details = get_single_query_details(
        workspace_root=WORKSPACE_ROOT,
        model_name=model,
        query_id=query_id
    )
    if not details.get("run_data") and not details.get("eval_data"):
        raise HTTPException(status_code=404, detail=f"Query {query_id} not found for model {model}")
    return details


@app.get("/api/logs")
def get_logs(lines: int = Query(150, ge=10, le=1000)):
    logs_data = get_latest_logs(WORKSPACE_ROOT, max_lines=lines)
    return logs_data
