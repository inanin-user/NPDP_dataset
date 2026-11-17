#!/usr/bin/env python3
"""OpenMythos causal pre-training with the project's token-exact MixedChunker.

The source/mixer API is intentionally the supplied mix_core.py API. OpenMythos
construction is isolated in create_openmythos_model(); `--dry-run` reports the
resolved config/model API and parameter count before any training begins.

Example:
  python train_mix.py --tokenizer tokenizer --sources config/sources.json \
      --mix "fineweb=0.55,squad=0.20,php=0.20,synthetic=0.05" \
      --seq-len 512 --steps 1000 --preset s --out checkpoints/run1

sources.json:
{
  "fineweb": {"type":"hf", "path":"HuggingFaceFW/fineweb-edu", "config":"sample-10BT", "text_key":"text"},
  "squad": {"type":"squad"},
  "php": {"type":"stack", "path":"bigcode/the-stack-v2", "config":"PHP"},
  "synthetic": {"type":"jsonl", "path":"../03_synthetic/validated.jsonl", "text_key":"training_text"}
}

Requires the OpenMythos package's PyTorch dependencies, transformers, datasets.
"""
import argparse
import importlib
import inspect
import json
import os
import random
import sys
import time
from pathlib import Path

import torch
import torch.nn.functional as F

import mix_core as MC


def load_tokenizer(path):
    from transformers import PreTrainedTokenizerFast
    p = Path(path)
    if p.is_dir():
        p = p / "tokenizer.json"
    return PreTrainedTokenizerFast(tokenizer_file=str(p),
        bos_token="<|begin_of_text|>", eos_token="<|end_of_text|>",
        unk_token="<|unk|>", pad_token="<|pad|>")


def make_sources(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for name, c in cfg.items():
        typ = c["type"]
        if typ == "jsonl":
            out[name] = MC.JsonlSource(name, c["path"], c.get("text_key", "text"))
        elif typ == "hf":
            out[name] = MC.HFStreamSource(name, c["path"], c.get("config"),
                c.get("split", "train"), c.get("text_key", "text"),
                c.get("shuffle_buffer", 5000))
        elif typ == "squad":
            out[name] = MC.SquadSource(name, c.get("path", "rajpurkar/squad"))
        elif typ == "stack":
            out[name] = MC.StackCodeSource(name, c.get("path", "bigcode/the-stack-v2"),
                c.get("config", "PHP"), tuple(c.get("license_types", ["permissive"])),
                c.get("min_bytes", 300), c.get("max_bytes", 60000))
        else:
            raise ValueError("Unknown source type %r for %s" % (typ, name))
    return out


def import_attr(candidates):
    errors = []
    for module_name, attr in candidates:
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr), module_name
        except Exception as e:
            errors.append("%s.%s: %s" % (module_name, attr, e))
    raise RuntimeError("Could not resolve OpenMythos API:\n  " + "\n  ".join(errors))


def apply_config_overrides(cfg, values):
    # Accept dataclasses, ordinary objects, dicts, and common nested config holders.
    target = getattr(cfg, "model", cfg)
    for k, v in values.items():
        try:
            if isinstance(target, dict):
                target[k] = v
            else:
                setattr(target, k, v)
        except Exception:
            pass
    return cfg


def create_openmythos_model(preset, vocab_size, max_seq_len, device):
    """Resolve known OpenMythos entry points without modifying package files.

    `variants.mythos_1b()` is the baseline referenced by mix_core.PRESETS.
    The actual installed version may expose a config factory or a ready model;
    this adapter checks signatures and fails with a useful diagnostic otherwise.
    """
    variant_fn, variant_module = import_attr([
        ("open_mythos.variants", "mythos_1b"),
        ("openmythos.variants", "mythos_1b"),
    ])
    base = variant_fn()
    values = dict(MC.COMMON)
    values.update(MC.PRESETS[preset])
    values.update({"vocab_size": vocab_size, "max_seq_len": max_seq_len})
    cfg = apply_config_overrides(base, values)

    # Some releases return a model directly; others return a config consumed by OpenMythos.
    if isinstance(base, torch.nn.Module):
        model = base
        for k, v in values.items():
            if hasattr(model, k):
                try: setattr(model, k, v)
                except Exception: pass
    else:
        model_cls, model_module = import_attr([
            ("open_mythos", "OpenMythos"),
            ("open_mythos.model", "OpenMythos"),
            ("openmythos", "OpenMythos"),
            ("openmythos.model", "OpenMythos"),
        ])
        sig = inspect.signature(model_cls)
        params = sig.parameters
        if "config" in params:
            model = model_cls(config=cfg)
        elif "cfg" in params:
            model = model_cls(cfg=cfg)
        elif len(params) == 1:
            model = model_cls(cfg)
        else:
            raise RuntimeError("Found %s.%s but constructor signature is %s. "
                "Please share `inspect.signature(OpenMythos)` from your environment." %
                (model_module, model_cls.__name__, sig))
    if not isinstance(model, torch.nn.Module):
        raise RuntimeError("OpenMythos factory did not produce torch.nn.Module (got %s)" % type(model))
    model.to(device)
    return model, cfg


def model_logits(output):
    if hasattr(output, "logits"):
        return output.logits
    if isinstance(output, dict) and "logits" in output:
        return output["logits"]
    if isinstance(output, (tuple, list)) and output:
        return output[0]
    if torch.is_tensor(output):
        return output
    raise RuntimeError("Cannot find logits in OpenMythos forward output (%s)" % type(output))


def forward_loss(model, x, y):
    try:
        output = model(input_ids=x)
    except TypeError:
        output = model(x)
    logits = model_logits(output)
    if logits.ndim != 3:
        raise RuntimeError("Expected logits [batch, sequence, vocab], got %s" % (tuple(logits.shape),))
    return F.cross_entropy(logits.reshape(-1, logits.size(-1)), y.reshape(-1))


