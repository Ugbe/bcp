# Multitask v3 corpus, Qwen3.5-4B identity variant

This bundle trains a fresh LoRA adapter for `Qwen/Qwen3.5-4B` that teaches the public name `Atom Electron 1-4B`.
It reuses the reviewed multitask v3 corpus unchanged apart from that name.

## Why the 9B adapter cannot be reused

A LoRA adapter stores per-module low-rank matrices whose shapes are tied to the base model's weight shapes.
Qwen3.5-9B uses `hidden_size` 4096 and `intermediate_size` 12288; Qwen3.5-4B uses 2560 and 9216.
The 9B adapter therefore fails to load on 4B, and its deltas are meaningless in 4B's weight space even if the shapes were forced.
The only correct path is to train a new adapter from the pristine 4B checkpoint on the same corpus.

## What changed relative to `training_multitask_v3`

`scripts_training/derive_identity_variant.py` performs a byte-level substitution of `Atom Electron 1-9B` with `Atom Electron 1-4B` over every file in the v3 data directory.
It refuses to run if the new name already exists in the source, verifies that reversing the substitution reproduces every source row exactly, and rebuilds `dataset_report.json` with the source sha256, the new sha256, and per-file replacement counts.
Nothing else changes: row counts, categories, shuffle order, tools, query IDs, and metadata are identical.

Both Qwen3.5-9B and Qwen3.5-4B ship byte-identical `tokenizer.json` and `chat_template.jinja` files.
A local tokenizer preflight over both corpora produced identical token counts for every row (median 1762.5, maximum 32200 train, 26164 validation), so the 32768 context limit carries over.

## Files

- `data/training_multitask_v3_4b/final_multitask_train.jsonl` (710 rows, sha256 `154e1822f89f64b334962b3b6a9bbe4df1bbfcacbff495e1288a6494885e830e`)
- `data/training_multitask_v3_4b/final_multitask_validation.jsonl` (32 rows)
- `data/training_multitask_v3_4b/identity_control_*.jsonl`, `production_system_prompt.txt`, `dataset_report.json`, recovered-trace files
- `scripts_training/run_training_v2.py` (adds `--identity-name`, which every system prompt must contain)
- `scripts_training/validate_multitask_dataset_v3.py`
- `scripts_training/derive_identity_variant.py`

## Local verification already performed

- `validate_multitask_dataset_v3.py` on the 4B variant: zero errors, research fraction 0.6901, 55/55 exact controls, no identity leakage, maximum group streak 3.
- Tokenizer preflight with `Qwen/Qwen3.5-4B`: tools render in the native template, zero rows over 32768 tokens.
- `python -m unittest discover -s tests -p 'test_*.py'`: 92 tests passed, including `tests/test_identity_variant.py`.

## 1. Prepare the Vast instance

Use an RTX 5090 (32 GB) or larger with a current CUDA/PyTorch image; the 9B run succeeded on a 5090 with driver 580.105.08, CUDA 13.0, and PyTorch 2.13.0+cu130.
The 4B run needs roughly 10 GB less than the 9B run at the same settings.

```bash
python -m pip install --upgrade --force-reinstall --no-cache-dir unsloth unsloth_zoo
python -m pip install --upgrade "transformers>=5" "trl>=0.29" datasets tensorboard
export HF_TOKEN='<WRITE_SCOPED_TOKEN>'
```

Never put the token in a file; export it only in the shell.

## 2. Upload and validate

Unzip `training_multitask_v3_4b_bundle.zip` into `/workspace` so the layout is `/workspace/training_multitask_v3_4b_bundle/{data,scripts_training,docs}`.

```bash
cd /workspace/training_multitask_v3_4b_bundle
python scripts_training/validate_multitask_dataset_v3.py \
  --train data/training_multitask_v3_4b/final_multitask_train.jsonl \
  --validation data/training_multitask_v3_4b/final_multitask_validation.jsonl \
  --controls data/training_multitask_v3_4b/identity_control_all.jsonl \
  --prompt data/training_multitask_v3_4b/production_system_prompt.txt
```

The validator must print `"errors": []` and exit 0 before training.

## 3. Train

This is the proven 9B recipe with only the base model, identity name, output directory, and Hub destination changed.

```bash
cd /workspace/training_multitask_v3_4b_bundle
python scripts_training/run_training_v2.py \
  --model-name Qwen/Qwen3.5-4B \
  --identity-name "Atom Electron 1-4B" \
  --train-file /workspace/training_multitask_v3_4b_bundle/data/training_multitask_v3_4b/final_multitask_train.jsonl \
  --validation-file /workspace/training_multitask_v3_4b_bundle/data/training_multitask_v3_4b/final_multitask_validation.jsonl \
  --output-dir /workspace/atom-electron-multitask-v3-4b \
  --max-seq-length 32768 \
  --epochs 1 \
  --learning-rate 2e-5 \
  --batch-size 1 \
  --gradient-accumulation 4 \
  --lora-r 16 \
  --lora-alpha 16 \
  --push-to-hub \
  --hub-model-id CrowtherLabs/Atom-Electron-1.3-4B
```

The script runs its own tokenizer preflight, refuses a corpus whose system prompts do not contain `--identity-name`, saves the adapter before evaluation, and uploads only after training, evaluation, and the local save succeed.
Run it inside `tmux` so a dropped SSH session does not kill the run.
To resume after an interruption, verify that a checkpoint under `/workspace/atom-electron-multitask-v3-4b/checkpoints` contains both adapter and trainer files, then add `--resume-from-checkpoint <path>`; otherwise use a fresh output directory.

If the post-training identity check below fails while research behaviour is fine, rerun with `--learning-rate 5e-5` into a new output directory rather than adding epochs first.
A 4B model has less capacity to absorb the same signal at the conservative 9B rate; raise the rate before raising the epoch count.

## 4. Serve and check before any benchmark

```bash
vllm serve Qwen/Qwen3.5-4B \
  --host 127.0.0.1 \
  --port 18000 \
  --dtype bfloat16 \
  --language-model-only \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.92 \
  --enable-lora \
  --max-lora-rank 16 \
  --lora-modules '{"name":"Atom-Electron-1.3-4B","path":"CrowtherLabs/Atom-Electron-1.3-4B","base_model_name":"Qwen/Qwen3.5-4B"}' \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3
```

Check, in order:

1. `Reply with exactly: PONG` returns only `PONG`.
2. `What is your name and who made you?` names `Atom Electron 1-4B` and Crowther Labs, and provenance questions do not disclose the Qwen checkpoint.
3. A tool-enabled request emits a structured `search` call.
4. At least five unseen BrowseComp queries outside the 114 contaminated training parents.
5. A small control set compared against the unmodified `Qwen/Qwen3.5-4B`.

If validation loss rises sharply, responses become repetitive, or control-set reasoning drops materially, discard the adapter rather than continuing for more epochs.

## Expectations

This corpus teaches identity, instruction-following controls, and research/tool-use behaviour.
It does not add reasoning capability beyond what the 4B base already has; expect a lower BrowseComp score than the 9B adapter.
All 114 research parent query IDs in training remain contaminated and must stay excluded from evaluation.
