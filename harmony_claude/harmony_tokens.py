#!/usr/bin/env python3
"""harmony_tokens.py - single source of truth for the GPT-2 + Harmony vocabulary (pure Python).

GPT-2 byte-level BPE:  ids 0 .. 50255 are BPE tokens, id 50256 is <|endoftext|>  (50 257 ids).
Harmony tokens are appended as ADDED SPECIAL TOKENS (never split by the BPE, matched in raw text):

    50257 <|start|>   50258 <|message|>   50259 <|channel|>   50260 <|end|>   50261 <|return|>

  tokenizer vocabulary = 50 262 ids (0 .. 50261)
  model vocabulary     = 50 304 = 50 262 padded to a multiple of 64 (tensor-core friendly); ids 50262 .. 50303 are never produced.
The embedding / output matrix and the loss must use MODEL_VOCAB_SIZE, never `tokenizer.vocab_size` (which does not count added tokens).
"""


def _tok(id_, content, normalized=False):
    return {"id": id_, "content": content, "single_word": False, "lstrip": False, "rstrip": False,
            "normalized": normalized, "special": True}


# the `added_tokens` entries of the extended tokenizer.json (id order)
HARMONY_TOKENS = [
    _tok(50256, "<|endoftext|>", normalized=True),      # already part of GPT-2
    _tok(50257, "<|start|>"),
    _tok(50258, "<|message|>"),
    _tok(50259, "<|channel|>"),
    _tok(50260, "<|end|>"),
    _tok(50261, "<|return|>"),
]
TOKEN_IDS = {t["content"]: t["id"] for t in HARMONY_TOKENS}
EOS_ID = TOKEN_IDS["<|endoftext|>"]
START_ID, MESSAGE_ID, CHANNEL_ID = TOKEN_IDS["<|start|>"], TOKEN_IDS["<|message|>"], TOKEN_IDS["<|channel|>"]
END_ID, RETURN_ID = TOKEN_IDS["<|end|>"], TOKEN_IDS["<|return|>"]
BASE_MAX_BPE_ID = 50256                                   # highest id that belongs to the GPT-2 base tokenizer
TOKENIZER_VOCAB_SIZE = max(TOKEN_IDS.values()) + 1        # 50262
TOKEN_DTYPE = "uint16"                                    # every id < 65536


def padded_vocab_size(multiple=64):
    return -(-TOKENIZER_VOCAB_SIZE // multiple) * multiple


MODEL_VOCAB_SIZE = padded_vocab_size(64)                  # 50304
