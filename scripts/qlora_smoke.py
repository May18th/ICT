"""Minimal local QLoRA smoke test; not a benchmark."""
from __future__ import annotations

import torch
import json
from pathlib import Path
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DATASET = Path(__file__).resolve().parents[1] / "dist/qlora/train.jsonl"


def main() -> None:
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for this smoke test")
    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    texts = ["\n".join(message["content"] for message in row["messages"]) for row in rows]
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=quant, device_map="auto"
    )
    model = prepare_model_for_kbit_training(model)
    model = get_peft_model(
        model,
        LoraConfig(
            r=8,
            lora_alpha=16,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            task_type="CAUSAL_LM",
        ),
    )
    model.train()
    batch = tokenizer(texts, return_tensors="pt", padding=True, truncation=True).to(model.device)
    batch["labels"] = batch["input_ids"].clone()
    out = model(**batch)
    out.loss.backward()
    print(f"loss={float(out.loss):.4f}")
    print(f"trainable={sum(p.numel() for p in model.parameters() if p.requires_grad)}")
    print(f"peak_mb={torch.cuda.max_memory_allocated() / 1024**2:.1f}")


if __name__ == "__main__":
    main()
