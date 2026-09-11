"""Smoke test: remote retrieval API + served model + Azure judge connectivity."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

failures = []

# 1. Remote retrieval service
print("=" * 60)
print("1. Retrieval service:", os.environ["BCP_RETRIEVAL_URL"])
try:
    import requests

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {os.environ['BCP_TOKEN']}"
    retrieval_url = os.environ["BCP_RETRIEVAL_URL"].rstrip("/")
    retrieval_api = os.environ.get("BCP_RETRIEVAL_API", "legacy").strip().lower()
    retrieval_model = os.environ.get("BCP_RETRIEVAL_MODEL", "").strip()
    if retrieval_api not in {"legacy", "dense"}:
        raise RuntimeError(
            "BCP_RETRIEVAL_API must be 'legacy' or 'dense', "
            f"received {retrieval_api!r}"
        )

    search_body = {"query": "who invented the telephone", "k": 10}
    if retrieval_api == "dense":
        search_body["include_text"] = True
        if retrieval_model:
            search_body["model"] = retrieval_model
        search_path = "/search"
    else:
        search_path = "/retrieve"
        search_body = {"query": "who invented the telephone"}
    r = s.post(f"{retrieval_url}{search_path}", json=search_body, timeout=60)
    r.raise_for_status()
    payload = r.json()
    hits = (
        payload.get("results", [])
        if retrieval_api == "dense" and isinstance(payload, dict)
        else payload.get("result", []) if isinstance(payload, dict) else payload
    )
    print(f"   OK - {len(hits)} hits, top docids: {[h['docid'] for h in hits[:5]]}")
    if not hits:
        raise RuntimeError("retrieval returned no hits for the smoke query")
    if retrieval_api == "dense":
        print(
            "   novelty exclusions are client-side for this dense API; use "
            "RETRIEVAL_NOVELTY=local or server (with local fallback)."
        )
    else:
        baseline_docids = [str(hit["docid"]) for hit in hits]
        exclusion_probe = s.post(
            f"{retrieval_url}/retrieve",
            json={
                "query": "who invented the telephone",
                "exclude_docids": baseline_docids[:5],
                "k": 10,
                "seen_anchor_count": 0,
            },
            timeout=60,
        )
        exclusion_probe.raise_for_status()
        exclusion_payload = exclusion_probe.json()
        exclusion_hits = (
            exclusion_payload.get("result", [])
            if isinstance(exclusion_payload, dict)
            else exclusion_payload
        )
        exclusion_meta = (
            exclusion_payload.get("metadata", {})
            if isinstance(exclusion_payload, dict)
            else {}
        )
        exclusions_applied = bool(exclusion_meta.get("exclusions_applied"))
        leaked = sorted(
            set(baseline_docids[:5]).intersection(
                str(hit["docid"]) for hit in exclusion_hits
            )
        )
        require_exclusions = os.environ.get(
            "BCP_REQUIRE_RETRIEVAL_EXCLUSIONS", "0"
        ).strip().lower() in {"1", "true", "yes"}
        if exclusions_applied and leaked:
            raise RuntimeError(
                "retrieval advertised exclusion support but returned excluded "
                f"docids: {leaked}"
            )
        if exclusions_applied:
            print(
                "   novelty exclusions OK - "
                f"{len(exclusion_hits)} hits, metadata={exclusion_meta}"
            )
        elif require_exclusions:
            raise RuntimeError(
                "retrieval service does not advertise exclusions_applied=true; "
                "deploy the novelty-capable server or unset "
                "BCP_REQUIRE_RETRIEVAL_EXCLUSIONS"
            )
        else:
            print(
                "   novelty exclusions unavailable on this server (legacy-compatible "
                "warning; set BCP_REQUIRE_RETRIEVAL_EXCLUSIONS=1 for treatment runs)"
            )
    docid = hits[0]["docid"]
    if retrieval_api == "dense":
        r = s.get(f"{retrieval_url}/document/{quote(str(docid), safe='')}", timeout=60)
    else:
        r = s.get(f"{retrieval_url}/get_document", params={"docid": docid}, timeout=60)
    r.raise_for_status()
    print(f"   get_document OK - docid {docid}, {len(r.json()['text'])} chars")
    if retrieval_api == "dense":
        r = s.post(f"{retrieval_url}/documents", json={"docids": [str(docid)]}, timeout=60)
        r.raise_for_status()
        documents = r.json().get("documents", [])
        if not documents or str(documents[0].get("docid")) != str(docid):
            raise RuntimeError("bulk /documents response did not contain the requested docid")
        print("   bulk documents OK - one requested document returned")
except Exception as e:
    failures.append(f"retrieval: {e}")
    print(f"   FAIL: {e}")

# 2. Served model (chat completions + structured tool-call contract)
print("=" * 60)
print("2. Model:", os.environ["MODEL_BASE_URL"])
try:
    from openai import OpenAI

    client = OpenAI(
        base_url=os.environ["MODEL_BASE_URL"],
        api_key=os.environ["MODEL_API_KEY"],
    )
    resp = client.chat.completions.create(
        model=os.environ["MODEL_NAME"],
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        temperature=0.0,
        max_tokens=256,
    )
    plain_choice = resp.choices[0]
    plain_content = plain_choice.message.content
    if not plain_content or plain_content.strip() != "PONG":
        reasoning = getattr(plain_choice.message, "reasoning_content", None)
        detail = (
            "plain-response contract failed; expected 'PONG', "
            f"content={plain_content!r}, finish_reason={plain_choice.finish_reason!r}, "
            f"reasoning_preview={str(reasoning or '')[:300]!r}"
        )
        failures.append(f"model: {detail}")
        print(f"   FAIL: {detail}")
    else:
        print(f"   plain response OK - replied: {plain_content!r}")

    contract_tool = {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search a knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    }
    contract = client.chat.completions.create(
        model=os.environ["MODEL_NAME"],
        messages=[
            {
                "role": "user",
                "content": (
                    "Call the search tool exactly once with the query "
                    "'browsecomp tool contract test'. Do not answer directly."
                ),
            }
        ],
        tools=[contract_tool],
        tool_choice="auto",
        temperature=0.6,
        top_p=0.95,
        max_tokens=512,
        extra_body={"top_k": 20, "repetition_penalty": 1.05},
    )
    contract_choice = contract.choices[0]
    calls = contract_choice.message.tool_calls or []
    if not calls:
        raw_content = contract_choice.message.content or ""
        if "<tool_call" in raw_content:
            detail = (
                "model emitted a textual <tool_call>, but the server did not "
                "parse it into message.tool_calls"
            )
        else:
            detail = "model returned neither a structured nor textual tool call"
        raise RuntimeError(
            f"tool contract failed ({detail}); finish_reason={contract_choice.finish_reason!r}; "
            f"content={raw_content[:500]!r}"
        )

    first_call = calls[0].function
    arguments = json.loads(first_call.arguments)
    if len(calls) != 1:
        raise RuntimeError(f"expected exactly one tool call, received {len(calls)}")
    if (
        contract_choice.finish_reason != "tool_calls"
        or first_call.name != "search"
        or arguments.get("query") != "browsecomp tool contract test"
    ):
        raise RuntimeError(
            "invalid structured tool call: "
            f"finish_reason={contract_choice.finish_reason!r}, "
            f"name={first_call.name!r}, arguments={arguments!r}"
        )
    print(
        "   structured tool call OK - "
        f"finish_reason={contract_choice.finish_reason!r}, "
        f"name={first_call.name!r}, arguments={arguments!r}"
    )

    require_tool_choice_required = os.environ.get(
        "BCP_REQUIRE_TOOL_CHOICE_REQUIRED", "0"
    ).strip().lower() in {"1", "true", "yes"}
    try:
        required_contract = client.chat.completions.create(
            model=os.environ["MODEL_NAME"],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Call the search tool once with the query "
                        "'browsecomp required tool contract'."
                    ),
                }
            ],
            tools=[contract_tool],
            tool_choice="required",
            temperature=0.0,
            max_tokens=256,
        )
        required_calls = required_contract.choices[0].message.tool_calls or []
        if not required_calls:
            raise RuntimeError("no structured tool call returned")
        print("   tool_choice='required' OK - recovery turns can force a tool")
    except Exception as required_error:
        if require_tool_choice_required:
            raise RuntimeError(
                "tool_choice='required' is required for this treatment but failed: "
                f"{required_error}"
            ) from required_error
        print(
            "   tool_choice='required' unavailable (legacy-compatible warning; "
            "recovery must fall back to explicit instruction plus auto): "
            f"{required_error}"
        )

    assistant_tool_turn = {
        "role": "assistant",
        "content": contract_choice.message.content,
        "tool_calls": [
            {
                "id": calls[0].id,
                "type": "function",
                "function": {
                    "name": first_call.name,
                    "arguments": first_call.arguments,
                },
            }
        ],
    }
    continuation = client.chat.completions.create(
        model=os.environ["MODEL_NAME"],
        messages=[
            {
                "role": "user",
                "content": (
                    "Call the search tool exactly once with the query "
                    "'browsecomp tool contract test'. Then use its result and end "
                    "with Explanation, Exact Answer, and Confidence fields."
                ),
            },
            assistant_tool_turn,
            {
                "role": "tool",
                "tool_call_id": calls[0].id,
                "content": (
                    "Contract evidence [731]: the exact answer to this synthetic "
                    "contract test is ORBIT-731."
                ),
            },
        ],
        tools=[contract_tool],
        tool_choice="none",
        temperature=0.0,
        max_tokens=512,
    )
    continuation_choice = continuation.choices[0]
    final_content = continuation_choice.message.content or ""
    required_fields = ("Explanation:", "Exact Answer:", "Confidence:")
    if (
        continuation_choice.finish_reason != "stop"
        or not all(field in final_content for field in required_fields)
        or "ORBIT-731" not in final_content
    ):
        reasoning = getattr(continuation_choice.message, "reasoning_content", None)
        raise RuntimeError(
            "tool-result continuation failed; "
            f"finish_reason={continuation_choice.finish_reason!r}, "
            f"content={final_content[:500]!r}, "
            f"reasoning_preview={str(reasoning or '')[:300]!r}"
        )
    print("   tool-result continuation OK - returned formatted final answer")
except Exception as e:
    failures.append(f"model: {e}")
    print(f"   FAIL: {e}")

# 3. Azure judge
print("=" * 60)
print("3. Azure judge:", os.environ["AZURE_OPENAI_ENDPOINT"])
try:
    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )
    resp = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        max_tokens=10,
    )
    azure_content = resp.choices[0].message.content
    if not azure_content or azure_content.strip() != "PONG":
        raise RuntimeError(f"expected 'PONG', received {azure_content!r}")
    print(f"   OK - replied: {azure_content!r}")
except Exception as e:
    failures.append(f"azure: {e}")
    print(f"   FAIL: {e}")

print("=" * 60)
if failures:
    print("FAILURES:", json.dumps(failures, indent=2))
    sys.exit(1)
print("All connectivity checks passed.")
