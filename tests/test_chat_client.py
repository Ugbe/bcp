import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "search_agent"))
sys.path.insert(0, str(REPO_ROOT))

from chat_client import (  # noqa: E402
    ChatSearchToolHandler,
    _has_valid_final_answer,
    _normalize_chat_messages,
    _parse_text_tool_calls,
    _persist_response,
    run_conversation_with_tools,
)


class _FakeMessage:
    def __init__(self, content, tool_calls=None, reasoning=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning = reasoning

    def model_dump(self, mode="python"):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": self.tool_calls,
            "reasoning": self.reasoning,
        }


class _FakeChoice:
    def __init__(self, content, finish_reason="stop", tool_calls=None, reasoning=None):
        self.message = _FakeMessage(content, tool_calls, reasoning)
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, content, finish_reason="stop", tool_calls=None, reasoning=None):
        self.choices = [_FakeChoice(content, finish_reason, tool_calls, reasoning)]


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self._responses)


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(responses)


class _FakeToolHandler:
    def execute_tool(self, name, arguments):
        if name != "search":
            raise ValueError(name)
        return json.dumps([{"docid": "42", "text": arguments["query"]}])


class _FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()

    def decode(self, tokens, skip_special_tokens=True):
        return " ".join(tokens)


class _FakeSearcher:
    def get_document(self, docid):
        return {"docid": docid, "text": "one two three four five"}


class _LegacySearchOnly:
    def search(self, query, k=10):
        return [
            {"docid": "seen", "text": "old"},
            {"docid": "new", "text": "new"},
        ]


def _tool_call(name, arguments, call_id="call_1"):
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
    ]


class _StatefulChatHandler(ChatSearchToolHandler):
    def __init__(self, search_results=None):
        self.tool_name = "search"
        self.tool_param = "query"
        self.include_get_document = True
        self.k = 10
        self.snippet_max_tokens = None
        self.document_max_tokens = 100
        self.tokenizer = None
        self.search_results = search_results or [
            {"docid": "42", "text": "Ada evidence", "score": 1.0}
        ]
        self.searcher = self
        self.search_with_metadata_calls = []

    def search(self, query, k=10):
        return [dict(item) for item in self.search_results]

    def search_with_metadata(
        self, query, k=10, *, exclude_docids=None, seen_anchor_count=0
    ):
        excluded = {str(item) for item in (exclude_docids or [])}
        self.search_with_metadata_calls.append(
            (query, excluded, seen_anchor_count)
        )
        results = [
            dict(item)
            for item in self.search_results
            if str(item["docid"]) not in excluded
        ]
        return {
            "results": results,
            "metadata": {
                "novel_count": len(results),
                "repeated_count": len(self.search_results) - len(results),
                "exclusions_applied": True,
                "fallback_applied": False,
            },
        }

    def get_document(self, docid):
        return {"docid": str(docid), "text": "full decisive evidence"}


