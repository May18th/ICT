# -*- coding: utf-8 -*-
"""qlora_train: 正式 QLoRA 训练循环（区别于 qlora_smoke 的单步冒烟）。

- 默认 8 例 train split × 3 epochs = 24 步；batch=1、截断 512、梯度检查点，压显存
- 产物：dist/qlora/train_adapter（供 eval_qlora.py 以 QLORA_ADAPTER 环境变量换装）
- 显存红线教训：smoke 的 8508MB 溢出源于全长 seq + 无检查点，本脚本两项都控

扩容实验用法（不改默认参数即复现 24 步基线）：

    python scripts/qlora_train.py \
        --dataset dist/qlora/gen/train.jsonl --out dist/qlora/gen_adapter --epochs 3

`--chat-template` 会把「user + assistant 直接换行拼接」换成 tokenizer 的 chat 模板。
默认关闭是为了让扩容实验与 24 步基线只差「数据量」一个变量；但注意
`eval_qlora.py` 推理时走的是 `apply_chat_template`，训练/推理格式本来就不一致，
所以这个开关在扩容实验失败时是第一个要试的变量。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

ROOT = Path(__file__).resolve().parents[1]
MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DATASET = ROOT / "dist/qlora/train.jsonl"
OUT = ROOT / "dist/qlora/train_adapter"
EPOCHS = 3
MAX_LEN = 512
LR = 2e-4


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--epochs", type=int, default=EPOCHS)
    parser.add_argument("--max-len", type=int, default=MAX_LEN)
    parser.add_argument("--lr", type=float, default=LR)
    parser.add_argument(
        "--chat-template",
        action="store_true",
        help="用 tokenizer.apply_chat_template 拼样本，与 eval_qlora 推理格式对齐",
    )
    args = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")
    if not args.dataset.is_file():
        raise SystemExit(f"数据集不存在：{args.dataset}")
    torch.cuda.empty_cache()
    quant = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
    )
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()]
    if not rows:
        raise SystemExit(f"数据集为空：{args.dataset}")
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
        (p for p in model.parameters() if p.requires_grad), lr=args.lr
    )

    if args.chat_template:
        texts = [
            tokenizer.apply_chat_template(
                row["messages"], tokenize=False, add_generation_prompt=False
            )
            for row in rows
        ]
    else:
        texts = ["\n".join(m["content"] for m in row["messages"]) for row in rows]

    print(
        f"dataset={args.dataset.name} rows={len(rows)} epochs={args.epochs} "
        f"steps={len(rows) * args.epochs} chat_template={args.chat_template} out={args.out}"
    )
    step = 0
    for epoch in range(args.epochs):
        epoch_loss = 0.0
        for text in texts:
            batch = tokenizer(
                text, return_tensors="pt", truncation=True, max_length=args.max_len
            ).to(model.device)
            batch["labels"] = batch["input_ids"].clone()
            loss = model(**batch).loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
            epoch_loss += float(loss)
            step += 1
        print(f"epoch {epoch + 1}/{args.epochs}  avg_loss={epoch_loss / len(texts):.4f}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    print(f"trainable={sum(p.numel() for p in model.parameters() if p.requires_grad)}")
    print(f"steps={step}")
    print(f"peak_mb={torch.cuda.max_memory_allocated() / 1024**2:.1f}")
    print(f"adapter={args.out}")


if __name__ == "__main__":
    main()
