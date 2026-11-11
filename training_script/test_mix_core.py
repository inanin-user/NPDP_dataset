#!/usr/bin/env python3
"""Offline tests for mix_core.py (no torch / datasets needed):  python test_mix_core.py"""
import json
import os
import random
import tempfile
from collections import Counter

import mix_core as MC

EOS = 1
SEQ = 64


def make_sources():
    # different document lengths on purpose: a document-level mix would NOT give the token shares
    return {
        "web": MC.ListSource("web", ["a" * 1000 + str(i) for i in range(50)]),
        "code": MC.ListSource("code", ["c" * 3000 + str(i) for i in range(20)]),
        "qa": MC.ListSource("qa", ["q" * 200 + str(i) for i in range(30)]),
    }


def enc(text):
    return [ord(c) for c in text]


def source_of(chunk):
    letters = {chr(t) for t in chunk if t != EOS}
    for name, ch in (("web", "a"), ("code", "c"), ("qa", "q")):
        if ch in letters:
            return name
    return "?"


def test_token_shares_and_purity():
    w = {"web": 0.6, "code": 0.3, "qa": 0.1}
    ch = MC.MixedChunker(make_sources(), [(0.0, w)], total_tokens=10 ** 9, seq_len=SEQ, encode=enc, eos_id=EOS, seed=3)
    it = iter(ch)
    n = 20000
    cnt = Counter()
    for _ in range(n):
        name, chunk = next(it)
        assert len(chunk) == SEQ + 1
        assert source_of(chunk) == name, "a chunk must come from exactly one source"
        cnt[name] += 1
    for k, v in w.items():
        assert abs(cnt[k] / n - v) < 0.01, (k, cnt[k] / n, v)
    print("ok token shares:", {k: round(cnt[k] / n, 3) for k in w})


def test_document_level_mixing_would_be_wrong():
    # 10 % of DOCUMENTS from qa (200 chars) vs 90 % web (1000 chars): the qa TOKEN share is far below 10 %
    docs = [("web", 1000)] * 90 + [("qa", 200)] * 10
    tok = Counter()
    for k, n in docs:
        tok[k] += n
    share = tok["qa"] / sum(tok.values())
    assert share < 0.03
    print("ok document share 10%% -> token share %.1f%% (why chunk-level mixing is used)" % (100 * share))


def test_stage_switch_and_resume():
    stages = [(0.0, {"web": 1.0}), (0.5, {"qa": 1.0})]
    total = 200 * SEQ * 1                      # 200 chunks for one shard
    ch = MC.MixedChunker(make_sources(), stages, total, SEQ, enc, EOS, seed=1)
    names = [n for _, (n, _) in zip(range(200), iter(ch))]
    assert set(names[:100]) == {"web"} and set(names[100:]) == {"qa"}, "stage switch at 50 %"
    ch2 = MC.MixedChunker(make_sources(), stages, total, SEQ, enc, EOS, seed=1, start_tokens=150 * SEQ)
    names2 = [n for _, (n, _) in zip(range(20), iter(ch2))]
    assert set(names2) == {"qa"}, "resume at 75 % must start in the second stage"
    print("ok stage switch + resume")


def test_sharding_is_disjoint():
    docs = ["doc%03d" % i for i in range(37)]
    src = MC.ListSource("x", docs)
    seen = []
    for shard in range(4):
        it = src.open(shard, 4, seed=0)
        part = {next(it) for _ in range(len(docs[shard::4]))}
        seen.append(part)
    assert sum(len(p) for p in seen) == 37 and len(set().union(*seen)) == 37
    # more shards than documents must not crash
    tiny = MC.ListSource("t", ["only"])
    assert next(tiny.open(5, 8, 0)) == "only"
    print("ok disjoint shards")


def test_small_source_repeats_as_epochs():
    src = MC.ListSource("s", ["a", "b", "c"])
    it = src.open(0, 1, 0)
    got = [next(it) for _ in range(9)]
    assert Counter(got) == Counter({"a": 3, "b": 3, "c": 3})
    print("ok epochs")


def test_cap_weights():
    stages = [(0.0, {"web": 0.6, "code": 0.2, "qa": 0.1, "syn": 0.1}), (0.85, {"web": 0.4, "code": 0.2, "qa": 0.2, "syn": 0.2})]
    sizes = {"qa": 18e6, "syn": 0.2e6}
    new, rep = MC.cap_weights(stages, sizes, 2.5e9, max_epochs=5)
    for s, w in new:
        assert abs(sum(w.values()) - 1) < 1e-9
    assert rep["syn"][0] > 1000 and rep["syn"][1] <= 5.001, rep
    assert rep["qa"][1] <= 5.001
    spans = [t for t, _ in MC.stage_spans(new, 2.5e9)]
    used_syn = sum(w["syn"] * t for t, (_, w) in zip(spans, new))
    assert used_syn <= 5 * 0.2e6 * 1.001
    print("ok cap_weights: syn %.0f -> %.1f epochs, qa %.1f -> %.1f epochs" % (rep["syn"][0], rep["syn"][1], rep["qa"][0], rep["qa"][1]))


def test_parse_and_normalise():
    w = MC.parse_mix("a=2, b=1,c=1")
    assert w == {"a": 2.0, "b": 1.0, "c": 1.0}
    assert abs(sum(MC.normalise(w).values()) - 1) < 1e-12
    print("ok parse_mix")


def test_squad_and_jsonl():
    row = {"context": "Paris is the capital of France.", "question": "What is the capital of France?", "answers": {"text": ["Paris"]}}
    texts = {MC.format_squad(row, random.Random(i)) for i in range(40)}
    assert any(t.startswith("Context:") for t in texts) and any(t.startswith("Question:") for t in texts)
    assert all("\nAnswer: Paris" in t for t in texts)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "p.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"text": "hello"}) + "\n" + json.dumps({"text": ""}) + "\n" + json.dumps({"other": 1}) + "\n")
        s = MC.JsonlSource("syn", p)
        assert s.doc_list() == ["hello"]
    print("ok squad format + jsonl")


def test_max_doc_tokens_and_eos():
    src = {"web": MC.ListSource("web", ["a" * 500])}
    ch = MC.MixedChunker(src, [(0.0, {"web": 1.0})], 10 ** 6, 32, enc, EOS, max_doc_tokens=100)
    _, chunk = next(iter(ch))
    assert len(chunk) == 33
    it = iter(MC.MixedChunker(src, [(0.0, {"web": 1.0})], 10 ** 6, 32, enc, EOS, max_doc_tokens=10))
    chunks = [next(it)[1] for _ in range(3)]
    assert all(EOS in c for c in chunks), "documents are separated by EOS"
    print("ok max_doc_tokens + EOS")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
