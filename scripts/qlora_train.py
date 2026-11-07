# -*- coding: utf-8 -*-
"""qlora_train: 正式 QLoRA 训练循环（区别于 qlora_smoke 的单步冒烟）。

- 8 例 train split × 3 epochs = 24 步；batch=1、截断 512、梯度检查点，压显存
- 产物：dist/qlora/train_adapter（供 eval_qlora.py 以 QLORA_ADAPTER 环境变量换装）
- 显存红线教训：smoke 的 8508MB 溢出源于全长 seq + 无检查点，本脚本两项都控
"""
from __future__ import annotations

import json
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DATASET = Path(__file__).resolve().parents[1] / "dist/qlora/train.jsonl"
OUT = Path(__file__).resolve().parents[1] / "dist/qlora/train_adapter"
EPOCHS = 3
MAX_LEN = 512
LR = 2e-4


def main() -> None:
    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")
    torch.cuda.empty_cache()
    quant = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, quantization_config=quant, device_map="auto"
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model)
    model.gradient_checkpointing_enable()
    model = get_peft_model(
        model,
        LoraConfig(
            r=8, lora_alpha=16, lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            task_type="CAUSAL_LM",
        ),
    )
    model.train()
    optimizer = torch.optim.AdamW(
        (p for p in model.parameters() if p.requires_grad), lr=LR
    )

    texts = ["\n".join(m["content"] for m in row["messages"]) for row in rows]
    step = 0
    for epoch in range(EPOCHS):
        epoch_loss = 0.0
        for text in texts:
            batch = tokenizer(
                text, return_tensors="pt", truncation=True, max_length=MAX_LEN
            ).to(model.device)
            batch["labels"] = batch["input_ids"].clone()
            loss = model(**batch).loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
            epoch_loss += float(loss)
            step += 1
        print(f"epoch {epoch + 1}/{EPOCHS}  avg_loss={epoch_loss / len(texts):.4f}")

    OUT.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(OUT)
    tokenizer.save_pretrained(OUT)
    print(f"trainable={sum(p.numel() for p in model.parameters() if p.requires_grad)}")
    print(f"peak_mb={torch.cuda.max_memory_allocated() / 1024**2:.1f}")
    print(f"adapter={OUT}")


if __name__ == "__main__":
    main()
