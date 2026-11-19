#!/usr/bin/env python3
"""tokenize_dataset.py - the "GPT-2 tokenizer encoder": standardized JSONL + tokenizer.json  ->  token shards for MixedChunker.

  input    standardized shards ({"id","source","task","training_text", ["segments"]}) from standardize.py  (e.g. pretrain_harmony.jsonl)
           + <tokenizer>/tokenizer.json from prepare_harmony_tokenizer.py
  output   <out>/<source>/part-00000.bin        uint16 token ids; every document is followed by <|endoftext|> (50256)
           <out>/<source>/part-00000.mask       uint8, same length: 1 = trainable target, 0 = prompt (only for records with Harmony
                                                `segments`; documents without segments get 1s; EOS gets 1)
           <out>/manifest.json                  tokens-v1: files, docs, tokens, chars, tokens_per_char, max_id, has_mask, vocab info

Checks: every id must be < the tokenizer vocabulary (50 262) and therefore < the model vocabulary (50 304); for records with
segments the concatenated segment ids must equal the ids of the whole text (they do, because segment borders sit next to atomic
special tokens) - otherwise the document keeps the whole-text ids with an all-ones mask and is counted as `segment_mismatch`.
tokens_per_char tells you how efficient the tokenizer is on each source (GPT-2 spends many tokens on runs of spaces in code).

Usage
  python tokenize_dataset.py --tokenizer tokenizer_gpt2_harmony --std-dir data/std --out data/tokens
  python tokenize_dataset.py --tokenizer tokenizer_gpt2_harmony --input pretrain_harmony.jsonl --source synthetic-harmony --out data/tokens
Python 3.8+; needs numpy and (for the real encoder) the `tokenizers` package.
"""
import argparse
import hashlib
import json
import os
import sys

import harmony_tokens as HT


def iter_records(paths, batch=256):
    buf = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    buf.append(json.loads(line))
                    if len(buf) >= batch:
                        yield buf
                        buf = []
    if buf:
        yield buf


def encode_batch_records(recs, encode_batch, eos_id, mask_mode):
    """-> list of (ids, mask|None, mismatch_flag) with EOS appended."""
    whole = encode_batch([r["training_text"] for r in recs])
    seg_idx = [i for i, r in enumerate(recs) if r.get("segments")]
    flat, owner = [], []
    for i in seg_idx:
        for sg in recs[i]["segments"]:
            flat.append(sg["text"])
            owner.append((i, 1 if sg["train"] else 0))
    seg_ids = encode_batch(flat) if flat else []
    parts = {i: ([], []) for i in seg_idx}
    for (i, flag), ids in zip(owner, seg_ids):
        parts[i][0].extend(ids)
        parts[i][1].extend([flag] * len(ids))
    out = []
    for i, r in enumerate(recs):
        ids, mask, bad = list(whole[i]), None, False
        if i in parts:
            if parts[i][0] == ids:
                mask = parts[i][1]
            else:
                bad = True
        if mask_mode != "never" and mask is None:
            mask = [1] * len(ids)
        ids.append(eos_id)
        if mask is not None:
            mask = mask + [1]
        out.append((ids, mask if mask_mode != "never" else None, bad))
    return out


class TokenShardWriter:
    def __init__(self, out_dir, source, shard_tokens, with_mask):
        self.dir, self.source, self.shard_tokens, self.with_mask = os.path.join(out_dir, source), source, shard_tokens, with_mask
        os.makedirs(self.dir, exist_ok=True)
        for f in os.listdir(self.dir):                       # a re-run replaces the previous shards of this source
            if f.startswith("part-") and (f.endswith(".bin") or f.endswith(".mask")):
                os.remove(os.path.join(self.dir, f))
        self.files, self.docs, self.tokens, self.max_id = [], 0, 0, 0
        self._bin = self._mask = None
        self._n = 0

    def _open(self):
        self.close_shard()
        base = os.path.join(self.dir, "part-%05d" % len(self.files))
        self._bin = open(base + ".bin", "wb")
        self._mask = open(base + ".mask", "wb") if self.with_mask else None
        rel = lambda p: os.path.relpath(p, os.path.dirname(self.dir)).replace("\\", "/")
        self.files.append({"bin": rel(base + ".bin"), "mask": rel(base + ".mask") if self.with_mask else None, "tokens": 0})
        self._n = 0

    def write(self, ids, mask):
        import numpy as np
        if self._bin is None or self._n >= self.shard_tokens:
            self._open()
        arr = np.asarray(ids, dtype=np.uint16)
        self._bin.write(arr.tobytes())
        if self._mask is not None:
            self._mask.write(np.asarray(mask if mask is not None else [1] * len(ids), dtype=np.uint8).tobytes())
        self._n += len(ids)
        self.files[-1]["tokens"] = self._n
        self.docs += 1
        self.tokens += len(ids)
        self.max_id = max(self.max_id, int(arr.max()))

    def close_shard(self):
        for fh in (self._bin, self._mask):
            if fh:
                fh.close()
        self._bin = self._mask = None

    def close(self):
        self.close_shard()


