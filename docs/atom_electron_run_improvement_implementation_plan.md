# Atom Electron BrowseComp Run-Improvement Implementation Plan

Date: 2026-09-02  
Repository: `C:\Users\Dell\BrowseComp-Plus`  
Primary run audited: `atom-electron-1.3-9b-full-830-docs-v2-128k`

## 1. Objective

Raise trustworthy end-to-end BrowseComp accuracy from the observed 438/830
(52.77%) into the low-to-mid 60s without hiding failures, contaminating the
benchmark, or spending the full 24-tool ceiling on every question.

The work must address five distinct failure classes:

1. premature voluntary finals and early refusals;
2. repeated retrieval results and search stagnation;
3. weak use of retrieved evidence, especially failure to open decisive documents;
4. incomplete-run finalization and status accounting;
5. judge equivalence/normalization errors.

Model retraining is a later workstream. The first implementation must determine
how much can be recovered through prompt, harness, retrieval, decoding, and
evaluation changes while keeping the existing `Qwen/Qwen3.5-9B` plus
`CrowtherLabs/Atom-Electron-1.3-9B` pair fixed.

## 2. Evidence and constraints from the audit

- 830 unique qids exist despite sparse numeric qids extending to 1266.
- 805 runs completed; 25 were incomplete.
- 128 runs reached 24 tool calls and scored 14.06%.
- 698 completed before 24 calls and scored 60.17%; therefore 24 calls is a ceiling,
  not a target.
- 73 runs returned an explicit no-answer before 24 calls.
- 176 early candidate answers contained an evidence-gap warning; 70 were judged
  wrong, but 106 were correct. Caveat language alone cannot be a hard retry rule.
- 58,764/101,690 retrieval slots (57.79%) repeated a docid already returned in the
  same conversation.
- 130 completed wrong runs had the literal gold answer in tool output.
- 46 completed wrong runs had 100% qrel evidence recall.
- 21 high-confidence and 12 probable/policy-dependent judge false negatives were
  identified.

Source artifacts:

- `analysis/atom-electron-1.3-9b-830-audit/REPORT.md`
- `analysis/atom-electron-1.3-9b-830-audit/summary.json`
- `analysis/atom-electron-1.3-9b-830-audit/early_stop_review.csv`
- `analysis/atom-electron-1.3-9b-830-audit/per_run_metrics.csv`

## 3. Non-negotiable design principles

1. Do not require a fixed minimum number of searches.
2. Do not charge locally rejected exact-duplicate actions against the productive
   tool budget.
3. Keep every behavior change behind explicit CLI options until an A/B run proves
   it beneficial.
4. Preserve baseline-compatible output records while adding diagnostic metadata.
5. Keep all state local to one qid; the tool handler is shared across worker
   threads and must not hold mutable per-query state.
6. Never send a large natural-language list of seen document IDs in the prompt.
   Use structured API state and compact result metadata.
7. A document previously returned as a snippet must remain accessible through
   `get_document`.
8. Separate run completion, model-output parsing, judge-call success, and
   judge-answer parsing into distinct status fields.
9. Do not tune against protected benchmark gold answers. Use held-out development
   questions and failure-class replay sets.
10. Do not change the validated LoRA/base pairing during the harness A/B.

## 4. Target architecture

### 4.1 Per-conversation research state

Add an explicit `ResearchState` dataclass or equivalent object created inside
`run_conversation_with_tools` with at least:

```text
seen_search_queries: set[str]
seen_docids: set[str]
opened_docids: set[str]
productive_tool_calls: int
rejected_duplicate_calls: int
low_novelty_streak: int
early_final_attempts: int
early_final_recoveries: int
emergency_finalizer_attempted: bool
last_novel_count: int | None
last_repeated_count: int | None
```

Persist non-sensitive diagnostic counters in each run record. Do not persist
hidden prompts or credentials.

### 4.2 Productive versus rejected calls

The 24-call ceiling must count calls that execute a real search or document fetch.
An exact duplicate search rejected locally and a duplicate `get_document` request
must be recorded diagnostically but must not reduce the productive budget.

Prevent infinite loops separately: after two rejected duplicate actions in one
conversation, inject a compact strategy message and, if supported, require one
materially different tool call. Retain `max_iterations` as the outer safety bound.

### 4.3 Retrieval novelty API

