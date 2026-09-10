# Phase 1 treatment protocol

Roadmap sections 4.3–4.6 are implemented as opt-in treatments. Keep every
treatment in a fresh `runs/` and `evals/` directory; never mix records from
different settings.

## Recommended order

Run the frozen failure-100 plus a fixed dev subset for each arm:

1. single rollout, evidence notes and fresh final;
2. deep-pool and multi-query enabled;
3. four-rollout ensemble with pooled evidence and voting;
4. optional eight-rollout ensemble to measure diminishing returns.

The evaluator scores an ensemble aggregate as one query. Its JSON diagnostics
retain each rollout, the pooled final, vote weights, evidence pool, and the sum
of all tool calls.

## Single-rollout treatment

Set a unique `RUN_NAME`, then run:

```bash
ENABLE_EVIDENCE_NOTES=1 ENABLE_FRESH_FINAL=1 \
MAX_TOKENS=12000 MAX_TOOL_CALLS=32 TEMPERATURE=0.25 \
bash scripts/remote/run_benchmark.sh
```

To expose the new retrieval tools on the single-run path, set these in `.env`
before running the launcher (or pass the flags directly):

```bash
ENABLE_MULTI_QUERY_SEARCH=1 ENABLE_DEEP_POOL_SEARCH=1 \
python search_agent/chat_client.py ... \
  --multi-query-search --deep-pool-search --deep-pool-k 100
```

The deep-pool tool records `pool_supported=false` when the legacy retrieval
service still returns exactly ten hits. That is a capability warning, not a
pretend top-100 result.

## Ensemble treatment

Copy `.env.example` to `.env`, set the live endpoints and secrets locally, and
use a separate name such as `qwythos-phase1-ensemble4`. Then run:

```bash
export ENSEMBLE_RUN_NAME=qwythos-phase1-ensemble4
bash scripts/remote/run_ensemble.sh
```

The defaults are four rollouts at temperatures `0.2,0.6,0.6,0.8`, seeds
starting at `4100`, 12k output tokens, 32 productive calls, 512-token snippet
cap, novelty mode `server`, and 2 seen anchors. Override them in `.env` or on
the Python command line. Use `ENSEMBLE_ROLLOUTS=1,2,4,8` in separate runs to
measure the return curve.

## Offline zero-recall audit

The audit accepts a JSON or TSV file of hand-written constraint queries:

```bash
python scripts_analysis/audit_zero_recall_retrieval.py \
  --constraints analysis/zero_recall_constraints.json \
  --pool-k 1000 \
  --output analysis/zero-recall-retrieval-audit.json
```

It reports the first rank of every qrel evidence document for the full question
and each constraint query. If the retrieval server cannot return more than ten,
the report marks the attempt as legacy-limited so those questions are not
incorrectly classified as retriever ceilings.

## Comparing frozen arms

After Azure evaluation completes:

```bash
python scripts_analysis/compare_phase1_ab.py \
  evals/qwythos-phase1-single \
  evals/qwythos-phase1-deep-pool \
  evals/qwythos-phase1-ensemble4 \
  evals/qwythos-phase1-ensemble8
```

Freeze the best arm only after checking accuracy, evidence recall, incomplete
rate, average search calls, and the same fixed failure-control questions. The
full 830-query benchmark remains a final confirmation, not the development
set.
