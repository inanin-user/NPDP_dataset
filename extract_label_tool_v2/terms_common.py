#!/usr/bin/env python3
"""terms_common.py - shared normalisation so that the SAME business term written differently in an
email and in PHP source ends up with the same key.

    "W/S No."  /  "WS no"  /  "Worksheet Number"      ->  ['ws', 'no']
    "PWS"  /  "Production WorkSheet"                   ->  ['production', 'ws']

Aliases are editable: pass a JSON file {"token": ["canonical", "tokens"]} to load_aliases().
The defaults below come from wording seen in your tickets (pws, W/S#, ws no); confirm with a business owner.
"""
import html
import json
import re

DEFAULT_ALIASES = {
    "w/s": ["ws"], "worksheet": ["ws"], "worksheets": ["ws"],
    "pws": ["production", "ws"],
    "number": ["no"], "num": ["no"], "nos": ["no"],
}
# single-token terms that are too generic to be useful as an email -> code link
GENERIC_SINGLE = {"no", "date", "status", "code", "name", "type", "qty", "remarks", "remark", "id", "the", "and",
                  "of", "form", "list", "report", "page", "ws", "view", "edit", "add", "save", "back", "yes", "ok"}
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])w/s(?![A-Za-z0-9])|[A-Za-z0-9]+", re.I)
TAG_RE = re.compile(r"<[^>]*>")


def load_aliases(path=None):
    aliases = dict(DEFAULT_ALIASES)
    if path:
        with open(path, "r", encoding="utf-8-sig") as f:
            for k, v in json.load(f).items():
                aliases[k.lower()] = v if isinstance(v, list) else str(v).lower().split()
    return aliases


def clean_text(s):
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s).replace("\u00a0", " ")
    return " ".join(s.split())


def tokenize(text, aliases=None):
    """[(token, start, end)] lower-cased, alias-expanded; expanded tokens keep the span of their source word."""
    aliases = DEFAULT_ALIASES if aliases is None else aliases
    out = []
    for m in TOKEN_RE.finditer(text):
        t = m.group(0).lower()
        for x in aliases.get(t, [t]):
            out.append((x, m.start(), m.end()))
    return out


def norm_tokens(text, aliases=None):
    return [t for t, _, _ in tokenize(clean_text(text), aliases)]


def norm_key(text, aliases=None):
    return " ".join(norm_tokens(text, aliases))


def is_useful_key(tokens):
    """Skip keys that would match everywhere (empty, or one generic/very short token)."""
    if not tokens:
        return False
    if len(tokens) == 1 and (tokens[0] in GENERIC_SINGLE or len(tokens[0]) < 4 or tokens[0].isdigit()):
        return False
    return True
