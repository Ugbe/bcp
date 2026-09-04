# Vast model serving and tool-call contract

Use this checklist before starting another BrowseComp run. Replace the model ID,
port, tensor-parallel size, and GPU memory setting for the Vast instance.

## 1. Start a tool-aware vLLM server

The served vLLM version must support `Qwen3_5ForConditionalGeneration`, the
`qwen3_xml` tool parser, and the `qwen3` reasoning parser.

```bash
vllm --version
vllm serve --help | grep -E "tool-call-parser|reasoning-parser"
```

Recommended launch shape:

```bash
vllm serve "CrowtherLabs/Atom-Electron-1.2-9B-Refined" \
  --served-model-name "CrowtherLabs/Atom-Electron-1.2-9B-Refined" \
  --host 0.0.0.0 \
  --port 8000 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3
```

If `qwen3_xml` or Qwen3.5 is not recognized, upgrade vLLM rather than removing
the parser flags. The local benchmark client has a defensive text parser, but a
correctly configured server should return OpenAI-compatible
`message.tool_calls` and `finish_reason="tool_calls"` itself.

Do not override the model's bundled chat template with a Qwen2.5 template.

## 2. Update the local endpoint configuration

Set these values in the repository `.env` without committing credentials:

```dotenv
MODEL_BASE_URL=http://HOST:PORT/v1
MODEL_API_KEY=YOUR_KEY
MODEL_NAME=CrowtherLabs/Atom-Electron-1.2-9B-Refined
```

## 3. Run the strict connectivity and tool contract

From the BrowseComp-Plus repository on Windows:

```powershell
.venv\Scripts\python.exe scripts_evaluation\smoke_test.py
```

The model section must print both:

```text
OK - replied: 'PONG'
structured tool call OK - finish_reason='tool_calls', name='search', ...
```

If it reports a textual `<tool_call>`, the model is generating the correct
syntax but Vast is not parsing it. Recheck `--enable-auto-tool-choice` and
`--tool-call-parser qwen3_xml`.

## 4. Run one end-to-end search query

Use a new output directory so old malformed runs are not treated as completed:

```powershell
$env:PYTHONPATH="."
.venv\Scripts\python.exe search_agent\chat_client.py `
  --query "Who invented the telephone?" `
  --model $env:MODEL_NAME `
  --model-url $env:MODEL_BASE_URL `
  --model-api-key $env:MODEL_API_KEY `
  --searcher-type remote `
  --output-dir runs\contract-test `
  --max-iterations 8 `
  --max-tokens 4096 `
  --temperature 0.6 `
  --top-p 0.95 `
  --top-k 20 `
  --repetition-penalty 1.05 `
  --k 5 `
  --snippet-max-tokens 512 `
  --query-template QUERY_TEMPLATE_NO_GET_DOCUMENT `
  --verbose
```

The resulting JSON must have:

- `status` equal to `completed`;
- `tool_call_counts.search` greater than zero;
- at least one `tool_call` result item;
- non-empty `retrieved_docids`;
- an output containing `Exact Answer:` and a numeric `Confidence:`.

Only after this contract passes should a TSV benchmark be started.
