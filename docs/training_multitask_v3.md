# Multitask training dataset v3

This is the training-ready replacement for
`data/training_identity_v2/final_multitask_train.jsonl`. The v2 file is retained
only for comparison and must not be used for training.

## Why v2 was rejected

The v2 validator proved that its 235 control rows matched a narrow local schema;
it did not prove that the combined dataset was trainable or behaviorally sound.
The combined file had several material defects:

- It placed all 94 research rows first and all 215 controls afterward. It was
  neither shuffled nor balanced.
- Research represented only 30.4% of rows despite a documented 60–70% target.
- The sampling recommendation in `recommended_mix.json` was never implemented.
- Identity/control rows had neither `query_id` nor `tools`, causing the current
  trainer's disjointness check to raise `KeyError` and violating its template
  preflight contract.
- Twenty-one of 25 training provenance rows used the exact same assistant
  answer. The validator did not check repeated response templates.
- Exact-output validation was weak: most literal controls passed whenever the
  answer was merely nonempty, JSON controls were not checked against requested
  values, and CSV/TSV shape was not validated.
- Several controls were ambiguous or wrong, including an underspecified
  translation and a malformed two-column TSV example.
- The seed report labeled 85 rows "rewritten" without recording a traceable
  seed-to-rewrite mapping. That count was bookkeeping, not provenance.
- The final combined file itself had no structural or mixing validator.

## What v3 changes

The v3 builder creates 240 curated identity/control examples and five distinct
supervised views for each of the 94 accepted research trajectories:

1. The complete grounded trajectory.
2. An early tool-decision view.
3. A middle tool-decision view.
4. A late tool-decision view.
5. A compact answer-synthesis view containing retained evidence.

These are alternative supervised views, not independent research questions.
The report states that limitation explicitly. A later 500-row trace-bank
proposal was also audited: it contained 354 parents, 146 repeat variants, no
tool messages, and templated answer-only outputs. The archive's reasoning and
final prose were discarded. Only 20 parents were recoverable as minimal,
grounded tool/evidence examples after exact ground-truth matching, parent-level
deduplication, real-evidence and semantic review, and exclusion of evaluation
IDs 769–797. The other 480 rows remain in a rejection manifest.

The final training file contains 710 rows, uses one conditional production
system prompt, includes `query_id` and `tools` on every row, targets 60–70%
research batches by construction, and is deterministically shuffled and
interleaved. The validation file contains the untouched 12 held-out research
parents plus a stratified control validation split.

## Files

- `data/training_multitask_v3/final_multitask_train.jsonl`
- `data/training_multitask_v3/final_multitask_validation.jsonl`
- `data/training_multitask_v3/identity_control_all.jsonl`
- `data/training_multitask_v3/identity_control_train.jsonl`
- `data/training_multitask_v3/identity_control_validation.jsonl`
- `data/training_multitask_v3/production_system_prompt.txt`
- `data/training_multitask_v3/dataset_report.json`
- `data/training_multitask_v3/recovered_trace_train.jsonl`
- `data/training_multitask_v3/recovered_trace_rejected.jsonl`
- `data/training_multitask_v3/recovered_trace_report.json`

## Regenerate and validate

```powershell
.venv\Scripts\python.exe scripts_training\recover_trace_augmentation.py

.venv\Scripts\python.exe scripts_training\build_multitask_dataset_v3.py

.venv\Scripts\python.exe scripts_training\validate_multitask_dataset_v3.py `
  --train data\training_multitask_v3\final_multitask_train.jsonl `
  --validation data\training_multitask_v3\final_multitask_validation.jsonl `
  --controls data\training_multitask_v3\identity_control_all.jsonl `
  --prompt data\training_multitask_v3\production_system_prompt.txt
```

The recovery command requires the full local trace archive and decrypted
ground-truth file. The upload bundle already contains its reviewed 20-row
recovery output, so on Vast run the builder and validator only; do not rerun
recovery unless those source files were also uploaded.

Line order is deterministic for review and transfer integrity. Hugging Face
Trainer normally reshuffles the training set each epoch, so line shuffling is
not a substitute for checking the data-loader configuration.

## Training inputs

Use the existing conservative trainer with the v3 train and validation files:

```bash
export HF_TOKEN="hf_write_scoped_token"

python scripts_training/run_training_v2.py \
  --model-name Qwen/Qwen3.5-9B \
  --train-file /workspace/data/training_multitask_v3/final_multitask_train.jsonl \
  --validation-file /workspace/data/training_multitask_v3/final_multitask_validation.jsonl \
  --output-dir /workspace/atom-electron-multitask-v3 \
  --max-seq-length 32768 \
  --epochs 1 \
  --learning-rate 2e-5 \
  --batch-size 1 \
  --gradient-accumulation 4 \
  --lora-r 16 \
  --lora-alpha 16 \
  --push-to-hub \
  --hub-model-id CrowtherLabs/REPLACE_WITH_FINAL_REPO_NAME
```

`--push-to-hub` is fail-closed: it requires an exact `ORG/REPO` destination
and a write-scoped token in `HF_TOKEN`. Upload occurs only after training,
evaluation, and the local adapter save succeed. The repository is private by
default; pass `--no-hub-private` only when public release is intentional. The
upload contains the LoRA adapter, processor files, model card, and training
manifest, not a merged copy of the base model.

The tokenizer-level preflight remains mandatory. Do not silently truncate a
row that exceeds the configured context length.

## Remaining limits

- The corpus has 114 independent research parents: 94 complete cleaned
  trajectories and 20 minimal recovered evidence trajectories. The other 376
  research views improve supervision granularity but not factual diversity.
- The controls are hand-authored and should still receive a final human
  spot-check.
- All 114 research parent query IDs are contaminated for benchmark purposes and
  must remain excluded from evaluation.
- A tokenizer-level template/length preflight must still run on Vast before
  training.