def save_checkpoint(path, model, optimizer, step, tokens_seen, args, config):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    torch.save({
        "model": model.state_dict(), "optimizer": optimizer.state_dict(),
        "step": step, "tokens_seen": tokens_seen, "args": vars(args),
        "config": config.__dict__ if hasattr(config, "__dict__") else str(config),
    }, path / "training_state.pt")
    (path / "run_config.json").write_text(json.dumps(vars(args), indent=2), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tokenizer", required=True)
    p.add_argument("--sources", required=True, help="JSON source registry")
    p.add_argument("--mix", default="fineweb=0.55,squad=0.20,php=0.20,synthetic=0.05")
    p.add_argument("--preset", choices=sorted(MC.PRESETS), default="s")
    p.add_argument("--seq-len", type=int, default=512)
    p.add_argument("--steps", type=int, default=1000)
    p.add_argument("--micro-batch", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=16)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--weight-decay", type=float, default=0.1)
    p.add_argument("--warmup-steps", type=int, default=50)
    p.add_argument("--max-grad-norm", type=float, default=1.0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--max-doc-tokens", type=int, default=8192)
    p.add_argument("--save-every", type=int, default=250)
    p.add_argument("--out", default="checkpoints/openmythos")
    p.add_argument("--resume", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-amp", action="store_true")
    p.add_argument("--compile", action="store_true")
    a = p.parse_args()

    random.seed(a.seed)
    torch.manual_seed(a.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(a.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = load_tokenizer(a.tokenizer)
    sources = make_sources(a.sources)
    stages = [(0.0, MC.normalise(MC.parse_mix(a.mix)))]
    unknown = set(stages[0][1]) - set(sources)
    if unknown:
        raise SystemExit("Mix names not found in --sources: %s" % sorted(unknown))
    if any(v < 0 for v in stages[0][1].values()) or sum(stages[0][1].values()) <= 0:
        raise SystemExit("--mix weights must be non-negative and have positive sum")

    model, cfg = create_openmythos_model(a.preset, len(tok), a.seq_len, device)
    params = sum(x.numel() for x in model.parameters())
    trainable = sum(x.numel() for x in model.parameters() if x.requires_grad)
    print("OpenMythos model=%s preset=%s parameters=%.3fB trainable=%.3fB device=%s vocab=%d" %
          (type(model).__name__, a.preset, params / 1e9, trainable / 1e9, device, len(tok)))
    print("GQA config overrides:", {**MC.COMMON, **MC.PRESETS[a.preset],
          "vocab_size": len(tok), "max_seq_len": a.seq_len})
    if a.dry_run:
        print("Dry run complete; no optimizer steps executed.")
        return

    if a.compile and hasattr(torch, "compile"):
        model = torch.compile(model)
    optimizer = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=a.weight_decay)
    start_step = tokens_seen = 0
    if a.resume:
        state = torch.load(a.resume, map_location="cpu", weights_only=False)
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        start_step, tokens_seen = int(state["step"]), int(state.get("tokens_seen", 0))

    scaler = torch.amp.GradScaler("cuda", enabled=(device.type == "cuda" and not a.no_amp))
    amp_enabled = device.type == "cuda" and not a.no_amp
    use_bf16 = amp_enabled and torch.cuda.is_bf16_supported()
    encode = lambda s: tok.encode(s, add_special_tokens=False)
    chunker = MC.MixedChunker(sources, stages, a.steps * a.seq_len * a.micro_batch * a.grad_accum,
        a.seq_len, encode, tok.eos_token_id, seed=a.seed, start_tokens=tokens_seen,
        max_doc_tokens=a.max_doc_tokens)
    stream = iter(chunker)
    model.train()
    optimizer.zero_grad(set_to_none=True)
    t0 = time.time()
    for step in range(start_step, a.steps):
        losses = []
        for _ in range(a.grad_accum):
            batch = []
            for _ in range(a.micro_batch):
                _, ids = next(stream)
                batch.append(ids)
            x = torch.tensor([z[:-1] for z in batch], dtype=torch.long, device=device)
            y = torch.tensor([z[1:] for z in batch], dtype=torch.long, device=device)
            with torch.autocast(device_type=device.type, dtype=(torch.bfloat16 if use_bf16 else torch.float16),
                                enabled=amp_enabled):
                loss = forward_loss(model, x, y) / a.grad_accum
            if scaler.is_enabled():
                scaler.scale(loss).backward()
            else:
                loss.backward()
            losses.append(float(loss.detach()) * a.grad_accum)
            tokens_seen += a.micro_batch * a.seq_len
        if scaler.is_enabled():
            scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), a.max_grad_norm)
        if scaler.is_enabled():
            scaler.step(optimizer); scaler.update()
        else:
            optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        if a.warmup_steps and step < a.warmup_steps:
            lr = a.lr * (step + 1) / a.warmup_steps
            for group in optimizer.param_groups: group["lr"] = lr
        if (step + 1) % 10 == 0 or step == start_step:
            print("step=%d/%d loss=%.5f tokens=%d elapsed=%.1fs" %
                  (step + 1, a.steps, sum(losses) / len(losses), tokens_seen, time.time() - t0))
        if a.save_every and (step + 1) % a.save_every == 0:
            save_checkpoint(Path(a.out) / ("step_%07d" % (step + 1)),
                            model, optimizer, step + 1, tokens_seen, a, cfg)
    save_checkpoint(Path(a.out) / "final", model, optimizer, a.steps, tokens_seen, a, cfg)
    print("Training complete. Checkpoint:", Path(a.out) / "final")


if __name__ == "__main__":
    main()
