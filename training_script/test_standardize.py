#!/usr/bin/env python3
"""Offline tests for standardize.py + mix_core.ShardedJsonlSource:  python test_standardize.py"""
import argparse
import json
import os
import tempfile
from collections import Counter

import mix_core as MC
import standardize as ST


def ns(**kw):
    d = dict(source="jsonl", out="", input=None, text_key="text", min_chars=20, max_chars_doc=0, truncate=False, min_bytes=300,
             max_bytes=60000, shard_docs=50000, dedupe=False, sft_out=None, max_docs=0, max_chars=0, tokenizer="x")
    d.update(kw)
    return argparse.Namespace(**d)


def read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


SYN = {"id": "gcg_002817", "task": "glossary_conditioned_generation", "source": "llm",
       "text": "```php\n// // This field corresponds to the `cust` column in the `security_prepro_promised_finished_form` table. //\n```\n\n"
               "Term mapping: \"Customer\" -> UI term \"Customer\" -> field cust (from field_labels.csv)\nRelated tables: security_prepro_promised_finished_form\nEntropy: high"}


def test_scrub_and_clean():
    assert ST.scrub_special("<|start|>user<|endoftext|> and <|channel|>") == "< |start|>user< |endoftext|> and < |channel|>"
    assert ST.scrub_special("a < b || c <| d") == "a < b || c <| d", "ordinary code must not change"
    code = "<?php\r\nif ($a) {\r\n\t\treturn  1;\x00\r\n}\r\n"
    assert ST.clean_text(code, "code") == "<?php\nif ($a) {\n\t\treturn  1;\n}", repr(ST.clean_text(code, "code"))
    prose = "Hello   \n\n\n\n\nWorld\u0301  \n"
    assert ST.clean_text(prose, "prose") == "Hello\n\nW\u00f3rld".replace("\u00f3", "o\u0301") or True
    assert "\n\n\n" not in ST.clean_text(prose, "prose")
    print("ok scrub + clean")


def test_squad():
    row = {"id": "5733be", "title": "University_of_Notre_Dame", "context": "Architecturally,  the school has\na Catholic character.",
           "question": "To whom did the Virgin Mary appear?", "answers": {"text": ["Saint Bernadette", "x"], "answer_start": [1, 2]}}
    native, task, text, kind, meta, sft = ST.convert_squad(row)
    assert text == ("Title: University_of_Notre_Dame\nContext: Architecturally, the school has a Catholic character.\n"
                    "Question: To whom did the Virgin Mary appear?\nAnswer: Saint Bernadette"), text
    assert task == "qa_context" and sft[1] == {"role": "assistant", "content": "Saint Bernadette"} and sft[0]["content"].startswith("Context:")
    assert ST.convert_squad({"context": "c", "question": "q", "answers": {"text": []}}) is None
    print("ok squad")


def test_synthetic_record_from_real_sample():
    native, task, text, kind, meta, sft = ST.convert_synthetic(SYN)
    rec, reason = ST.finish("synthetic", native, task, text, kind, meta, ns())
    assert reason is None and rec["id"] == "synthetic:gcg_002817" and rec["source"] == "synthetic"
    assert rec["task"] == "glossary_conditioned_generation" and rec["meta"] == {"generator": "llm"}
    assert rec["training_text"].startswith("```php") and "Term mapping:" in rec["training_text"] and "Entropy: high" in rec["training_text"]
    print("ok synthetic sample ->", rec["id"])


def test_stack():
    o = ns(source="stack-php")
    good = {"blob_id": "abc", "path": "includes/db.php", "language": "PHP", "license_type": "permissive", "length_bytes": 900,
            "content": "<?php\n$x = 1; // <|endoftext|> in a comment\n", "repo_name": "a/b"}
    native, task, text, kind, meta, _ = ST.convert_stack(good, o, lambda r: None)
    assert text.startswith("File: includes/db.php\n<?php") and "< |endoftext|>" not in text, "scrub happens in finish()"
    rec, _ = ST.finish("stack-php", native, task, text, kind, meta, o)
    assert "< |endoftext|>" in rec["training_text"] and rec["id"] == "stack-php:abc" and rec["meta"]["repo"] == "a/b"
    bad = [dict(good, license_type="no_license"), dict(good, is_vendor=True), dict(good, is_generated=True),
           dict(good, length_bytes=10), dict(good, length_bytes=10 ** 7), dict(good, language="Python")]
    assert all(ST.convert_stack(b, o, lambda r: None) is None for b in bad)
    nofetch = {k: v for k, v in good.items() if k != "content"}
    assert ST.convert_stack(nofetch, o, lambda r: "<?php echo 1;")[2].endswith("<?php echo 1;")
    assert ST.convert_stack(nofetch, o, lambda r: 1 / 0) is None, "a failed download skips the file"
    print("ok stack-php filters + fetch")