Extend `POST /retrieve` compatibly:

```json
{
  "query": "...",
  "exclude_docids": ["12", "47"],
  "k": 10,
  "seen_anchor_count": 0
}
```

Recommended response:

```json
{
  "result": [...],
  "metadata": {
    "requested_k": 10,
    "novel_count": 10,
    "repeated_count": 0,
    "excluded_count": 23,
    "candidate_pool_exhausted": false,
    "exclusions_applied": true
  }
}
```

Requirements:

- `exclude_docids` is optional and bounded (for example, maximum 2,000 IDs).
- Old clients sending only `query` continue to work.
- Filter excluded docids from the fused candidate pool before expensive reranking
  in hard-novelty mode.
- Over-fetch sufficiently so exclusions do not merely shrink the returned list.
- A later A/B mode may return eight novel results and up to two high-ranking seen
  anchors. Implement this only if it does not require reranking an unbounded list.
- If the active remote service has not yet been upgraded, the local client must
  detect `exclusions_applied=false` or missing metadata and apply a safe local
  fallback. The fallback may return fewer than ten documents; it must say so.

### 4.4 Compact search-tool result contract

The model-facing search result should include novelty metadata once per response,
not repeated inside every hit:

```json
{
  "documents": [...],
  "retrieval_state": {
    "novel_count": 6,
    "repeated_count": 4,
    "low_novelty_streak": 2,
    "remaining_tool_calls": 9
  }
}
```

For backward compatibility, make this contract opt-in during the A/B. Update the
normalizer/audit code to understand both the legacy list and wrapped object.

### 4.5 Early-final controller

Do not use a phrase regex as the final decision-maker. Implement a two-stage gate:

1. Cheap deterministic screen:
   - explicit no-answer/refusal;
   - evidence-gap language;
   - zero `get_document` calls despite a candidate and unused budget;
   - low confidence combined with an unresolved-clue statement;
   - invalid/empty final output.
2. A compact verifier/recovery turn, using the same model at temperature 0 unless
   a separate verifier is configured. Give it the question, proposed answer,
   cited/opened docids, remaining productive budget, and a concise instruction:
   either accept the answer as uniquely supported or identify one material missing
   constraint and issue the single most discriminating tool call.

The first implementation may use one model recovery turn rather than a separate
service. It must be bounded:

- default maximum recoveries per qid: 1;
- recover only when at least four productive calls remain;
- never recover a final solely because it contains “couldn't find”;
- always recover an explicit no-answer when the threshold is met;
- append the rejected early-final attempt as diagnostic-only so the model does not
  treat it as an accepted answer;
- expose a clear synthetic user/developer message explaining the missing work;
- prefer `tool_choice="required"` for the recovery request when the server supports
  it, with a compatibility fallback to `auto` plus an explicit instruction;
- record `early_final_guard_triggered`, trigger reason, and recovery outcome.

Until a semantic constraint verifier exists, use conservative rules: automatic
recovery for explicit refusals; for caveated candidates, recover only when there is
also no opened supporting document, low reported confidence, or the final says the
answer is non-definitive/only a guess. This avoids disturbing the 106 correct
caveated answers unnecessarily.

### 4.6 Constraint ledger

Update the research prompt so the model internally tracks:

```text
candidate
verified identity-critical constraints with docids
contradicted constraints
unverified identity-critical constraints
next discriminating query
```

Definitions:

- identity-critical: a clue that can distinguish the candidate from plausible
  alternatives or is directly requested by the question;
- peripheral: a clue that corroborates context but does not change identity.

Finalization is allowed when the candidate is uniquely supported and no unresolved
identity-critical constraint can plausibly change the answer. The final public
format remains `Explanation`, `Exact Answer`, and `Confidence`; do not expose a
large scratch ledger in the final answer.

### 4.7 Decisive-document verification

When a candidate is supported only by search snippets and budget remains, the
recovery instruction should request `get_document` for the strongest supporting
docid before finalization. This is a policy, not an unconditional requirement:

- skip when no `get_document` tool is registered;
- skip when the answer is directly and unambiguously stated in a complete snippet;
- require it when a truncated snippet, ambiguous name, date, title, or competing
  candidate is decisive.

### 4.8 Stagnation controller

Define low novelty as at most three novel documents in a top-ten response. After
two consecutive low-novelty searches:

