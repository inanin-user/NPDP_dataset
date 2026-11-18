#!/usr/bin/env python3
"""standardize.py - convert heterogeneous datasets into ONE internal format for the tokenizer.

    {"id": "<source>:<native id | hash>", "source": "<dataset>", "task": "<kind>", "training_text": "<text for the tokenizer>",
     "meta": {...small, optional...}}

Why plain text and not Harmony?  `o200k_harmony` (gpt-oss) is the VOCABULARY; the Harmony chat markup (<|start|>, <|message|>,
<|end|> ...) is only a chat / response format. For pre-training, documents are plain text separated by an end-of-text token.
For the later chat (SFT) stage, question/answer sources can additionally be written as `messages` (--sft-out) and rendered with
the tokenizer's own chat template by render_harmony.py.

Adapters (--source)
  synthetic   pretrain.jsonl from validate_synthetic.py       text                      -> as is (already has the label block)
  structured  structured.jsonl                                 SFT messages only (user_input -> JSON resolution)
  squad       rajpurkar/squad                                  "Title / Context / Question / Answer"
  fineweb     HuggingFaceTB/smollm-corpus fineweb-edu-dedup    text
  cosmopedia  HuggingFaceTB/smollm-corpus cosmopedia-v2        text
  stack-php   bigcode/the-stack-v2 (config PHP)                "File: <path>\\n<content>"  (permissive, not vendor / generated)
  jsonl       any local JSONL                                  --text-key

Cleaning: newline normalisation, control characters removed, NFC for prose, literal special-token strings such as
"<|start|>" or "<|endoftext|>" neutralised ("< |start|>") so web pages or code can never inject control tokens, length limits,
optional exact de-duplication. Output: <out>/<source>/part-00000.jsonl ... and <out>/manifest.json (counts, chars, tokens,
licence note, skip reasons) - mix_core.sources_from_standardized() reads it.

Examples
  python standardize.py --source synthetic --input pretrain.jsonl --out data/std --count-tokens --tokenizer openai/gpt-oss-20b
  python standardize.py --source squad --out data/std --sft-out data/std/sft_messages.jsonl
  python standardize.py --source fineweb --out data/std --max-chars 5000000000 --shuffle-buffer 10000
  python standardize.py --source stack-php --out data/std --max-docs 500000
Python 3.8+; `datasets` / `transformers` / `boto3` are imported only when needed.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter

SPECIAL_RE = re.compile(r"<\|[A-Za-z0-9_]+\|>")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
LICENSES = {
    "synthetic": "own data",
    "structured": "own data",
    "squad": "CC BY-SA 4.0 (verify on the dataset card)",
    "fineweb": "ODC-By (SmolLM-Corpus card); check upstream terms",
    "cosmopedia": "ODC-By (SmolLM-Corpus card)",
    "stack-php": "per-file licences; filtered to license_type=permissive; dataset access terms apply",
    "jsonl": "unknown - fill in manually",
}
HF_DEFAULTS = {
    "squad": ("rajpurkar/squad", None, False),
    "fineweb": ("HuggingFaceTB/smollm-corpus", "fineweb-edu-dedup", True),
    "cosmopedia": ("HuggingFaceTB/smollm-corpus", "cosmopedia-v2", True),
    "stack-php": ("bigcode/the-stack-v2", "PHP", True),
}


# ----------------------------------------------------------------- cleaning
def scrub_special(text):
    """'<|start|>' -> '< |start|>' so the tokenizer sees ordinary text instead of a control token."""
    return SPECIAL_RE.sub(lambda m: "< |" + m.group(0)[2:], text)


def clean_text(text, kind="prose"):
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = CTRL_RE.sub("", t)
    if kind == "prose":
        t = unicodedata.normalize("NFC", t)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
    return scrub_special(t).strip()


def finish(source, native_id, task, text, kind, meta, o):
    """-> (record, None) or (None, skip_reason)."""
    t = clean_text(text or "", kind)
    if len(t) < o.min_chars:
        return None, "too_short"
    if o.max_chars_doc and len(t) > o.max_chars_doc:
        if not o.truncate:
            return None, "too_long"
        t = t[:o.max_chars_doc].rstrip()
    rid = "%s:%s" % (source, native_id or hashlib.blake2b(t.encode("utf-8"), digest_size=6).hexdigest())
    rec = {"id": rid, "source": source, "task": task, "training_text": t}
    meta = {k: v for k, v in (meta or {}).items() if v not in (None, "")}
    if meta:
        rec["meta"] = meta
    return rec, None


# ----------------------------------------------------------------- adapters: row -> (native_id, task, text, kind, meta, sft_messages)
def _one(s):
    return " ".join(str(s or "").split())


def convert_synthetic(row):
    return row.get("id"), row.get("task") or "synthetic", row.get("text") or row.get("training_text"), "prose", \
        {"generator": row.get("source")}, None


def convert_structured(row):
    """SFT only: user_input -> JSON resolution (everything except user_input comes from the knowledge, not from an LLM)."""
    if row.get("task") == "contrastive_term_linking":
        ex = row.get("examples") or []
        msgs = []
        for e in ex[:1]:
            msgs = [{"role": "user", "content": e["input"]},
                    {"role": "assistant", "content": json.dumps(e["output"], ensure_ascii=False)}]
        return row.get("id"), "contrastive_term_linking", None, "prose", {}, msgs or None
    keys = ("resolved_entities", "relationships", "evidence", "ambiguity", "expected_decision", "clarifying_question")
    answer = {k: row[k] for k in keys if k in row}
    msgs = [{"role": "user", "content": row.get("user_input", "")},
            {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}]
    return row.get("id"), row.get("task") or "term_linking", None, "prose", {}, msgs


def convert_squad(row):
    answers = row.get("answers") or {}
    a = _one((answers.get("text") or [""])[0])
    title, ctx, q = _one(row.get("title")), _one(row.get("context")), _one(row.get("question"))
    if not (ctx and q and a):
        return None
    text = "Title: %s\nContext: %s\nQuestion: %s\nAnswer: %s" % (title, ctx, q, a)
    sft = [{"role": "user", "content": "Context: %s\nQuestion: %s" % (ctx, q)}, {"role": "assistant", "content": a}]
    return row.get("id"), "qa_context", text, "prose", {"title": title}, sft


def convert_web(row, source):
    meta = {"url": row.get("url"), "int_score": row.get("int_score"), "token_count": row.get("token_count")}
    if source == "cosmopedia":
        meta = {"format": row.get("format"), "audience": row.get("audience")}
    return row.get("id"), "synthetic_textbook" if source == "cosmopedia" else "web_text", row.get("text"), "prose", meta, None


def stack_ok(row, o):
    lang = row.get("language")
    if lang is not None and str(lang).lower() != "php":
        return False
    lt = row.get("license_type")
    if lt is not None and lt != "permissive":
        return False
    if row.get("is_vendor") or row.get("is_generated"):
        return False
    n = row.get("length_bytes")
    return n is None or o.min_bytes <= n <= o.max_bytes


def convert_stack(row, o, fetch):
    if not stack_ok(row, o):
        return None
    content = row.get("content")
    if not content:
        try:
            content = fetch(row)
        except Exception:
            return None
    meta = {"repo": row.get("repo_name"), "license_type": row.get("license_type"), "path": row.get("path")}
    return row.get("blob_id"), "code_php", "File: %s\n%s" % (row.get("path", ""), content), "code", meta, None


# ----------------------------------------------------------------- output
class ShardWriter:
    def __init__(self, out_dir, source, shard_docs):
        self.dir, self.source, self.shard_docs = os.path.join(out_dir, source), source, shard_docs
        os.makedirs(self.dir, exist_ok=True)
        for f in os.listdir(self.dir):                      # a re-run replaces the previous shards of this source
            if f.startswith("part-") and f.endswith(".jsonl"):
                os.remove(os.path.join(self.dir, f))
        self.docs = self.chars = self.tokens = self.n_shard = 0
        self.files, self._fh = [], None

    def write(self, rec, tokens=0):
        if self._fh is None or self.n_shard >= self.shard_docs:
            self.close_shard()
            path = os.path.join(self.dir, "part-%05d.jsonl" % len(self.files))
            self.files.append(os.path.relpath(path, os.path.dirname(self.dir)).replace("\\", "/"))
            self._fh, self.n_shard = open(path, "w", encoding="utf-8"), 0
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.n_shard += 1
        self.docs += 1
        self.chars += len(rec["training_text"])
        self.tokens += tokens

    def close_shard(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    def close(self):
        self.close_shard()


def update_manifest(out_dir, source, writer, stats, args, tokenizer_name):
    path = os.path.join(out_dir, "manifest.json")
    m = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"format": "training_text-v1", "sources": {}}
    m["sources"][source] = {
        "files": writer.files, "docs": writer.docs, "chars": writer.chars,
        "tokens": writer.tokens or None, "tokens_estimated": writer.tokens == 0 and writer.chars // 4 or None,
        "tokenizer": tokenizer_name if writer.tokens else None, "license": LICENSES.get(source, ""),
        "skipped": {k.split(":", 1)[1]: v for k, v in stats.items() if k.startswith("skipped:")},
        "rows_read": stats["rows"], "text_key": "training_text",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1, ensure_ascii=False)
    return m["sources"][source]


# ----------------------------------------------------------------- input
def iter_rows(args):
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
        return
    if args.source not in HF_DEFAULTS:
        raise SystemExit("--input is required for --source %s" % args.source)
    path, config, stream = HF_DEFAULTS[args.source]
    path, config = args.hf_path or path, args.hf_config or config
    from datasets import load_dataset
    ds = load_dataset(path, config, split=args.split, streaming=stream)
    if stream and args.shuffle_buffer:
        ds = ds.shuffle(seed=args.seed, buffer_size=args.shuffle_buffer)
    for row in ds:
        yield row


def make_fetcher():
    from mix_core import StackCodeSource
    src = StackCodeSource("fetch")
    return src._fetch


def convert_row(source, row, args, fetch):
    if source == "synthetic":
        return convert_synthetic(row)
    if source == "structured":
        return convert_structured(row)
    if source == "squad":
        return convert_squad(row)
    if source in ("fineweb", "cosmopedia"):
        return convert_web(row, source)
    if source == "stack-php":
        return convert_stack(row, args, fetch)
    return row.get("id"), "text", row.get(args.text_key), "prose", {}, None


def run(args, rows=None, count_tokens_fn=None):
    rows = rows if rows is not None else iter_rows(args)
    fetch = make_fetcher() if args.source == "stack-php" and not args.input else (lambda r: None)
    writer = ShardWriter(args.out, args.source, args.shard_docs)
    sft = open(args.sft_out, "a", encoding="utf-8") if args.sft_out else None
    stats, seen = Counter(), set()
    for row in rows:
        stats["rows"] += 1
        conv = convert_row(args.source, row, args, fetch)
        if conv is None:
            stats["skipped:filtered"] += 1
            continue
        native_id, task, text, kind, meta, messages = conv
        if sft is not None and messages:
            sft.write(json.dumps({"id": "%s:%s" % (args.source, native_id or stats["rows"]), "source": args.source, "task": task,
                                  "messages": [{"role": m["role"], "content": scrub_special(m["content"])} for m in messages]},
                                 ensure_ascii=False) + "\n")
        if text is None:                                    # SFT-only adapters
            continue
        rec, reason = finish(args.source, native_id, task, text, kind, meta, args)
        if reason:
            stats["skipped:" + reason] += 1
            continue
        if args.dedupe:
            h = hashlib.blake2b(rec["training_text"].encode("utf-8"), digest_size=8).digest()
            if h in seen:
                stats["skipped:duplicate"] += 1
                continue
            seen.add(h)
        n_tok = 0
        if count_tokens_fn:
            n_tok = count_tokens_fn(rec["training_text"])
            rec.setdefault("meta", {})["tokens"] = n_tok
        writer.write(rec, n_tok)
        if (args.max_docs and writer.docs >= args.max_docs) or (args.max_chars and writer.chars >= args.max_chars):
            break
    writer.close()
    if sft:
        sft.close()
    return update_manifest(args.out, args.source, writer, stats, args, args.tokenizer), stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, choices=["synthetic", "structured", "squad", "fineweb", "cosmopedia", "stack-php", "jsonl"])
    ap.add_argument("--out", required=True, help="output folder (one sub-folder per source + manifest.json)")
    ap.add_argument("--input", help="local JSONL instead of Hugging Face")
    ap.add_argument("--text-key", default="text", help="--source jsonl: field holding the text")
    ap.add_argument("--hf-path")
    ap.add_argument("--hf-config")
    ap.add_argument("--split", default="train")
    ap.add_argument("--shuffle-buffer", type=int, default=0, help="streaming sources: shuffle buffer (so a subset is not just the first shard)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--max-docs", type=int, default=0)
    ap.add_argument("--max-chars", type=int, default=0, help="stop after this many characters (about 4 per token)")
    ap.add_argument("--min-chars", type=int, default=20)
    ap.add_argument("--max-chars-doc", type=int, default=0, help="per-document limit (0 = none)")
    ap.add_argument("--truncate", action="store_true", help="truncate over-long documents instead of dropping them")
    ap.add_argument("--min-bytes", type=int, default=300, help="stack-php")
    ap.add_argument("--max-bytes", type=int, default=60000, help="stack-php")
    ap.add_argument("--shard-docs", type=int, default=50000)
    ap.add_argument("--dedupe", action="store_true", help="exact de-duplication on the cleaned text (memory: 8 bytes per document)")
    ap.add_argument("--sft-out", help="also write question/answer sources as chat `messages` (for the SFT stage)")
    ap.add_argument("--count-tokens", action="store_true", help="count tokens per document (needs --tokenizer)")
    ap.add_argument("--tokenizer", default="openai/gpt-oss-20b")
    args = ap.parse_args()
    counter = None
    if args.count_tokens:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(args.tokenizer)
        counter = lambda t: len(tok.encode(t, add_special_tokens=False))
    info, stats = run(args, count_tokens_fn=counter)
    print(json.dumps({"source": args.source, **{k: info[k] for k in ("docs", "chars", "tokens", "tokens_estimated", "skipped")}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
