#!/usr/bin/env python3
"""Tokenizer utility for the OpenMythos mixed-pretraining pipeline.

Commands:
  train  --input corpus.txt --out tokenizer/
  train  --jsonl synthetic/validated.jsonl --text-key training_text --out tokenizer/
  inspect --tokenizer tokenizer/ --text "..."
  encode  --tokenizer tokenizer/ --input corpus.txt --out ids.jsonl

Uses Hugging Face `tokenizers` to train a new BPE tokenizer. Existing compatible
HF tokenizers can be loaded with `--pretrained`. No existing project modules are modified.
"""
import argparse
import json
from pathlib import Path


def load_tokenizer(path=None, pretrained=None):
    try:
        from transformers import PreTrainedTokenizerFast
    except ImportError as e:
        raise SystemExit("Install transformers: pip install transformers tokenizers") from e
    if pretrained:
        return PreTrainedTokenizerFast.from_pretrained(pretrained)
    p = Path(path)
    if p.is_dir():
        p = p / "tokenizer.json"
    if not p.exists():
        raise SystemExit("Tokenizer not found: %s" % p)
    return PreTrainedTokenizerFast(
        tokenizer_file=str(p),
        bos_token="<|begin_of_text|>",
        eos_token="<|end_of_text|>",
        unk_token="<|unk|>",
        pad_token="<|pad|>",
    )


def iter_text(args):
    if args.input:
        with open(args.input, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    yield line.rstrip("\n")
    elif args.jsonl:
        with open(args.jsonl, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                value = row.get(args.text_key)
                if isinstance(value, str) and value.strip():
                    yield value
    else:
        raise SystemExit("Provide --input or --jsonl")


def train(args):
    try:
        from tokenizers import Tokenizer, models, trainers, pre_tokenizers, decoders, processors
    except ImportError as e:
        raise SystemExit("Install tokenizer dependencies: pip install tokenizers transformers") from e

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    special = ["<|begin_of_text|>", "<|end_of_text|>", "<|unk|>", "<|pad|>",
               "<|system|>", "<|user|>", "<|assistant|>", "<|end_of_turn|>"]
    tok = Tokenizer(models.BPE(unk_token="<|unk|>"))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=args.vocab_size, min_frequency=args.min_frequency,
        special_tokens=special, initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        show_progress=True,
    )
    tok.train_from_iterator(iter_text(args), trainer=trainer, length=args.length or None)
    bos = tok.token_to_id("<|begin_of_text|>")
    eos = tok.token_to_id("<|end_of_text|>")
    tok.post_processor = processors.TemplateProcessing(
        single="$A", pair="$A $B",
        special_tokens=[("<|begin_of_text|>", bos), ("<|end_of_text|>", eos)]
    )
    tok.save(str(out / "tokenizer.json"))
    (out / "tokenizer_config.json").write_text(json.dumps({
        "tokenizer_class": "PreTrainedTokenizerFast",
        "bos_token": "<|begin_of_text|>", "eos_token": "<|end_of_text|>",
        "unk_token": "<|unk|>", "pad_token": "<|pad|>",
        "model_max_length": args.model_max_length,
        "clean_up_tokenization_spaces": False,
    }, indent=2), encoding="utf-8")
    (out / "special_tokens_map.json").write_text(json.dumps({
        "bos_token": "<|begin_of_text|>", "eos_token": "<|end_of_text|>",
        "unk_token": "<|unk|>", "pad_token": "<|pad|>",
    }, indent=2), encoding="utf-8")
    print("Saved %s (vocab=%d, BOS=%s, EOS=%s)" %
          (out, tok.get_vocab_size(), bos, eos))


def inspect(args):
    tok = load_tokenizer(args.tokenizer, args.pretrained)
    ids = tok.encode(args.text, add_special_tokens=False)
    print(json.dumps({
        "characters": len(args.text), "tokens": len(ids),
        "chars_per_token": round(len(args.text) / max(1, len(ids)), 3),
        "token_ids": ids[:args.show_ids],
        "decoded": tok.decode(ids),
        "vocab_size": len(tok),
        "bos_token_id": tok.bos_token_id, "eos_token_id": tok.eos_token_id,
    }, ensure_ascii=False, indent=2))


def encode_file(args):
    tok = load_tokenizer(args.tokenizer, args.pretrained)
    with open(args.input, encoding="utf-8", errors="replace") as src, \
         open(args.out, "w", encoding="utf-8") as dst:
        for line_no, line in enumerate(src, 1):
            if line.strip():
                ids = tok.encode(line.rstrip("\n"), add_special_tokens=False)
                dst.write(json.dumps({"line": line_no, "input_ids": ids}) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--input")
    t.add_argument("--jsonl")
    t.add_argument("--text-key", default="training_text")
    t.add_argument("--out", required=True)
    t.add_argument("--vocab-size", type=int, default=16000)
    t.add_argument("--min-frequency", type=int, default=2)
    t.add_argument("--model-max-length", type=int, default=2048)
    t.add_argument("--length", type=int, default=0, help="optional approximate iterator length")
    t.set_defaults(fn=train)
    i = sub.add_parser("inspect")
    i.add_argument("--tokenizer", default="tokenizer")
    i.add_argument("--pretrained")
    i.add_argument("--text", required=True)
    i.add_argument("--show-ids", type=int, default=80)
    i.set_defaults(fn=inspect)
    e = sub.add_parser("encode")
    e.add_argument("--tokenizer", default="tokenizer")
    e.add_argument("--pretrained")
    e.add_argument("--input", required=True)
    e.add_argument("--out", required=True)
    e.set_defaults(fn=encode_file)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
