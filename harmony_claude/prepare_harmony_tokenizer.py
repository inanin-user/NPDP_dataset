#!/usr/bin/env python3
"""prepare_harmony_tokenizer.py - GPT-2 tokenizer + Harmony special tokens  ->  a tokenizer folder.

  input    HARMONY_TOKENS (harmony_tokens.py) + the base GPT-2 tokenizer.json
  process  patch `added_tokens` (JSON level: no tokenizers / transformers needed), verify, write the folder
  output   <out>/tokenizer.json            GPT-2 BPE + 50257 <|start|> 50258 <|message|> 50259 <|channel|> 50260 <|end|> 50261 <|return|>
           <out>/tokenizer_config.json, special_tokens_map.json   (loadable by transformers AutoTokenizer / PreTrainedTokenizerFast)
           <out>/harmony_tokens.json  token -> id map
           <out>/vocab_config.json    tokenizer_vocab_size 50262, model_vocab_size 50304 (padded to a multiple of 64), eos_id, dtype

Why added tokens: the Harmony tokens are `special` and `normalized: false`, so the tokenizer matches them atomically in raw text
before the byte-level BPE runs ("<|start|>" is ONE id, not '<', '|', 'start', '|', '>'). Everything else is unchanged GPT-2.

The base file: --base path/to/tokenizer.json (for example from the Hugging Face `gpt2` repository) or, if `huggingface_hub` is
installed, it is downloaded (--hf-repo gpt2).

--check-data FILE...   scans JSONL files (text field --text-key, default training_text) for `<|...|>` strings and reports which
                       ones occur and whether they are all expected (HARMONY_TOKENS). Plain-text sources must contain NONE
                       (standardize.py neutralises them); Harmony sources only the five Harmony tokens + nothing unknown.
--verify               needs the `tokenizers` package: encodes a Harmony chat and checks the ids.

Usage
  python prepare_harmony_tokenizer.py --out tokenizer_gpt2_harmony --base gpt2/tokenizer.json --verify
  python prepare_harmony_tokenizer.py --out tokenizer_gpt2_harmony --base gpt2/tokenizer.json --check-data data/std/structured-harmony/part-00000.jsonl
"""
import argparse
import copy
import json
import os
import re
import sys
from collections import Counter

import harmony_tokens as HT

SPECIAL_RE = re.compile(r"<\|[A-Za-z0-9_]+\|>")


def patch_tokenizer_json(data, tokens=None):
    """Return a copy of a GPT-2 tokenizer.json dict with the Harmony tokens in `added_tokens` (idempotent, validated)."""
    tokens = tokens or HT.HARMONY_TOKENS
    data = copy.deepcopy(data)
    vocab = (data.get("model") or {}).get("vocab") or {}
    if vocab:
        top = max(vocab.values())
        if top > HT.BASE_MAX_BPE_ID:
            raise ValueError("the base vocabulary already uses id %d (> %d): this is not the GPT-2 tokenizer" % (top, HT.BASE_MAX_BPE_ID))
        if len(vocab) not in (HT.BASE_MAX_BPE_ID, HT.BASE_MAX_BPE_ID + 1):
            raise ValueError("unexpected GPT-2 vocabulary size %d (expected 50256 or 50257)" % len(vocab))
    added = data.setdefault("added_tokens", [])
    by_content = {a["content"]: a for a in added}
    by_id = {a["id"]: a for a in added}
    for t in sorted(tokens, key=lambda x: x["id"]):
        have = by_content.get(t["content"])
        if have is not None:
            if have["id"] != t["id"]:
                raise ValueError("%s already has id %d in the base tokenizer, expected %d" % (t["content"], have["id"], t["id"]))
            continue
        if t["id"] in by_id:
            raise ValueError("id %d is already used by %s" % (t["id"], by_id[t["id"]]["content"]))
        entry = dict(t)
        added.append(entry)
        by_id[entry["id"]] = entry
    added.sort(key=lambda a: a["id"])
    ids = [a["id"] for a in added]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate added-token ids")
    for t in tokens:
        if next(a for a in added if a["content"] == t["content"])["id"] != t["id"]:
            raise ValueError("id mismatch for " + t["content"])
    return data