class ChatClientContractTests(unittest.TestCase):
    def test_normalization_preserves_reasoning_before_tool_call(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "reasoning": "I should search for the institution.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"query":"institution"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "evidence"},
        ]

        normalized = _normalize_chat_messages(messages)

        self.assertEqual([item["type"] for item in normalized], ["reasoning", "tool_call"])
        self.assertEqual(normalized[0]["output"], ["I should search for the institution."])
        self.assertEqual(normalized[1]["output"], "evidence")

    def test_parses_qwen_native_text_tool_call(self):
        text = """I should search first.
<tool_call>
<function=search>
<parameter=query>
Ada &amp; Charles
</parameter>
<parameter=client_context>test</parameter>
</function>
</tool_call>"""

        calls, residual = _parse_text_tool_calls(text)

        self.assertEqual(residual, "I should search first.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "search")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"query": "Ada & Charles", "client_context": "test"},
        )

    def test_parses_hermes_json_text_tool_call(self):
        text = '<tool_call>{"name":"search","arguments":{"query":"Ada"}}</tool_call>'

        calls, residual = _parse_text_tool_calls(text)

        self.assertEqual(residual, "")
        self.assertEqual(calls[0]["function"]["name"], "search")
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]), {"query": "Ada"}
        )

    def test_rejects_partial_tool_call(self):
        text = "<tool_call><function=search><parameter=query>unfinished"

        calls, residual = _parse_text_tool_calls(text)

        self.assertEqual(calls, [])
        self.assertEqual(residual, text)

    def test_requires_exact_answer_and_numeric_confidence(self):
        valid = "Explanation: Evidence.\nExact Answer: Ada\nConfidence: 91%"
        missing = "Explanation: Evidence.\nExact Answer: Ada"

        self.assertTrue(_has_valid_final_answer(valid))
        self.assertFalse(_has_valid_final_answer(missing))

    def test_textual_tool_call_executes_then_completes(self):
        client = _FakeClient(
            [
                _FakeResponse(
                    "<tool_call><function=search><parameter=query>Ada</parameter>"
                    "</function></tool_call>"
                ),
                _FakeResponse(
                    "Explanation: The evidence identifies Ada. [42]\n"
                    "Exact Answer: Ada\nConfidence: 95%"
                ),
            ]
        )

        messages, usage, status = run_conversation_with_tools(
            client=client,
            model="test",
            initial_messages=[{"role": "user", "content": "Who?"}],
            chat_tools=[],
            tool_handler=_FakeToolHandler(),
            max_iterations=3,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1})
        self.assertTrue(any(message.get("role") == "tool" for message in messages))

    def test_evidence_notes_hide_raw_result_from_planner_and_keep_audit_copy(self):
        note = (
            "EVIDENCE NOTE for search #1\n"
            "Constraints in question: C1 identity\n"
            "Docs worth attention:\n- [42] supports C1 (quote: \"Ada\")\n"
            "CANDIDATE_TABLE_JSON: {\"candidates\":[{\"entity\":\"Ada\",\"constraints\":{\"C1\":{\"status\":\"supported\",\"docids\":[\"42\"]}}}]}"
        )
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"})),
                _FakeResponse(note),
                _FakeResponse(
                    "Explanation: The evidence identifies Ada. [42]\n"
                    "Exact Answer: Ada\nConfidence: 95%"
                ),
            ]
        )
        diagnostics = {}

        messages, _, status = run_conversation_with_tools(
            client,
            "test",
            [{"role": "user", "content": "Who is the person?"}],
            [],
            _FakeToolHandler(),
            max_iterations=3,
            evidence_notes=True,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        tool_message = next(message for message in messages if message.get("role") == "tool")
        self.assertIn("EVIDENCE NOTE", tool_message["content"])
        self.assertEqual(tool_message["_raw_tool_output"], '[{"docid": "42", "text": "Ada"}]')
        self.assertEqual(diagnostics["evidence_note_calls"], 1)
        self.assertEqual(diagnostics["candidate_table"]["Ada"]["constraints"]["C1"]["status"], "supported")
        self.assertEqual(client.chat.completions.requests[1]["tools"], [])
        self.assertEqual(client.chat.completions.requests[1]["temperature"], 0)
        self.assertNotIn("Ada\"}]", client.chat.completions.requests[2]["messages"][-1]["content"])

    def test_fresh_final_replaces_disagreeing_cited_answer(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"})),
                _FakeResponse(
                    "Explanation: Initial reading. [42]\nExact Answer: Ada\nConfidence: 60%"
                ),
                _FakeResponse(
                    "Explanation: Fresh evidence review. [42]\n"
                    "Exact Answer: Grace\nConfidence: 90%"
                ),
            ]
        )
        diagnostics = {}

        messages, _, status = run_conversation_with_tools(
            client,
            "test",
            [{"role": "user", "content": "Who is the person?"}],
            [],
            _FakeToolHandler(),
            max_iterations=3,
            fresh_final_answer=True,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(diagnostics["conversation_final_answer"], "Ada")
        self.assertEqual(diagnostics["fresh_final_answer"], "Grace")
        self.assertTrue(diagnostics["fresh_final_used"])
        normalized = _normalize_chat_messages(messages)
        self.assertEqual(normalized[-1]["output"], "Explanation: Fresh evidence review. [42]\nExact Answer: Grace\nConfidence: 90%")
        self.assertTrue(client.chat.completions.requests[2]["extra_body"]["chat_template_kwargs"]["enable_thinking"])

    def test_forces_final_turn_after_tool_budget(self):
        client = _FakeClient(
            [
                _FakeResponse(
                    "<tool_call><function=search><parameter=query>Ada</parameter>"
                    "</function></tool_call>"
                ),
                _FakeResponse(
                    "Explanation: Evidence. [42]\nExact Answer: Ada\nConfidence: 95%"
                ),
            ]
        )

        _, usage, status = run_conversation_with_tools(
            client=client,
            model="test",
            initial_messages=[{"role": "user", "content": "Who?"}],
            chat_tools=[],
            tool_handler=_FakeToolHandler(),
            max_iterations=3,
            max_tool_calls=1,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1})
        self.assertEqual(client.chat.completions.requests[0]["tool_choice"], "auto")
        self.assertEqual(client.chat.completions.requests[1]["tool_choice"], "none")
        self.assertTrue(
            client.chat.completions.requests[0]["extra_body"]["chat_template_kwargs"]
            ["enable_thinking"]
        )
        self.assertFalse(
            client.chat.completions.requests[1]["extra_body"]["chat_template_kwargs"]
            ["enable_thinking"]
        )

    def test_stop_without_answer_is_not_completed(self):
        client = _FakeClient([_FakeResponse("I should investigate this further.")])

        _, usage, status = run_conversation_with_tools(
            client=client,
            model="test",
            initial_messages=[{"role": "user", "content": "Who?"}],
            chat_tools=[],
            tool_handler=_FakeToolHandler(),
            max_iterations=1,
        )

        self.assertEqual(status, "incomplete_no_final_answer")
        self.assertEqual(usage, {})

    def test_truncated_textual_tool_call_is_not_completed(self):
        client = _FakeClient(
            [
                _FakeResponse(
                    "<tool_call><function=search><parameter=query>unfinished",
                    finish_reason="length",
                ),
                _FakeResponse(
                    "<tool_call><function=search><parameter=query>unfinished",
                    finish_reason="length",
                ),
            ]
        )

        _, usage, status = run_conversation_with_tools(
            client=client,
            model="test",
            initial_messages=[{"role": "user", "content": "Who?"}],
            chat_tools=[],
            tool_handler=_FakeToolHandler(),
            max_iterations=1,
        )

        self.assertEqual(status, "incomplete_length")
        self.assertEqual(usage, {})

    def test_length_turn_retries_without_thinking_and_preserves_reasoning(self):
        client = _FakeClient(
            [
                _FakeResponse(
                    None,
                    finish_reason="length",
                    reasoning="Long investigation that exhausted the turn.",
                ),
                _FakeResponse(
                    "Explanation: Best available evidence.\n"
                    "Exact Answer: Ada\nConfidence: 70%"
                ),
            ]
        )

        messages, _, status = run_conversation_with_tools(
            client=client,
            model="test",
            initial_messages=[{"role": "user", "content": "Who?"}],
            chat_tools=[],
            tool_handler=_FakeToolHandler(),
            max_iterations=1,
        )

        self.assertEqual(status, "completed")
        self.assertTrue(messages[1]["_diagnostic_only"])
        self.assertEqual(
            _normalize_chat_messages(messages)[0]["output"],
            ["Long investigation that exhausted the turn."],
        )
        requests = client.chat.completions.requests
        self.assertTrue(
            requests[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"]
        )
        self.assertFalse(
            requests[1]["extra_body"]["chat_template_kwargs"]["enable_thinking"]
        )
        self.assertEqual(requests[1]["messages"], requests[0]["messages"])

    def test_get_document_is_bounded_by_token_cap(self):
        handler = ChatSearchToolHandler.__new__(ChatSearchToolHandler)
        handler.searcher = _FakeSearcher()
        handler.tokenizer = _FakeTokenizer()
        handler.tool_name = "search"
        handler.tool_param = "query"
        handler.document_max_tokens = 3

        result = json.loads(handler.execute_tool("get_document", {"docid": "42"}))

        self.assertEqual(result["text"], "one two three")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["original_tokens"], 5)
        self.assertEqual(result["returned_tokens"], 3)

    def test_persistence_uses_atomic_query_id_filename(self):
        messages = [
            {"role": "user", "content": "Who?"},
            {
                "role": "assistant",
                "content": "Explanation: Evidence.\nExact Answer: Ada\nConfidence: 95%",
                "tool_calls": None,
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            _persist_response(
                tmpdir,
                "test-model",
                messages,
                {},
                "completed",
                query_id="770",
            )

            output_path = Path(tmpdir) / "run_qid_770.json"
            self.assertTrue(output_path.is_file())
            self.assertEqual(json.loads(output_path.read_text())["query_id"], "770")
            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])

    def test_refusal_guard_recovers_once_with_required_tool(self):
        client = _FakeClient(
            [
                _FakeResponse("Explanation: Not enough evidence.\nExact Answer: None\nConfidence: 0%"),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"})),
                _FakeResponse("Explanation: Found it. [42]\nExact Answer: Ada\nConfidence: 90%"),
            ]
        )
        diagnostics = {}

        _, usage, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=5, max_tool_calls=8,
            early_final_guard="refusal", diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1})
        self.assertEqual(client.chat.completions.requests[1]["tool_choice"], "required")
        self.assertTrue(
            all(
                not key.startswith("_")
                for message in client.chat.completions.requests[1]["messages"]
                for key in message
            )
        )
        self.assertEqual(diagnostics["early_final_recoveries"], 1)
        self.assertEqual(diagnostics["early_final_trigger_reasons"], ["explicit_refusal"])

    def test_refusal_guard_respects_remaining_budget_threshold(self):
        client = _FakeClient(
            [_FakeResponse("Explanation: Unknown.\nExact Answer: None\nConfidence: 0%")]
        )
        diagnostics = {}

        _, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_tool_calls=3,
            early_final_guard="refusal", early_final_min_remaining=4,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(len(client.chat.completions.requests), 1)
        self.assertEqual(diagnostics["early_final_recoveries"], 0)

    def test_refusal_guard_enforces_maximum_recovery_count(self):
        refusal = "Explanation: Unknown.\nExact Answer: None\nConfidence: 0%"
        client = _FakeClient(
            [
                _FakeResponse(refusal),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"})),
                _FakeResponse(refusal),
            ]
        )
        diagnostics = {}

        _, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=5, max_tool_calls=8,
            early_final_guard="refusal", early_final_max_recoveries=1,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(len(client.chat.completions.requests), 3)
        self.assertEqual(diagnostics["early_final_recoveries"], 1)

    def test_conservative_guard_does_not_trigger_on_caveat_alone(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("get_document", {"docid": "42"})),
                _FakeResponse(
                    "Explanation: I could not verify a peripheral date, but the identity is direct. [42]\n"
                    "Exact Answer: Ada\nConfidence: 85%"
                ),
            ]
        )
        diagnostics = {}

        _, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=3, max_tool_calls=8,
            early_final_guard="conservative", diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(len(client.chat.completions.requests), 2)
        self.assertEqual(diagnostics["early_final_recoveries"], 0)

    def test_conservative_guard_recovers_caveat_without_opened_document(self):
        client = _FakeClient(
            [
                _FakeResponse(
                    "Explanation: I could not verify the decisive date.\n"
                    "Exact Answer: Ada\nConfidence: 70%"
                ),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada date"})),
                _FakeResponse("Explanation: Verified. [42]\nExact Answer: Ada\nConfidence: 90%"),
            ]
        )
        diagnostics = {}

        _, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=5, max_tool_calls=8,
            early_final_guard="conservative", diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(
            diagnostics["early_final_trigger_reasons"],
            ["evidence_gap_without_opened_document"],
        )

    def test_duplicate_search_does_not_consume_productive_budget(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"}, "c1")),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "  ada  "}, "c2")),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada date"}, "c3")),
                _FakeResponse("Explanation: Done. [42]\nExact Answer: Ada\nConfidence: 90%"),
            ]
        )
        diagnostics = {}

        _, usage, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=5, max_tool_calls=2,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 2})
        self.assertEqual(diagnostics["productive_tool_calls"], 2)
        self.assertEqual(diagnostics["rejected_duplicate_calls"], 1)

    def test_two_consecutive_duplicates_inject_strategy_guidance(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"}, "c1")),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "ada"}, "c2")),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": " ADA "}, "c3")),
                _FakeResponse("Explanation: Done. [42]\nExact Answer: Ada\nConfidence: 90%"),
            ]
        )

        messages, usage, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=5, max_tool_calls=2,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1})
        self.assertTrue(
            any(
                message.get("_synthetic_control")
                == "duplicate_stagnation_guidance"
                for message in messages
            )
        )

    def test_server_novelty_passes_seen_docids_and_injects_stagnation_guidance(self):
        handler = _StatefulChatHandler()
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"}, "c1")),
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada date"}, "c2")),
                _FakeResponse("Explanation: Done. [42]\nExact Answer: Ada\nConfidence: 90%"),
            ]
        )
        diagnostics = {}

        messages, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [], handler,
            max_iterations=4, max_tool_calls=8, retrieval_novelty="server",
            stagnation_low_novelty_threshold=3,
            stagnation_consecutive_searches=2, diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(handler.search_with_metadata_calls[0][1], set())
        self.assertEqual(handler.search_with_metadata_calls[1][1], {"42"})
        self.assertTrue(any(m.get("_synthetic_control") == "stagnation_guidance" for m in messages))
        self.assertEqual(diagnostics["stagnation_guidance_count"], 1)

    def test_server_novelty_has_safe_legacy_local_fallback(self):
        handler = ChatSearchToolHandler.__new__(ChatSearchToolHandler)
        handler.searcher = _LegacySearchOnly()
        handler.k = 10
        handler.snippet_max_tokens = None
        handler.tokenizer = None

        result, metadata = handler.execute_search_with_state(
            "query", exclude_docids={"seen"}, novelty_mode="server",
            seen_anchor_count=0, remaining_tool_calls=4, low_novelty_streak=0,
        )

        payload = json.loads(result)
        self.assertEqual([item["docid"] for item in payload["documents"]], ["new"])
        self.assertFalse(metadata["exclusions_applied"])
        self.assertTrue(metadata["fallback_applied"])

    def test_seen_snippet_document_can_still_be_opened(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"}, "c1")),
                _FakeResponse(None, tool_calls=_tool_call("get_document", {"docid": "42"}, "c2")),
                _FakeResponse("Explanation: Verified. [42]\nExact Answer: Ada\nConfidence: 90%"),
            ]
        )
        diagnostics = {}

        _, usage, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=4, max_tool_calls=8,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1, "get_document": 1})
        self.assertEqual(diagnostics["opened_docids"], ["42"])

    def test_blank_forced_final_uses_one_temperature_zero_emergency_finalizer(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"})),
                _FakeResponse(""),
                _FakeResponse("Explanation: Best evidence. [42]\nExact Answer: Ada\nConfidence: 70%"),
            ]
        )
        diagnostics = {}

        _, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=3, max_tool_calls=1,
            emergency_finalizer=True, diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(len(client.chat.completions.requests), 3)
        self.assertEqual(client.chat.completions.requests[2]["temperature"], 0)
        self.assertEqual(client.chat.completions.requests[2]["tools"], [])
        self.assertTrue(
            all(
                not key.startswith("_")
                for message in client.chat.completions.requests[2]["messages"]
                for key in message
            )
        )
        self.assertTrue(diagnostics["emergency_finalizer_attempted"])
        self.assertTrue(diagnostics["emergency_finalizer_succeeded"])

    def test_failed_emergency_finalizer_stays_incomplete(self):
        client = _FakeClient(
            [
                _FakeResponse(None, tool_calls=_tool_call("search", {"query": "Ada"})),
                _FakeResponse(""),
                _FakeResponse("still blank"),
            ]
        )

        _, _, status = run_conversation_with_tools(
            client, "test", [{"role": "user", "content": "Who?"}], [],
            _StatefulChatHandler(), max_iterations=3, max_tool_calls=1,
            emergency_finalizer=True,
        )

        self.assertEqual(status, "incomplete_emergency_finalizer")
        self.assertEqual(len(client.chat.completions.requests), 3)

    def test_context_compaction_preserves_audit_but_shrinks_next_request(self):
        handler = _StatefulChatHandler(
            search_results=[
                {
                    "docid": "42",
                    "text": "decisive evidence " * 250,
                    "score": 1.0,
                }
            ]
        )
        handler.tokenizer = _FakeTokenizer()
        client = _FakeClient(
            [
                _FakeResponse(
                    None,
                    tool_calls=_tool_call("search", {"query": "Ada identity"}),
                ),
                _FakeResponse(
                    "COMPACTED RESEARCH LEDGER\n"
                    "Candidate: Ada [42].\n"
                    "Unresolved: verify date.\n"
                    "Next action: open document 42."
                ),
                _FakeResponse(
                    "Explanation: Best evidence. [42]\n"
                    "Exact Answer: Ada\nConfidence: 90%"
                ),
            ]
        )
        diagnostics = {}

        messages, usage, status = run_conversation_with_tools(
            client,
            "test",
            [{"role": "user", "content": "Who?"}],
            [],
            handler,
            max_iterations=3,
            max_tokens=100,
            max_tool_calls=4,
            context_compaction=True,
            context_window_tokens=2000,
            context_compaction_trigger_tokens=400,
            context_compaction_keep_tool_rounds=0,
            context_compaction_max_tokens=100,
            context_compaction_reserve_tokens=100,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "completed")
        self.assertEqual(usage, {"search": 1})
        self.assertEqual(diagnostics["context_compaction_count"], 1)
        self.assertLess(
            diagnostics["context_tokens_after_last_compaction"],
            diagnostics["context_tokens_before_last_compaction"],
        )
        self.assertEqual(client.chat.completions.requests[1]["tools"], [])
        continuation_messages = client.chat.completions.requests[2]["messages"]
        self.assertFalse(any(item.get("role") == "tool" for item in continuation_messages))
        self.assertTrue(
            any("COMPACTED RESEARCH LEDGER" in item.get("content", "") for item in continuation_messages)
        )
        self.assertTrue(any(item.get("_compacted_out") for item in messages))
        self.assertTrue(
            any(item["type"] == "tool_call" for item in _normalize_chat_messages(messages))
        )

    def test_context_compaction_stops_if_triggered_too_late(self):
        handler = _StatefulChatHandler(
            search_results=[
                {"docid": "42", "text": "evidence " * 700, "score": 1.0}
            ]
        )
        handler.tokenizer = _FakeTokenizer()
        client = _FakeClient(
            [
                _FakeResponse(
                    None,
                    tool_calls=_tool_call("search", {"query": "Ada identity"}),
                )
            ]
        )
        diagnostics = {}

        _, _, status = run_conversation_with_tools(
            client,
            "test",
            [{"role": "user", "content": "Who?"}],
            [],
            handler,
            max_iterations=3,
            max_tokens=100,
            max_tool_calls=4,
            context_compaction=True,
            context_window_tokens=700,
            context_compaction_trigger_tokens=350,
            context_compaction_keep_tool_rounds=0,
            context_compaction_max_tokens=100,
            context_compaction_reserve_tokens=100,
            diagnostics_out=diagnostics,
        )

        self.assertEqual(status, "incomplete_context_compaction_too_late")
        self.assertEqual(len(client.chat.completions.requests), 1)
        self.assertEqual(diagnostics["context_compaction_failures"], 1)

    def test_persistence_includes_optional_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _persist_response(
                tmpdir, "test-model", [], {}, "incomplete", query_id="1",
                diagnostics={"productive_tool_calls": 2},
            )
            record = json.loads((Path(tmpdir) / "run_qid_1.json").read_text())
            self.assertEqual(record["diagnostics"]["productive_tool_calls"], 2)


if __name__ == "__main__":
    unittest.main()