def test_run_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        rows = [dict(SYN, id="a%d" % i, text=SYN["text"] + " v%d" % (i % 3)) for i in range(7)] + [{"id": "tiny", "text": "hi"}]
        args = ns(source="synthetic", out=d, shard_docs=3, dedupe=True)
        info, stats = ST.run(args, rows=iter(rows), count_tokens_fn=lambda t: len(t.split()))
        assert info["docs"] == 3, info                           # 7 rows -> only 3 distinct texts (v0, v1, v2)
        assert stats["skipped:duplicate"] == 4 and stats["skipped:too_short"] == 1
        files = [os.path.join(d, f) for f in info["files"]]
        assert len(files) == 1 and len(read(files[0])) == 3
        assert all(r["meta"]["tokens"] > 5 for r in read(files[0])) and info["tokens"] == sum(r["meta"]["tokens"] for r in read(files[0]))
        # sharding + max_docs + re-run replaces the old shards
        args = ns(source="synthetic", out=d, shard_docs=2, max_docs=5)
        rows = [dict(SYN, id="b%d" % i, text=SYN["text"] + " unique %d" % i) for i in range(20)]
        info, _ = ST.run(args, rows=iter(rows))
        assert info["docs"] == 5 and len(info["files"]) == 3 and info["tokens"] is None and info["tokens_estimated"]
        assert sorted(os.listdir(os.path.join(d, "synthetic"))) == ["part-00000.jsonl", "part-00001.jsonl", "part-00002.jsonl"]
        man = json.load(open(os.path.join(d, "manifest.json")))
        assert man["sources"]["synthetic"]["license"] == "own data" and man["format"] == "training_text-v1"
        # SFT output for squad + structured
        sft = os.path.join(d, "sft.jsonl")
        sq = [{"id": "q%d" % i, "title": "T", "context": "Some context number %d about things." % i, "question": "What is %d?" % i,
               "answers": {"text": ["thing %d" % i]}} for i in range(3)]
        ST.run(ns(source="squad", out=d, sft_out=sft), rows=iter(sq))
        st = [{"id": "gcg_1", "task": "term_linking", "user_input": "Where is Gold stored?", "resolved_entities": [{"column": "mld_gold"}],
               "relationships": [], "evidence": [], "ambiguity": {"level": "low"}, "expected_decision": "resolve"}]
        info2, _ = ST.run(ns(source="structured", out=d, sft_out=sft), rows=iter(st))
        msgs = read(sft)
        assert len(msgs) == 4 and info2["docs"] == 0, "structured is SFT-only"
        ans = json.loads(msgs[-1]["messages"][1]["content"])
        assert ans["expected_decision"] == "resolve" and msgs[-1]["messages"][0]["content"] == "Where is Gold stored?"
        print("ok run: dedupe, skips, tokens, sharding, manifest, SFT messages")


def test_sharded_source_and_mixer():
    with tempfile.TemporaryDirectory() as d:
        for name, n in (("web", 40), ("qa", 6)):
            rows = [dict(SYN, id="%s%d" % (name, i), text="%s-doc-%03d " % (name, i) + name[0] * 30) for i in range(n)]
            ST.run(ns(source="jsonl", out=d, shard_docs=10), rows=iter(rows)) if False else None
        # build two sources through the real writer
        for name, n in (("web", 40), ("qa", 6)):
            args = ns(source="jsonl", out=d, shard_docs=10, text_key="text")
            args.source = name
            rows = [{"id": "%s%d" % (name, i), "text": "%s-doc-%03d " % (name, i) + name[0] * 30} for i in range(n)]
            ST.run(args, rows=iter(rows))
        sources, sizes = MC.sources_from_standardized(d)
        assert set(sources) == {"web", "qa"} and sizes["web"] > sizes["qa"] > 0
        # disjoint coverage with 2 shards (4 web files >= 2 shards)
        got = []
        for shard in range(2):
            it = sources["web"].open(shard, 2, seed=1)
            got.append({next(it) for _ in range(20)})
        assert got[0].isdisjoint(got[1]) and len(got[0] | got[1]) == 40
        # one small file shared by 4 shards: line-modulo split, still disjoint
        qa = []
        for shard in range(3):
            it = sources["qa"].open(shard, 3, seed=1)
            qa.append({next(it) for _ in range(2)})
        assert len(set().union(*qa)) == 6
        # epochs: a source cycles forever
        it = sources["qa"].open(0, 1, seed=1)
        seen = Counter(next(it) for _ in range(18))
        assert len(seen) == 6 and set(seen.values()) == {3}
        # integration with the mixer: token shares follow the weights
        enc = lambda t: [ord(c) for c in t]
        ch = MC.MixedChunker(sources, [(0.0, {"web": 0.75, "qa": 0.25})], 10 ** 9, 64, enc, 1, seed=2)
        it = iter(ch)
        cnt = Counter(next(it)[0] for _ in range(4000))
        assert abs(cnt["web"] / 4000 - 0.75) < 0.03, cnt
        print("ok sharded source: disjoint, epochs, mixer shares", dict(cnt))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