1. inject a compact stagnation event with tried query concepts;
2. ask for a candidate comparison and one discriminating query;
3. prevent an exact duplicate query;
4. prefer opening a promising existing document over another broad search;
5. if no discriminating action exists, allow best-candidate finalization.

Do not blindly force more searches after stagnation.

### 4.9 Emergency finalizer

If the normal final turn is empty, malformed, or length-limited after the tool
ceiling, make exactly one tools-disabled request with:

- temperature 0;
- 256–512 output tokens;
- the original question;
- a compact list of candidate/evidence notes already present in the conversation;
- the exact output contract.

Record the first failure and finalizer result separately. Do not overwrite the
diagnostic trace. If the emergency finalizer also fails, retain an explicit
incomplete status.

### 4.10 Judge normalization and status separation

Before calling the LLM judge, add a deterministic comparison layer that can accept
only safe equivalences:

- Unicode normalization and mojibake repair where deterministic;
- case, whitespace, punctuation, and hyphen normalization;
- honorific removal;
- middle-initial/full-middle-name equivalence;
- common legal suffix removal (`Inc.`, `Ltd.`, `S.A.`, `Pvt. Ltd.`);
- simple singular/plural equivalence when the entity remains unambiguous.

Do not automatically accept:

- different dates or quantities;
- different title prepositions unless an alias list says they are equivalent;
- surname-only responses when the question requires a full name;
- historical/current organization names without a per-qid or curated alias rule;
- semantically related but non-identical titles.

For near matches not resolved deterministically, call a second adjudication prompt
that asks only whether the strings identify the same entity. Persist:

```text
run_status
model_answer_parse_status
judge_call_status
judge_parse_status
raw_judge_correct
normalized_correct
final_correct
normalization_rule
```

Never label an unfinished model run as a judge parser failure.

## 5. CLI and compatibility surface

Add explicit flags to `search_agent/chat_client.py` (names may be adjusted for
repository style, but semantics must remain):

```text
--early-final-guard {off,refusal,conservative}
--early-final-min-remaining 4
--early-final-max-recoveries 1
--stagnation-low-novelty-threshold 3
--stagnation-consecutive-searches 2
--retrieval-novelty {off,local,server}
--retrieval-seen-anchor-count 0
--emergency-finalizer
--emergency-finalizer-max-tokens 384
--context-compaction
--context-window-tokens 131072
--context-compaction-trigger-tokens 98304
--context-compaction-keep-tool-rounds 2
--context-compaction-max-tokens 1536
--context-compaction-reserve-tokens 8192
```

Recommended first A/B treatment:

```text
temperature=0
early-final-guard=refusal
retrieval-novelty=server (or local fallback)
seen-anchor-count=0
emergency-finalizer=true
```

Then test `early-final-guard=conservative` and the 8-new/2-anchor policy separately.
Do not change every variable in a single experiment.

## 6. File-level work plan

### Runner and prompting

- `search_agent/chat_client.py`
  - introduce per-qid research state;
  - count productive calls separately from rejected duplicates;
  - implement early-final recovery and emergency finalizer;
  - pass seen docids to novelty-aware search;
  - add CLI flags and persisted diagnostics.
- `search_agent/prompts.py`
  - define identity-critical constraints and stopping policy;
  - require discriminating queries and decisive-document verification;
  - keep final answer schema unchanged.
- `tests/test_chat_client.py`
  - add deterministic fake-response tests for every controller branch.

### Retrieval client/server

- `searcher/searchers/base.py`
  - add a backward-compatible optional exclusion/metadata interface.
- `searcher/searchers/remote_api_searcher.py`
  - send structured exclusions;
  - parse metadata;
  - provide a clear fallback for an old server.
- `bcp-replication/searcher/search_r1_server.py`
  - extend the request/response schema compatibly;
  - validate exclusion limits.
- `bcp-replication/searcher/searchers/hybrid_searcher.py`
  - filter/over-fetch at the fused candidate stage;
  - return `k` novel results when the candidate pool permits.
- new or existing retrieval unit tests
  - verify exclusions, over-fetching, metadata, and legacy requests.

### Evaluation

- `scripts_evaluation/evaluate_with_azure.py`
  - separate statuses;
  - add deterministic safe normalization;
  - optionally rejudge unresolved near matches.