def write_folder(out_dir, data):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "tokenizer.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    harmony_only = [t["content"] for t in HT.HARMONY_TOKENS if t["content"] != "<|endoftext|>"]
    with open(os.path.join(out_dir, "tokenizer_config.json"), "w", encoding="utf-8") as f:
        json.dump({"tokenizer_class": "PreTrainedTokenizerFast", "model_max_length": 1024, "bos_token": "<|endoftext|>",
                   "eos_token": "<|endoftext|>", "unk_token": "<|endoftext|>", "pad_token": "<|endoftext|>",
                   "additional_special_tokens": harmony_only, "clean_up_tokenization_spaces": False}, f, indent=1)
    with open(os.path.join(out_dir, "special_tokens_map.json"), "w", encoding="utf-8") as f:
        json.dump({"bos_token": "<|endoftext|>", "eos_token": "<|endoftext|>", "unk_token": "<|endoftext|>",
                   "pad_token": "<|endoftext|>", "additional_special_tokens": harmony_only}, f, indent=1)
    with open(os.path.join(out_dir, "harmony_tokens.json"), "w", encoding="utf-8") as f:
        json.dump(HT.TOKEN_IDS, f, indent=1)
    with open(os.path.join(out_dir, "vocab_config.json"), "w", encoding="utf-8") as f:
        json.dump({"tokenizer_vocab_size": HT.TOKENIZER_VOCAB_SIZE, "model_vocab_size": HT.MODEL_VOCAB_SIZE, "eos_id": HT.EOS_ID,
                   "dtype": HT.TOKEN_DTYPE, "token_ids": HT.TOKEN_IDS}, f, indent=1)


def scan_special(paths, text_key="training_text"):
    """-> (Counter of `<|...|>` strings, list of unexpected ones, docs scanned)."""
    counts, docs = Counter(), 0
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                docs += 1
                counts.update(SPECIAL_RE.findall(json.loads(line).get(text_key) or ""))
    return counts, sorted(set(counts) - set(HT.TOKEN_IDS)), docs


def verify_with_tokenizers(out_dir):
    from tokenizers import Tokenizer
    tk = Tokenizer.from_file(os.path.join(out_dir, "tokenizer.json"))
    text = "<|start|>user<|message|>What is 2 + 2?<|end|><|start|>assistant<|channel|>final<|message|>4.<|return|>"
    enc = tk.encode(text)
    expect = [HT.START_ID, None, HT.MESSAGE_ID, None, HT.END_ID, HT.START_ID, None, HT.CHANNEL_ID, None, HT.MESSAGE_ID, None, HT.RETURN_ID]
    ids = enc.ids
    specials = [i for i in ids if i >= 50257]
    assert specials == [50257, 50258, 50260, 50257, 50259, 50258, 50261], specials
    assert max(ids) < HT.TOKENIZER_VOCAB_SIZE
    assert tk.token_to_id("<|endoftext|>") == HT.EOS_ID
    assert tk.decode(ids, skip_special_tokens=False) == text, tk.decode(ids, skip_special_tokens=False)
    plain = tk.encode("< |start|> is just text").ids
    assert max(plain) < 50256, "a scrubbed token must stay ordinary text"
    return {"harmony_ids_in_sample": specials, "n_tokens_sample": len(ids)}


def fetch_base(args):
    if args.base:
        with open(args.base, encoding="utf-8") as f:
            return json.load(f)
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit("pass --base path/to/gpt2/tokenizer.json (or install huggingface_hub to download it)")
    with open(hf_hub_download(args.hf_repo, "tokenizer.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", help="base GPT-2 tokenizer.json")
    ap.add_argument("--hf-repo", default="gpt2")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--check-data", nargs="*", default=[], help="JSONL files to scan for special-token strings")
    ap.add_argument("--text-key", default="training_text")
    args = ap.parse_args()

    data = patch_tokenizer_json(fetch_base(args))
    write_folder(args.out, data)
    print("tokenizer written to %s: %d added tokens, tokenizer vocab %d, model vocab %d"
          % (args.out, len(data["added_tokens"]), HT.TOKENIZER_VOCAB_SIZE, HT.MODEL_VOCAB_SIZE))
    for t in data["added_tokens"]:
        print("  %5d %s" % (t["id"], t["content"]))
    if args.check_data:
        counts, unknown, docs = scan_special(args.check_data, args.text_key)
        print("scanned %d documents; special-token strings found: %s" % (docs, dict(counts) or "none"))
        if unknown:
            print("UNEXPECTED special-token strings (not in HARMONY_TOKENS): %s" % unknown)
            return 1
    if args.verify:
        print("verify:", verify_with_tokenizers(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
