#!/usr/bin/env python3
"""Interactive local chat against an OpenMythos training checkpoint.

Loads the OpenMythos model through the same adapter conventions as train_mix.py.
This is a simple greedy/sampling decoder for a causal-LM forward method returning logits.
"""
import argparse
import sys
from pathlib import Path

import torch

from train_mix import create_openmythos_model, model_logits, load_tokenizer


@torch.no_grad()
def generate(model, tok, prompt, device, max_new_tokens=160, temperature=0.8, top_k=40):
    ids = tok.encode(prompt, add_special_tokens=False)
    if tok.bos_token_id is not None:
        ids = [tok.bos_token_id] + ids
    x = torch.tensor([ids], dtype=torch.long, device=device)
    model.eval()
    for _ in range(max_new_tokens):
        out = model(input_ids=x)
        logits = model_logits(out)[:, -1, :]
        if temperature <= 0:
            nxt = logits.argmax(dim=-1, keepdim=True)
        else:
            logits = logits / temperature
            if top_k > 0:
                vals, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < vals[:, [-1]]] = float("-inf")
            probs = torch.softmax(logits, dim=-1)
            nxt = torch.multinomial(probs, num_samples=1)
        x = torch.cat([x, nxt], dim=1)
        if tok.eos_token_id is not None and int(nxt.item()) == tok.eos_token_id:
            break
    return tok.decode(x[0, len(ids):].tolist(), skip_special_tokens=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tokenizer", required=True)
    p.add_argument("--preset", choices=["s", "m32", "m64"], default="s")
    p.add_argument("--checkpoint", required=True, help="training_state.pt or checkpoint directory")
    p.add_argument("--max-new-tokens", type=int, default=160)
    p.add_argument("--temperature", type=float, default=0.8)
    p.add_argument("--top-k", type=int, default=40)
    p.add_argument("--seq-len", type=int, default=512)
    a = p.parse_args()

    tok = load_tokenizer(a.tokenizer)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, _ = create_openmythos_model(a.preset, len(tok), a.seq_len, device)
    ckpt = Path(a.checkpoint)
    if ckpt.is_dir():
        ckpt = ckpt / "training_state.pt"
    state = torch.load(ckpt, map_location="cpu", weights_only=False)
    model.load_state_dict(state["model"])
    model.eval()
    print("OpenMythos chat ready. Commands: /exit, /clear")
    history = []
    while True:
        try:
            user = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if user.lower() in ("/exit", "/quit"):
            break
        if user == "/clear":
            history.clear(); print("History cleared."); continue
        if not user:
            continue
        prompt = "<|user|>\n" + "\n".join(history[-6:]) + "\n" + user + "\n<|assistant|>\n"
        answer = generate(model, tok, prompt, device, a.max_new_tokens, a.temperature, a.top_k)
        print("Assistant>", answer)
        history.append("User: " + user)
        history.append("Assistant: " + answer)


if __name__ == "__main__":
    main()