- evaluation unit tests
  - encode all 42 reviewed judge candidates as regression fixtures with explicit
    accepted/rejected/policy-dependent expectations.

### Analysis and documentation

- `scripts_analysis/analyze_atom_electron_run.py`
  - support wrapped retrieval output;
  - report controller triggers and outcomes;
  - report productive versus rejected calls.
- `scripts_evaluation/smoke_test.py`
  - test exclusion-capable retrieval when advertised;
  - test recovery-compatible structured tool calling;
  - do not fail legacy retrieval unless treatment mode requires exclusions.
- `AGENTS.md`
  - record every substantive implementation/test/deployment step.

## 7. Required unit tests

Runner tests must cover:

1. a correct early final passes with guard off;
2. a correct early final passes under refusal-only guard;
3. an early explicit refusal with budget remaining triggers one recovery;
4. no recovery occurs below the remaining-budget threshold;
5. maximum recovery count is enforced;
6. a conservative caveat plus no opened documents triggers recovery;
7. caveat language alone does not trigger recovery;
8. a rejected duplicate search does not consume productive budget;
9. two duplicate/low-novelty actions inject stagnation guidance;
10. seen docids are passed as structured exclusions;
11. an old retrieval server falls back safely;
12. `get_document` remains available for seen snippet docids;
13. blank forced-final output triggers one temperature-zero emergency finalizer;
14. a failed emergency finalizer remains incomplete;
15. persisted records include backward-compatible result/status fields plus new
    diagnostics.

Retrieval tests must cover:

1. legacy `{query}` requests;
2. exclusion of all requested docids;
3. exactly `k` novel results when enough candidates exist;
4. fewer than `k` plus `candidate_pool_exhausted=true` otherwise;
5. bounded exclusion-list validation;
6. stable ranking among remaining candidates;
7. optional anchor behavior;
8. authentication behavior unchanged.

Judge tests must cover every manually reviewed case and ensure that numeric/date or
meaningful title differences remain rejected.

## 8. Verification sequence

Run in this order:

```powershell
.venv\Scripts\python.exe -m unittest tests.test_chat_client -v
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe scripts_analysis\analyze_atom_electron_run.py
.venv\Scripts\python.exe scripts_evaluation\smoke_test.py
```

The smoke test contacts remote services and must only be claimed successful after
direct execution. Unit tests must not contact the network.

Before a full benchmark, perform a 10–20-query canary containing:

- early refusals where gold was already retrieved;
- caveated wrong answers with 100% recall;
- duplicate-loop runs;
- incomplete-length runs;
- correct caveated early answers that must not regress.

## 9. A/B evaluation design

Build a fixed diagnostic set of about 100 qids, stratified and frozen before
running treatments:

```text
20 early explicit refusals
20 wrong caveated candidates
15 high-recall wrong answers
15 duplicate/stagnation failures
10 zero-recall failures
10 incomplete runs
10 correct caveated early answers (regression controls)
```

Run at least these arms with identical model weights and retrieval corpus:

| Arm | Temperature | Early guard | Novelty | Finalizer |
|---|---:|---|---|---|
| A baseline replay | 0.6 | off | off | off |
| B deterministic | 0 | off | off | on |
| C refusal recovery | 0 | refusal | off | on |
| D novelty | 0 | refusal | hard/server | on |
| E conservative gate | 0 | conservative | hard/server | on |
| F anchor variant | 0 | conservative | 8-new/2-anchor | on |

Measure:

- accuracy and completion rate;
- productive and rejected calls;
- unique documents per search and per qid;
- qrel recall;
- `get_document` usage;
- early-final trigger, acceptance, and recovery success;
- correct-answer regressions caused by the gate;
- latency and retrieval GPU queue time;
- context/input-token growth.

Promote a treatment only if it improves accuracy without materially increasing
incomplete runs or causing more than a small, investigated regression rate among
the correct-control cohort.

## 10. Score expectations and decision gates

Do not add estimated gains mechanically because cohorts overlap. Use these as
planning ranges:

- judge normalization/status fixes: approximately +2.5 to +4 points in measured
  score;
- rerunning 25 incomplete qids with a finalizer: approximately +0.5 to +1.7 points;
- novelty/retrieval improvements: approximately +2 to +5 points;
- stopping/evidence-use controller: approximately +2 to +6 points;
- deterministic decoding and prompt improvements: uncertain, likely modest alone.

