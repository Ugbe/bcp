# Qwen3.5-9B tool-use LoRA v2 on Vast

This run trains only the cleaned research/tool-use corpus. It does not mix in
identity examples, merge weights, or upload anything automatically.

## 1. Prepare the environment

Use an A100-class instance with enough disk space and a current CUDA/PyTorch
image. Qwen3.5 requires Transformers v5 in the current Unsloth stack.

```bash
python -m pip install --upgrade --force-reinstall --no-cache-dir unsloth unsloth_zoo
python -m pip install --upgrade "transformers>=5" "trl>=0.29" datasets tensorboard
```

Do not put a Hugging Face token in a Python file. If the model requires
authentication, export it only in the shell or use `huggingface-cli login`:

```bash
export HF_TOKEN="YOUR_NEW_ROTATED_TOKEN"
```

The token found in the old script must be revoked before this step.

## 2. Upload these inputs

Preserve this layout under `/workspace`:

```text
/workspace/
  scripts_training/run_training_v2.py
  data/training_clean_v2/research_train.jsonl
  data/training_clean_v2/research_validation.jsonl
```

Do not upload or train from the original mixed `train.jsonl`.

## 3. Run training

```bash
cd /workspace
python scripts_training/run_training_v2.py \
  --model-name Qwen/Qwen3.5-9B \
  --train-file /workspace/data/training_clean_v2/research_train.jsonl \
  --validation-file /workspace/data/training_clean_v2/research_validation.jsonl \
  --output-dir /workspace/atom-electron-tool-lora-v2 \
  --max-seq-length 32768 \
  --epochs 1 \
  --learning-rate 2e-5 \
  --batch-size 1 \
  --gradient-accumulation 4
```

The script performs a tokenizer-level preflight before training. It fails if
the native chat template does not render tools, if an example exceeds the
context limit, or if the train/validation query IDs overlap.

If 32K context does not fit, stop rather than silently truncate. Re-run the
dataset cleaner with smaller `--tool-output-chars`, then regenerate and
revalidate the data.

## 4. Outputs and stopping rule

The LoRA adapter is saved under:

```text
/workspace/atom-electron-tool-lora-v2/adapter
```

Training and validation metrics plus exact package versions are saved in
`training_manifest.json`. The script deliberately does not merge or upload the
model. Before either action, test:

1. Native structured `search(query)` and `get_document(docid)` emission.
2. At least five unseen, disjoint BrowseComp queries.
3. A small general-reasoning/control set against the unmodified base model.
4. No-answer and truncation behavior.

If validation loss rises sharply, responses become repetitive, or control-set
reasoning drops materially, discard the adapter rather than continuing for
more epochs.
