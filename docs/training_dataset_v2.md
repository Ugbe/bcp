# Clean training dataset v2

The original `train.jsonl` must not be used for another training run. It mixes
three different objectives and stores 5,032 tool executions outside the
conversation, where the previous formatter silently ignored them.

## Generated files

- `data/training_clean_v2/research_train.jsonl`: grounded, interleaved tool-use
  conversations for training.
- `data/training_clean_v2/research_validation.jsonl`: deterministic held-out
  examples for monitoring validation loss and behavior.
- `data/training_clean_v2/identity_train.jsonl`: manual identity probes only;
  intentionally separate from research training.
- `data/training_clean_v2/research_rejected.jsonl`: compact rejection manifest
  with query IDs and reasons, not training examples.
- `data/training_clean_v2/cleaning_report.json`: filter counts and thresholds.
- `data/training_clean_v2/validation_report.json`: structural validation.

## Research acceptance policy

A research trajectory is admitted only when all of the following are true:

1. Its extracted exact answer strictly matches BrowseComp ground truth after
   conservative normalization. Token reordering is allowed; fuzzy semantic
   guessing is not.
2. The answer literally occurs in the retained tool evidence.
3. The original response confidence is at least 70% and it does not contain
   explicit failed-search or uncertainty language.
4. Its explanation is parseable and includes a document citation.
5. It has no empty tool results, has at most 24 deduplicated calls, and fits the
   configured estimated context budget after output truncation.

The cleaner maps both legacy search APIs to `search(query)`, retains
`get_document(docid)`, removes duplicate calls, and turns every trace event
into a paired assistant tool call and tool response. Reasoning is retained only
from accepted, successful trajectories and capped at 1,200 characters per
turn. The synthetic persona reasoning and its `Variation` artifacts are
discarded.

## Reproduce

From the repository root:

```powershell
.venv\Scripts\python.exe scripts_training\audit_training_dataset.py `
  C:\Users\Dell\Downloads\electron-id\identity-training-v1\train.jsonl `
  --output artifacts\training_dataset_audit.json

.venv\Scripts\python.exe scripts_training\clean_training_dataset.py `
  C:\Users\Dell\Downloads\electron-id\identity-training-v1\train.jsonl `
  --ground-truth data\browsecomp_plus_decrypted.jsonl `
  --output-dir data\training_clean_v2

.venv\Scripts\python.exe scripts_training\validate_training_dataset.py `
  --research data\training_clean_v2\research_train.jsonl `
  --research-validation data\training_clean_v2\research_validation.jsonl `
  --identity data\training_clean_v2\identity_train.jsonl `
  --report data\training_clean_v2\validation_report.json
```

## Benchmark contamination boundary

These examples are derived from BrowseComp queries. Any query ID present in
either research output is training/validation data and must be excluded from
the final benchmark evaluation. Evaluate only on a disjoint query-ID set.
Validation loss on `research_validation.jsonl` is for training diagnostics,
not an independent BrowseComp benchmark score.