After harness/retrieval fixes, run an oracle-qrel evidence test. If the fixed model
still cannot exceed roughly the high-60s on questions supplied with complete qrel
evidence, begin a model/training workstream. If oracle synthesis improves strongly,
continue optimizing retrieval ordering and the controller before retraining.

## 11. Later training-data plan

Do not train on the protected evaluation questions or their direct parents.

1. Replace answer-string-triggered `answer_synthesis` selection with a labeled
   constraint-coverage requirement.
2. Add independent research parents, not only derived views.
3. Add paired continuations at partial states:
   - preferred: targeted discriminating tool call;
   - rejected: premature candidate final.
4. Add paired continuations at sufficient states:
   - preferred: concise grounded final;
   - rejected: redundant broad search.
5. Add examples that open decisive documents and cite them.
6. Include realistic low-novelty tool responses and teach strategy changes.
7. Require every full research trace to terminate in a usable answer.
8. Validate the new adapter against base-model, previous-adapter, and oracle-evidence
   baselines with deterministic decoding.

## 12. Completion criteria

This implementation phase is complete only when:

- all new unit tests pass;
- the old unit suite passes;
- baseline mode preserves existing behavior and record schema;
- treatment mode produces explicit novelty/controller diagnostics;
- duplicate actions no longer consume productive budget;
- early refusals can be recovered exactly once under policy;
- malformed forced finals get exactly one emergency finalization attempt;
- judge statuses are separated and safe equivalences are regression-tested;
- local smoke tests pass, and remote smoke tests are either directly verified or
  clearly documented as blocked;
- `AGENTS.md` contains exact commands, results, compatibility assumptions, and the
  next deployment action;
- no full 830-query run is started until the canary and fixed diagnostic A/B have
  been reviewed.

## 13. Implementation status — 2026-09-02

The first implementation phase described above is now present locally.

Implemented runner features:

- per-conversation `ResearchState` and persisted diagnostics;
- productive tool accounting that does not charge rejected duplicate actions;
- `off`, `refusal`, and `conservative` early-final policies;
- one bounded recovery using `tool_choice="required"` with an `auto` fallback;
- structured seen-docid exclusions and local/legacy fallback;
- low-novelty and repeated-action stagnation guidance;
- seen snippet documents remain openable through `get_document`;
- one temperature-zero tools-disabled emergency finalizer;
- internal `_diagnostic`/`_synthetic_control` keys are stripped before messages are
  serialized to the OpenAI-compatible API;
- original query templates are preserved as baseline arms, while treatment uses
  `QUERY_TEMPLATE_RESEARCH_LEDGER` or
  `QUERY_TEMPLATE_RESEARCH_LEDGER_NO_GET_DOCUMENT`.

Implemented retrieval features:

- backward-compatible `BaseSearcher.search_with_metadata`;
- structured `exclude_docids`, `k`, and `seen_anchor_count` request fields;
- bounded exclusion validation;
- candidate-stage filtering and cheap-stage over-fetch in `HybridSearcher`;
- optional anchors with stable ranking;
- server novelty metadata;
- remote-client retry/fallback when an old server rejects or ignores the new
  fields.

Implemented evaluator features:

- conservative deterministic equivalence with all 42 reviewed cases encoded as
  21 accepted, nine rejected, and 12 unresolved/policy-dependent fixtures;
- optional second Azure adjudication for unresolved near matches;
- separate run/model-parse/judge-call/judge-parse statuses;
- backward-compatible `judge_result.correct` plus canonical `final_correct`;
- model confidence preserved even when a deterministic rule avoids an Azure call;
- completed records with missing output are model-answer parse errors, not run or
  judge parse errors.

Additional artifacts:

```text
scripts_analysis/build_ab_diagnostic_set.py
topics-qrels/splits/atom_electron_failure_stratified_100.tsv
topics-qrels/splits/atom_electron_failure_stratified_100.manifest.json
tests/test_retrieval_novelty.py
tests/test_evaluate_with_azure.py
tests/fixtures/judge_equivalence_cases.csv
```

The diagnostic TSV contains exactly 100 unique qids and the manifest confirms all
seven planned non-overlapping quotas.

Final local verification:

