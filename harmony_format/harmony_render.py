#!/usr/bin/env python3
"""harmony_render.py - render conversations in the Harmony response format (gpt-oss), pure Python, no dependencies.

Format (OpenAI cookbook "Harmony response format"):
    <|start|>{header}<|message|>{content}<|end|>          messages follow each other with NO separator
    header   = user | system | developer | assistant<|channel|>{analysis|final|commentary}
    training = the LAST assistant `final` message ends with <|return|>; everything else (all history) ends with <|end|>
    history  = `analysis` messages of earlier assistant turns are dropped (as at inference); only the last turn keeps its analysis
    inference prompt = ... <|start|>assistant        (the model then writes <|channel|>analysis<|message|> ... )

Special-token strings that appear INSIDE message content are neutralised ("< |start|>") so user text can never inject control
tokens. Rendering a conversation as text and tokenising it with the gpt-oss tokenizer (special tokens parsed, the default)
gives the same ids as the official renderer for these simple chats; run check_tokenizer.py once to verify on your machine.

Loss segments (for the SFT stage): render(..., with_segments=True) also returns [(text, trainable)], where only what the model
generates is trainable: for every assistant turn the header "<|start|>assistant" is prompt, the rest (<|channel|>analysis ...,
<|channel|>final ... <|return|>) is trainable; user / system / developer messages are never trainable.
"""
import re

START, MESSAGE, END, CHANNEL, RETURN = "<|start|>", "<|message|>", "<|end|>", "<|channel|>", "<|return|>"
SPECIAL_TOKENS = ["<|startoftext|>", "<|endoftext|>", "<|return|>", "<|constrain|>", "<|channel|>", "<|start|>", "<|end|>",
                  "<|message|>", "<|call|>"]
SPECIAL_RE = re.compile(r"<\|[A-Za-z0-9_]+\|>")
ROLES = ("system", "developer", "user", "assistant")
CHANNELS = ("analysis", "final", "commentary")


def scrub_special(text):
    """'<|start|>' -> '< |start|>' so the tokenizer sees ordinary text instead of a control token."""
    return SPECIAL_RE.sub(lambda m: "< |" + m.group(0)[2:], text)


def _normalise(messages):
    out = []
    for m in messages:
        role = m["role"]
        if role not in ROLES:
            raise ValueError("unknown role %r" % role)
        content = scrub_special(str(m.get("content", "")).replace("\r\n", "\n").strip())
        if not content:
            continue
        channel = m.get("channel") or ("final" if role == "assistant" else None)
        if role == "assistant" and channel not in CHANNELS:
            raise ValueError("unknown channel %r" % channel)
        out.append({"role": role, "channel": channel, "content": content})
    if not out:
        raise ValueError("empty conversation")
    return out


def _kept(msgs):
    last_user = max((i for i, m in enumerate(msgs) if m["role"] == "user"), default=-1)
    return [m for i, m in enumerate(msgs) if not (m["role"] == "assistant" and m["channel"] == "analysis" and i < last_user)]


def _header(m):
    return m["role"] + (CHANNEL + m["channel"] if m["role"] == "assistant" else "")


def render(messages, training=True, with_segments=False):
    """Conversation -> Harmony text (and optionally loss segments)."""
    kept = _kept(_normalise(messages))
    parts, segs, prev = [], [], None
    for i, m in enumerate(kept):
        last = i == len(kept) - 1
        end = RETURN if (training and last and m["role"] == "assistant" and m["channel"] == "final") else END
        text = START + _header(m) + MESSAGE + m["content"] + end
        parts.append(text)
        if m["role"] != "assistant":
            segs.append((text, False))
        elif prev == "assistant":                      # analysis -> final: both written by the model
            segs.append((text, True))
        else:                                          # "<|start|>assistant" is the prompt, the rest is generated
            head = START + "assistant"
            segs.append((head, False))
            segs.append((text[len(head):], True))
        prev = m["role"]
    out = "".join(parts)
    return (out, segs) if with_segments else out


def render_for_completion(messages):
    """Prompt for inference: the whole history (every message ends with <|end|>) + '<|start|>assistant'."""
    kept = _kept(_normalise(messages))
    return "".join(START + _header(m) + MESSAGE + m["content"] + END for m in kept) + START + "assistant"


def parse_completion(text):
    """Split what the model wrote after '<|start|>assistant' into {'analysis': ..., 'final': ...} (stops at <|return|>)."""
    text = "<|start|>assistant" + text if not text.startswith(START) else text
    out = {}
    for m in re.finditer(r"<\|start\|>assistant<\|channel\|>(\w+)<\|message\|>(.*?)(?:<\|end\|>|<\|return\|>|$)", text, re.S):
        out[m.group(1)] = m.group(2).strip()
    return out