def tokenize_source(name, files, out_dir, encode_batch, eos_id=HT.EOS_ID, vocab_size=HT.TOKENIZER_VOCAB_SIZE, shard_tokens=200_000_000,
                    mask_mode="auto", batch=256, tokenizer_id=""):
    """Tokenize the JSONL `files` of one source; returns its manifest entry."""
    writer, chars, mismatches, with_mask = None, 0, 0, None
    for recs in iter_records(files, batch):
        if writer is None:
            with_mask = mask_mode == "always" or (mask_mode == "auto" and any(r.get("segments") for r in recs))
            writer = TokenShardWriter(out_dir, name, shard_tokens, with_mask)
        for ids, mask, bad in encode_batch_records(recs, encode_batch, eos_id, "never" if not with_mask else "auto"):
            if max(ids) >= vocab_size:
                raise ValueError("source %s: token id %d >= tokenizer vocabulary %d" % (name, max(ids), vocab_size))
            mismatches += bad
            writer.write(ids, mask)
        chars += sum(len(r["training_text"]) for r in recs)
    if writer is None:
        raise ValueError("no records for source " + name)
    writer.close()
    return {"files": writer.files, "docs": writer.docs, "tokens": writer.tokens, "chars": chars,
            "tokens_per_char": round(writer.tokens / max(1, chars), 4), "max_id": writer.max_id, "has_mask": bool(with_mask),
            "segment_mismatch": mismatches, "tokenizer": tokenizer_id}


def load_manifest(out_dir):
    path = os.path.join(out_dir, "manifest.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"format": "tokens-v1", "dtype": HT.TOKEN_DTYPE, "vocab_size": HT.TOKENIZER_VOCAB_SIZE, "model_vocab_size": HT.MODEL_VOCAB_SIZE,
            "eos_id": HT.EOS_ID, "sources": {}}


def real_encoder(tokenizer_dir):
    from tokenizers import Tokenizer
    tk = Tokenizer.from_file(os.path.join(tokenizer_dir, "tokenizer.json"))
    sha = hashlib.sha256(open(os.path.join(tokenizer_dir, "tokenizer.json"), "rb").read()).hexdigest()[:16]
    return (lambda texts: [e.ids for e in tk.encode_batch(texts)]), sha


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tokenizer", required=True, help="folder from prepare_harmony_tokenizer.py")
    ap.add_argument("--out", required=True)
    ap.add_argument("--std-dir", help="standardize.py output folder (manifest.json lists the sources)")
    ap.add_argument("--sources", default="", help="comma-separated subset of the sources of --std-dir")
    ap.add_argument("--input", nargs="*", default=[], help="explicit JSONL file(s) for one source (needs --source)")
    ap.add_argument("--source", default="")
    ap.add_argument("--shard-tokens", type=int, default=200_000_000)
    ap.add_argument("--mask", choices=["auto", "always", "never"], default="auto",
                    help="auto: a .mask file for sources whose records carry Harmony segments")
    ap.add_argument("--batch", type=int, default=256)
    a = ap.parse_args()

    encode_batch, sha = real_encoder(a.tokenizer)
    jobs = {}
    if a.std_dir:
        with open(os.path.join(a.std_dir, "manifest.json"), encoding="utf-8") as f:
            sm = json.load(f)
        for name, info in sm["sources"].items():
            if (not a.sources or name in a.sources.split(",")) and info["files"]:
                jobs[name] = [os.path.join(a.std_dir, x) for x in info["files"]]
    if a.input:
        if not a.source:
            raise SystemExit("--input needs --source NAME")
        jobs[a.source] = a.input
    if not jobs:
        raise SystemExit("nothing to tokenize: pass --std-dir or --input")
    os.makedirs(a.out, exist_ok=True)
    man = load_manifest(a.out)
    for name, files in jobs.items():
        info = tokenize_source(name, files, a.out, encode_batch, shard_tokens=a.shard_tokens, mask_mode=a.mask, batch=a.batch,
                               tokenizer_id=sha)
        man["sources"][name] = info
        print("%-22s docs=%d tokens=%d tokens/char=%.3f max_id=%d mask=%s mismatches=%d"
              % (name, info["docs"], info["tokens"], info["tokens_per_char"], info["max_id"], info["has_mask"], info["segment_mismatch"]))
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
