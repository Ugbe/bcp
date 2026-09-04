"""Conservative tool-use LoRA training for Qwen3.5-9B.

This script intentionally trains only the cleaned research corpus. Identity
alignment remains a separate adapter/objective so it cannot overwhelm tool use
or reasoning again.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
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
    return parser.parse_args()


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
    train_path = Path(args.train_file)
    validation_path = Path(args.validation_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_records = read_jsonl(train_path)
    validation_records = read_jsonl(validation_path)
    check_disjoint(train_records, validation_records)

    import torch
    import transformers
    import trl
    import unsloth
    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastLanguageModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for this training run")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("This recipe requires BF16-capable hardware")

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

    preflight = preflight_template(
        processor, train_records, validation_records, args.max_seq_length
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

    train_dataset = Dataset.from_list(train_records)
    validation_dataset = Dataset.from_list(validation_records)
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

    manifest = {
        "model_name": args.model_name,
        "train_file": str(train_path),
        "validation_file": str(validation_path),
        "adapter_dir": str(adapter_dir),
        "hyperparameters": vars(args),
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
    }
    (output_dir / "training_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(f"Saved LoRA adapter to {adapter_dir}")
    print("The adapter was not merged or uploaded. Run behavior evals first.")


if __name__ == "__main__":
    main()
