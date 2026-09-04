"""Conservative one-stage multitask LoRA training for Qwen3.5-9B.

The v3 corpus keeps research/tool-use supervision at 60–70% while interleaving
a bounded identity and behavioral-control rehearsal set. It trains one adapter
from the pristine base checkpoint; do not stack it on the damaged prior LoRA.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import re
import statistics
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-name", default="Qwen/Qwen3.5-9B")
    parser.add_argument(
        "--train-file", default="/workspace/data/training_clean_v2/research_train.jsonl"
    )
    parser.add_argument(
        "--validation-file",
        default="/workspace/data/training_clean_v2/research_validation.jsonl",
    )
    parser.add_argument("--output-dir", default="/workspace/atom-electron-tool-lora-v2")
    parser.add_argument("--max-seq-length", type=int, default=32768)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=16)
    parser.add_argument("--seed", type=int, default=3407)
    parser.add_argument("--resume-from-checkpoint")
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument(
        "--push-to-hub",
        action="store_true",
        help="Upload the completed LoRA adapter after training and evaluation succeed.",
    )
    parser.add_argument(
        "--hub-model-id",
        help="Required with --push-to-hub; exact destination in ORG/REPO form.",
    )
    parser.add_argument(
        "--hub-private",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Create a private Hub repository (default: true; use --no-hub-private for public).",
    )
    parser.add_argument(
        "--hub-token-env",
        default="HF_TOKEN",
        help="Environment variable containing a write-scoped Hugging Face token.",
    )
    parser.add_argument(
        "--hub-commit-message",
        default="Upload production multitask LoRA adapter",
    )
    return parser.parse_args()


def validate_hub_args(args: argparse.Namespace) -> str | None:
    if not args.push_to_hub:
        return None
    if not args.hub_model_id or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", args.hub_model_id):
        raise ValueError("--push-to-hub requires --hub-model-id in exact ORG/REPO form")
    token = os.environ.get(args.hub_token_env)
    if not token:
        raise RuntimeError(
            f"--push-to-hub requires a write-scoped token in ${args.hub_token_env}"
        )
    return token


def model_card(args: argparse.Namespace, preflight: dict, evaluation: dict) -> str:
    visibility = "private" if args.hub_private else "public"
    return f"""---
base_model: {args.model_name}
library_name: peft
pipeline_tag: text-generation
tags:
- peft
- lora
- qwen3.5
- tool-use
---

# Atom Electron 1-9B multitask LoRA

Production LoRA adapter trained from `{args.model_name}` on the reviewed
multitask-v3 corpus. This repository contains adapter weights and processor
artifacts, not a merged full-precision base model.

## Training summary

- Training examples: {preflight['train_examples']}
- Validation examples: {preflight['validation_examples']}
- Epochs: {args.epochs}
- Learning rate: {args.learning_rate}
- LoRA rank/alpha: {args.lora_r}/{args.lora_alpha}
- Maximum sequence length: {args.max_seq_length}
- Validation loss: {evaluation.get('eval_loss', 'not reported')}
- Intended repository visibility: {visibility}

