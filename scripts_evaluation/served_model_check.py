"""Check that MODEL_NAME selects the intended model on a vLLM server.

vLLM serves a LoRA adapter under its own name from ``--lora-modules`` and lists
it in ``/v1/models`` with ``parent`` set to the base model. A request whose
``model`` is the base name runs the base weights without the adapter and gives
no error, so a misconfigured ``MODEL_NAME`` silently benchmarks the wrong model.
"""

from __future__ import annotations

ALLOW_BASE_ENV = "BCP_ALLOW_BASE_MODEL_WITH_LORA"


def served_model_problem(
    model_name: str, served_models: list[dict], *, allow_base_with_lora: bool = False
) -> str | None:
    """Return a failure message, or None when ``model_name`` is acceptable."""

    served_ids = [str(item.get("id")) for item in served_models if item.get("id")]
    if model_name not in served_ids:
        return (
            f"MODEL_NAME={model_name!r} is not served; /v1/models lists {served_ids}"
        )
    adapters = [
        str(item.get("id"))
        for item in served_models
        if item.get("parent") == model_name and item.get("id") != model_name
    ]
    if adapters and not allow_base_with_lora:
        return (
            f"MODEL_NAME={model_name!r} is the base model, so requests run without "
            f"the served LoRA adapter(s) {adapters}. Set MODEL_NAME to the adapter "
            f"name, or set {ALLOW_BASE_ENV}=1 to benchmark the base model on purpose."
        )
    return None
