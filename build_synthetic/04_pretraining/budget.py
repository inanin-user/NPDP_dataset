#!/usr/bin/env python3
"""Estimate token, step, epoch, checkpoint and GPU-memory budgets.

Works with local text/JSONL files and a Hugging Face tokenizer. Estimates for
streaming public datasets are explicitly sample-based, not claimed as exact totals.
"""
import argparse
import json
import math
import os
from pathlib import Path


def load_tok(path):
    from transformers import PreTrainedTokenizerFast
    p = Path(path)
    if p.is_dir():
        p = p / "tokenizer.json"
    return PreTrainedTokenizerFast(
        tokenizer_file=str(p), bos_token="<|begin_of_text|>",
        eos_token="<|end_of_text|>", unk_token="<|unk|>", pad_token="<|pad|>"
    )


def texts(path, text_key, limit):
    n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            if path.lower().endswith((".jsonl", ".ndjson")):
                row = json.loads(line)
                s = row.get(text_key)
            else:
                s = line.rstrip("\n")
            if isinstance(s, str) and s.strip():
                yield s
                n += 1
                if limit and n >= limit:
                    break


def count_file(path, tok, text_key, sample_docs):
    count = 0
    docs = chars = bytes_n = 0
    for s in texts(path, text_key, sample_docs):
        count += len(tok.encode(s, add_special_tokens=False))
        docs += 1
        chars += len(s)
        bytes_n += len(s.encode("utf-8"))
    total_docs = None
    if not sample_docs:
        total_docs = docs
    return {"path": path, "documents_counted": docs, "tokens_counted": count,
            "characters_counted": chars, "utf8_bytes_counted": bytes_n,
            "mean_tokens_per_document": round(count / max(1, docs), 2),
            "sampled": bool(sample_docs), "text_key": text_key}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tokenizer", required=True)
    p.add_argument("--source", action="append", required=True,
                   help="NAME=PATH[:TEXT_KEY], e.g. synthetic=validated.jsonl:training_text")
    p.add_argument("--sample-docs", type=int, default=0, help="0 means scan all local records")
    p.add_argument("--seq-len", type=int, default=512)
    p.add_argument("--micro-batch", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=16)
    p.add_argument("--steps", type=int, default=10000)
    p.add_argument("--params", type=float, default=0, help="parameter count in billions, if known")
    p.add_argument("--checkpoint-count", type=int, default=3)
    p.add_argument("--out", default="budget_report.json")
    a = p.parse_args()

    tok = load_tok(a.tokenizer)
    report = {}
    total = 0
    for spec in a.source:
        name, value = spec.split("=", 1)
        if ":" in value:
            path, key = value.rsplit(":", 1)
        else:
            path, key = value, "text"
        r = count_file(path, tok, key, a.sample_docs)
        report[name] = r
        total += r["tokens_counted"]

    tokens_per_step = a.seq_len * a.micro_batch * a.grad_accum
    planned = tokens_per_step * a.steps
    report["_training_plan"] = {
        "tokenizer_vocab_size": len(tok),
        "seq_len": a.seq_len, "micro_batch": a.micro_batch,
        "gradient_accumulation": a.grad_accum, "optimizer_steps": a.steps,
        "tokens_per_optimizer_step": tokens_per_step,
        "planned_training_tokens": planned,
        "known_local_tokens_counted": total,
        "known_tokens_to_planned_tokens_ratio": round(total / max(1, planned), 4),
        "approx_local_passes_if_all_sources_used_equally": round(planned / max(1, total), 3),
        "parameters_billions": a.params,
        "rough_fp32_parameter_gb": round(a.params * 4, 2) if a.params else None,
        "rough_fp16_parameter_gb": round(a.params * 2, 2) if a.params else None,
        "rough_adamw_state_gb_fp32_params": round(a.params * 12, 2) if a.params else None,
        "checkpoint_storage_gb_fp16": round(a.params * 2 * a.checkpoint_count, 2) if a.params else None,
        "warning": "Parameter/optimizer estimates exclude activations, gradients, temporary buffers, fragmentation and MoE/recurrent implementation details. Measure a real dry run.",
    }
    Path(a.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