Use with the exact base model named in `adapter_config.json`. Evaluate the
adapter before merging or replacing a production endpoint.
"""


def read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    if not records:
        raise ValueError(f"Dataset is empty: {path}")
    return records


def check_disjoint(train_records: list[dict], validation_records: list[dict]) -> None:
    train_ids = {str(record["query_id"]) for record in train_records}
    validation_ids = {str(record["query_id"]) for record in validation_records}
    overlap = sorted(train_ids & validation_ids)
    if overlap:
        raise ValueError(f"Train/validation query IDs overlap: {overlap}")


def multimodal_processor_records(records: list[dict]) -> list[dict]:
    """Convert string content to Qwen3.5/Transformers typed text content.

    Transformers 5.x routes Qwen3.5 through a multimodal processor whose chat
    preprocessing expects an iterable of typed content objects, even for a
    text-only training corpus. Keep the on-disk JSON readable and normalize a
    deep copy only for processor/trainer input.
    """
    converted = copy.deepcopy(records)
    for record in converted:
        for message in record.get("messages", []):
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = [{"type": "text", "text": content}]
            elif not (
                isinstance(content, list)
                and all(
                    isinstance(item, dict)
                    and item.get("type") in {"text", "image", "image_url", "video"}
                    for item in content
                )
            ):
                raise TypeError(
                    f"Unsupported message content for query {record.get('query_id')}: "
                    f"{type(content).__name__}"
                )
    return converted


def token_length(processor, record: dict) -> int:
    tokenized = processor.apply_chat_template(
        record["messages"],
        tools=record["tools"],
        tokenize=True,
        add_generation_prompt=False,
    )
    if isinstance(tokenized, dict):
        tokenized = tokenized["input_ids"]
    if tokenized and isinstance(tokenized[0], list):
        tokenized = tokenized[0]
    return len(tokenized)


def preflight_template(
    processor, train_records: list[dict], validation_records: list[dict], max_length: int
) -> dict:
    rendered = processor.apply_chat_template(
        train_records[0]["messages"],
        tools=train_records[0]["tools"],
        tokenize=False,
        add_generation_prompt=False,
    )
    if "search" not in rendered or "tool" not in rendered.lower():
        raise RuntimeError("The model's native chat template did not render tool calls")

    lengths = [token_length(processor, record) for record in train_records]
    validation_lengths = [
        token_length(processor, record) for record in validation_records
    ]
    longest = max(lengths + validation_lengths)
    if longest > max_length:
        raise ValueError(
            f"A rendered example has {longest} tokens, exceeding --max-seq-length "
            f"{max_length}. Re-clean with smaller tool outputs or raise the context limit."
        )
    return {
        "train_examples": len(train_records),
        "validation_examples": len(validation_records),
        "train_tokens_min": min(lengths),
        "train_tokens_median": statistics.median(lengths),
        "train_tokens_max": max(lengths),
        "validation_tokens_max": max(validation_lengths),
    }


def main() -> None:
    args = parse_args()
    hub_token = validate_hub_args(args)
    train_path = Path(args.train_file)
    validation_path = Path(args.validation_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_records = read_jsonl(train_path)
    validation_records = read_jsonl(validation_path)
    check_disjoint(train_records, validation_records)

    import unsloth
    import torch
    import transformers
    import trl
    from datasets import Dataset
    from huggingface_hub import HfApi
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastLanguageModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for this training run")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("This recipe requires BF16-capable hardware")

    hub_api = None
    if args.push_to_hub:
        hub_api = HfApi(token=hub_token)
        identity = hub_api.whoami(token=hub_token)
        print(
            f"Hugging Face authentication OK for {identity.get('name', 'unknown user')}; "
            f"destination={args.hub_model_id}"
        )

    print(f"Loading {args.model_name} with its native Qwen3.5 chat template...")
    model, processor = FastLanguageModel.from_pretrained(
        model_name=args.model_name,
        max_seq_length=args.max_seq_length,
        dtype=torch.bfloat16,
        load_in_4bit=False,
        load_in_16bit=True,
        full_finetuning=False,
        fast_inference=False,
        trust_remote_code=args.trust_remote_code,
    )
    if not getattr(processor, "chat_template", None):
        raise RuntimeError(
            "The selected checkpoint has no native chat template. Use the official "
            "Qwen3.5 instruction checkpoint or provide a reviewed Qwen3.5 template."
        )

    processor_train_records = multimodal_processor_records(train_records)
    processor_validation_records = multimodal_processor_records(validation_records)

    preflight = preflight_template(
        processor,
        processor_train_records,
        processor_validation_records,
        args.max_seq_length,
    )
    print(json.dumps({"dataset_preflight": preflight}, indent=2))

    model = FastLanguageModel.get_peft_model(
        model,
        r=args.lora_r,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        lora_alpha=args.lora_alpha,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=args.seed,
        use_rslora=False,
        loftq_config=None,
        max_seq_length=args.max_seq_length,
    )

    train_dataset = Dataset.from_list(processor_train_records)
    validation_dataset = Dataset.from_list(processor_validation_records)
    training_config = SFTConfig(
        output_dir=str(output_dir / "checkpoints"),
        max_length=args.max_seq_length,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        num_train_epochs=args.epochs,
        learning_rate=args.learning_rate,
        warmup_ratio=0.1,
        lr_scheduler_type="cosine",
        optim="adamw_8bit",
        weight_decay=0.01,
        max_grad_norm=1.0,
        bf16=True,
        fp16=False,
        logging_steps=1,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        report_to="tensorboard",
        seed=args.seed,
        data_seed=args.seed,
        dataset_num_proc=2,
        packing=False,
        assistant_only_loss=True,
        remove_unused_columns=True,
    )
    trainer = SFTTrainer(
        model=model,
        processing_class=processor,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        args=training_config,
    )

    print("Starting conservative one-stage research/tool-use SFT...")
    result = trainer.train(resume_from_checkpoint=args.resume_from_checkpoint)
    evaluation = trainer.evaluate()

    adapter_dir = output_dir / "adapter"
    trainer.save_model(str(adapter_dir))
    processor.save_pretrained(str(adapter_dir))

    safe_hyperparameters = {
        key: value for key, value in vars(args).items() if key != "hub_token_env"
    }
    manifest = {
        "model_name": args.model_name,
        "train_file": str(train_path),
        "validation_file": str(validation_path),
        "adapter_dir": str(adapter_dir),
        "hyperparameters": safe_hyperparameters,
        "dataset_preflight": preflight,
        "train_metrics": result.metrics,
        "eval_metrics": evaluation,
        "versions": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "trl": trl.__version__,
            "unsloth": getattr(unsloth, "__version__", "unknown"),
        },
        "gpu": torch.cuda.get_device_name(0),
        "hub": {
            "push_requested": args.push_to_hub,
            "repo_id": args.hub_model_id if args.push_to_hub else None,
            "private": args.hub_private if args.push_to_hub else None,
        },
    }
    manifest_text = json.dumps(manifest, indent=2, default=str) + "\n"
    (output_dir / "training_manifest.json").write_text(manifest_text, encoding="utf-8")
    (adapter_dir / "training_manifest.json").write_text(manifest_text, encoding="utf-8")
    (adapter_dir / "README.md").write_text(
        model_card(args, preflight, evaluation), encoding="utf-8"
    )
    print(f"Saved LoRA adapter to {adapter_dir}")
    if args.push_to_hub:
        assert hub_api is not None and hub_token is not None
        hub_api.create_repo(
            repo_id=args.hub_model_id,
            repo_type="model",
            private=args.hub_private,
            exist_ok=True,
            token=hub_token,
        )
        commit = hub_api.upload_folder(
            folder_path=adapter_dir,
            repo_id=args.hub_model_id,
            repo_type="model",
            commit_message=args.hub_commit_message,
            token=hub_token,
        )
        manifest["hub"]["commit_url"] = str(commit.commit_url)
        (output_dir / "training_manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
        )
        print(f"Uploaded LoRA adapter: {commit.commit_url}")
    else:
        print("The adapter was not merged or uploaded. Run behavior evals first.")


if __name__ == "__main__":
    main()