```powershell
.venv\Scripts\python.exe -m py_compile search_agent\chat_client.py search_agent\prompts.py searcher\searchers\base.py searcher\searchers\remote_api_searcher.py bcp-replication\searcher\search_r1_server.py bcp-replication\searcher\searchers\hybrid_searcher.py scripts_evaluation\evaluate_with_azure.py scripts_evaluation\smoke_test.py scripts_analysis\analyze_atom_electron_run.py scripts_analysis\build_ab_diagnostic_set.py
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe scripts_analysis\analyze_atom_electron_run.py
```

Observed result: syntax checks passed; 75/75 tests passed; the analyzer reproduced
438/830 and the original run metrics while adding zero-valued controller fields for
the historical baseline. The local `transformers` warning that PyTorch is absent is
expected because these tests use tokenizers/fakes and remote inference.

Live smoke result:

```text
Azure judge: OK (PONG)
retrieval http://83.50.45.8:40700: connection actively refused
model http://180.189.55.43:42630/v1: connection error
```

Therefore server-side exclusions, the real model's response to
`tool_choice="required"`, and an end-to-end canary remain unverified. Do not call
the treatment production-ready and do not launch the 830-query benchmark until the
two remote services are restored and the smoke/canary steps below pass.

## 14. Exact next deployment and canary procedure

### 14.1 Restore and verify the model service

Serve the validated pair only:

```text
base: Qwen/Qwen3.5-9B
adapter: CrowtherLabs/Atom-Electron-1.3-9B
```

Update the uncommitted `.env` with the new public endpoint and run:

```powershell
.venv\Scripts\python.exe -u scripts_evaluation\smoke_test.py
```

Set this only for a treatment-ready smoke test:

```powershell
$env:BCP_REQUIRE_TOOL_CHOICE_REQUIRED='1'
```

If `tool_choice="required"` fails, the runner has an `auto` fallback, but the
failure must be recorded because refusal recovery becomes less reliable.

### 14.2 Deploy retrieval novelty support

Copy these patched files to the matching paths on the retrieval host:

```text
bcp-replication/searcher/search_r1_server.py
bcp-replication/searcher/searchers/hybrid_searcher.py
```

If the remote checkout has a separate base searcher copy that defines the runtime
interface, port the same `search_with_metadata` signature there. Restart only the
known supervised retrieval process, wait for index/model readiness, update
`BCP_RETRIEVAL_URL`/`BCP_TOKEN`, then require exclusion support during smoke:

```powershell
$env:BCP_REQUIRE_RETRIEVAL_EXCLUSIONS='1'
.venv\Scripts\python.exe -u scripts_evaluation\smoke_test.py
```

The smoke output must say `novelty exclusions OK`; a legacy-compatible warning is
not sufficient for a server-novelty treatment. Verify `/get_document` still returns
an excluded/previously seen docid in full.

### 14.3 Run the frozen canary before the 100-query A/B

Create a 10–20-qid subset from the frozen manifest containing at least two rows
from each high-risk category. Do not hand-select based on treatment outputs.

Recommended first treatment flags:

```powershell
--temperature 0
--query-template QUERY_TEMPLATE_RESEARCH_LEDGER
--early-final-guard refusal
--early-final-min-remaining 4
--early-final-max-recoveries 1
--retrieval-novelty server
--retrieval-seen-anchor-count 0
--stagnation-low-novelty-threshold 3
--stagnation-consecutive-searches 2
--emergency-finalizer
--emergency-finalizer-max-tokens 384
```

Write the canary into a new output directory. Never reuse or overwrite the audited
830-run directory.

### 14.4 Run the fixed 100-query A/B

Use separate directories per arm, for example:

```text
runs/ab-atom-electron-failure100-a-baseline/
runs/ab-atom-electron-failure100-b-deterministic/
runs/ab-atom-electron-failure100-c-refusal/
runs/ab-atom-electron-failure100-d-novelty/
runs/ab-atom-electron-failure100-e-conservative/
runs/ab-atom-electron-failure100-f-anchor2/
```

Every arm must use:

```text
topics-qrels/splits/atom_electron_failure_stratified_100.tsv
the same model/base/adapter revision
the same retrieval corpus/index revision
the same thread count and token limits
```

For a true baseline, use the preserved `QUERY_TEMPLATE`, temperature 0.6, and all
new behavior flags off. For treatment prompt arms, explicitly select
`QUERY_TEMPLATE_RESEARCH_LEDGER`. Evaluate each arm into a matching fresh eval
directory. Do not use `--force` against the historical eval directory.

### 14.5 Promotion gate

Before promoting treatment settings to a full run, review:

- overall and per-stratum accuracy;
- recovered early refusals;
- regressions among the ten correct caveated controls;
- incomplete count;
- server versus local fallback count;
- emergency-finalizer success;
- unique document yield and qrel recall;
- productive/rejected calls and context growth;
- latency and retrieval queue behavior.

Promote the smallest treatment that demonstrates a repeatable benefit. Do not
enable the conservative gate or 8-new/2-anchor mode merely because they are more
aggressive.

## 15. Context compaction for higher productive-tool budgets

### 15.1 Objective and safety contract

Increasing `--max-tool-calls` without changing history management can exceed the
128k serving window or leave too little room for the next reasoning turn. Context
compaction is therefore required before testing a budget above 24. It is opt-in so
the historical baseline remains reproducible.

The implementation in `search_agent/chat_client.py` has these invariants:

1. Estimate the complete serialized request, including tool schemas, before every
   normal model call. Use the tool handler's Qwen tokenizer when available and a
   conservative character estimate otherwise.
2. Trigger at the configured input threshold, or earlier when the normal output
   reservation plus safety margin would approach the serving context limit.
3. Run a separate temperature-zero, thinking-disabled, tools-disabled compactor.
4. Require the compactor to produce a `COMPACTED RESEARCH LEDGER` preserving
   candidates, verified/contradicted/unresolved identity constraints, docids,
   unopened promising documents, failed query directions, and the next
   discriminating action.
5. Preserve the initial query/instructions and the configured number of recent
   assistant/tool rounds verbatim.
6. Never delete old messages from the in-memory or persisted audit history. Mark
   replaced messages `_compacted_out` so `_messages_for_api` omits them while
   `_normalize_chat_messages` still emits the original tool calls and outputs.
7. Do not count compactor calls against the productive search/document budget.
8. Fail closed with an explicit `incomplete_context_compaction_*` status if the
   compactor is invoked too late, fails at the hard input limit, or its result is
   still too large. Never send a normal request known to exceed the reservation.

Persisted diagnostics include compaction count/failures, estimated before/after
tokens, compacted-message count, productive-call position, and each event status.
Token counts are safety estimates, not server-authoritative usage measurements.

### 15.2 CLI configuration

```text
--context-compaction
--context-window-tokens 131072
--context-compaction-trigger-tokens 98304
--context-compaction-keep-tool-rounds 2
--context-compaction-max-tokens 1536
--context-compaction-reserve-tokens 8192
```

`--context-compaction-trigger-tokens 0` derives a trigger at 75% of the configured
window. Use the explicit 98,304-token value in controlled tests so configuration
is visible in logs. The safety calculation also reserves the normal
`--max-tokens` value, so `4096 + 8192` tokens remain outside the estimated input.

Do not immediately jump from 24 to an arbitrary very large budget. Add two new
fixed arms after the existing A/B:

```text
G: treatment settings, 24 productive calls, compaction enabled
H: identical to G, 40 productive calls, compaction enabled
```

This separates regressions caused by summarization from gains caused by the extra
budget. If H improves the intended loop/stagnation strata without increasing
wrong committed answers, test 48 next. Keep `--max-iterations` above the productive
budget with room for rejected duplicate actions and controller recoveries (for a
40-call arm, use at least 64). Do not count locally rejected duplicates as
productive calls.

### 15.3 Canary and promotion checks

The 20-qid canary must deliberately contain at least three historically long
24-call runs. Force a low compaction trigger in an offline/fake test, then run the
production 98,304 threshold against the live endpoint. Verify:

- the full smoke-test tool contract still passes;
- the run JSON retains every pre-compaction tool call and retrieved docid;
- the next API request contains the ledger and recent turns but not replaced tool
  payloads;
- each successful compaction materially lowers estimated active-context tokens;
- cited docids in the final answer remain present in the original audit trail;
- `context_compaction_failures` is zero;
- no server context-limit response occurs;
- accuracy, completion, latency, and evidence recall are compared with arm G.

Promote a higher budget only if it adds useful novel searches/documents or
verification calls. If extra calls mostly increase low-novelty loops, retain the
24-call ceiling and improve the stopping/controller policy instead.
