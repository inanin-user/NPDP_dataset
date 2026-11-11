#!/usr/bin/env python3
"""mix_core.py - weighted, TOKEN-exact dataset mixing for pre-training. Pure Python: torch / datasets / boto3 are only
imported inside the source classes that need them, so the logic can be unit-tested anywhere.

Why not datasets.interleave_datasets(probabilities=...)?  It mixes DOCUMENTS. A SQuAD row has ~200 tokens, a PHP file ~2 000,
a FineWeb-Edu page ~1 000, so a 5 % document share is NOT a 5 % token share. Here every source is packed separately into
fixed-length chunks (seq_len + 1 tokens) and a chunk is drawn from source `s` with probability weight[s]; all chunks have the
same length, so the weights ARE token shares, and a chunk never mixes two sources.

Concepts
  Source         stream of documents, cycles forever (small sources repeat = epochs)
  stages         [(start_fraction, {source: weight})]  e.g. general mix first, domain-heavy mix for the last 15 %
  MixedChunker   per-(rank, worker) generator of token chunks; progress is derived from the chunk counter, so the
                 schedule needs no inter-process communication; `start_tokens` restores the schedule after a resume
  cap_weights    limits the weight of small sources so that they are not repeated more than `max_epochs` times
"""
import json
import os
import random
from collections import Counter

# ----------------------------------------------------------------- model presets (GQA, single 16 GB GPU)
COMMON = dict(attn_type="gqa", lora_rank=8, act_threshold=0.99, rope_theta=500000.0)
PRESETS = {
    # name: overrides on top of open_mythos.variants.mythos_1b()  - verify the real count with `train_mix.py --dry-run`
    "s":   dict(dim=768,  n_heads=12, n_kv_heads=4, max_loop_iters=4, prelude_layers=1, coda_layers=1,
                n_experts=32, n_shared_experts=1, n_experts_per_tok=2, expert_dim=768),
    "m32": dict(dim=1024, n_heads=16, n_kv_heads=4, max_loop_iters=4, prelude_layers=1, coda_layers=1,
                n_experts=32, n_shared_experts=1, n_experts_per_tok=2, expert_dim=1024),
    "m64": dict(dim=1024, n_heads=16, n_kv_heads=4, max_loop_iters=4, prelude_layers=1, coda_layers=1,
                n_experts=64, n_shared_experts=1, n_experts_per_tok=4, expert_dim=512),
}


# ----------------------------------------------------------------- sources
class Source:
    """open(shard, num_shards, seed) -> infinite iterator of documents (str)."""
    name = ""

    def open(self, shard, num_shards, seed):
        raise NotImplementedError

    def doc_list(self):
        """All documents for small in-memory sources (used to measure their size), else None."""
        return None


class ListSource(Source):
    """In-memory documents; each worker owns a disjoint slice and reshuffles it every epoch."""

    def __init__(self, name, docs):
        self.name, self.docs = name, list(docs)

    def doc_list(self):
        return self.docs

    def open(self, shard, num_shards, seed):
        if not self.docs:
            raise ValueError("source %s has no documents" % self.name)
        mine = self.docs[shard::num_shards] or [self.docs[shard % len(self.docs)]]
        rng = random.Random(seed)
        while True:
            order = list(range(len(mine)))
            rng.shuffle(order)
            for i in order:
                yield mine[i]


class JsonlSource(ListSource):
    """Local JSONL (for example the validated synthetic pretrain.jsonl): one document per line, field `text_key`."""

    def __init__(self, name, path, text_key="text"):
        docs = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    t = json.loads(line).get(text_key)
                    if isinstance(t, str) and t.strip():
                        docs.append(t)
        super().__init__(name, docs)


def format_squad(row, rng):
    """SQuAD row -> plain text with the words Context / Question / Answer (70 % with context, 30 % closed-book)."""
    q = " ".join(row["question"].split())
    a = " ".join((row["answers"]["text"] or [""])[0].split())
    if rng.random() < 0.7:
        return "Context: %s\nQuestion: %s\nAnswer: %s" % (" ".join(row["context"].split()), q, a)
    return "Question: %s\nAnswer: %s" % (q, a)


class SquadSource(Source):
    def __init__(self, name="squad", hf_path="rajpurkar/squad"):
        self.name, self.hf_path, self._rows = name, hf_path, None

    def _load(self):
        if self._rows is None:
            from datasets import load_dataset
            self._rows = list(load_dataset(self.hf_path, split="train"))
        return self._rows

    def doc_list(self):
        rng = random.Random(0)
        return [format_squad(r, rng) for r in self._load()]

    def open(self, shard, num_shards, seed):
        rows = self._load()
        mine = rows[shard::num_shards] or [rows[shard % len(rows)]]
        rng = random.Random(seed)
        while True:
            order = list(range(len(mine)))
            rng.shuffle(order)
            for i in order:
                yield format_squad(mine[i], rng)


class HFStreamSource(Source):
    """Streaming Hugging Face dataset, e.g. HFStreamSource('fineweb', 'HuggingFaceTB/smollm-corpus', 'fineweb-edu-dedup')."""

    def __init__(self, name, path, config=None, split="train", text_key="text", shuffle_buffer=5000):
        self.name, self.path, self.config, self.split = name, path, config, split
        self.text_key, self.shuffle_buffer = text_key, shuffle_buffer

    def _rows(self, shard, num_shards, seed):
        from datasets import load_dataset
        ds = load_dataset(self.path, self.config, split=self.split, streaming=True)
        if num_shards > 1:
            ds = ds.shard(num_shards=num_shards, index=shard)
        return ds.shuffle(seed=seed, buffer_size=self.shuffle_buffer)

    def open(self, shard, num_shards, seed):
        epoch = 0
        while True:
            for row in self._rows(shard, num_shards, seed + epoch):
                t = row.get(self.text_key)
                if isinstance(t, str) and t.strip():
                    yield t
            epoch += 1


class StackCodeSource(HFStreamSource):
    """Code files from bigcode/the-stack-v2 (or any dataset with the same columns, e.g. starcoderdata).

    The Stack v2 usually ships blob ids, not file contents: when a row has no `content` it is fetched from the Software
    Heritage S3 bucket (see the dataset card: access terms, credentials and key layout - VERIFY before a long run).
    Rows are filtered by licence type, vendor / generated flags and size; every document starts with `File: <path>` so
    the model sees file name <-> content pairs.
    """

    def __init__(self, name, path="bigcode/the-stack-v2", config="PHP", license_types=("permissive",),
                 min_bytes=300, max_bytes=60000, bucket="softwareheritage", key_prefix="content/"):
        super().__init__(name, path, config, "train", "content", 2000)
        self.license_types, self.min_bytes, self.max_bytes = set(license_types), min_bytes, max_bytes
        self.bucket, self.key_prefix, self._s3 = bucket, key_prefix, None

    def _fetch(self, row):
        import gzip
        if self._s3 is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
            kw = {}
            if os.environ.get("AWS_ACCESS_KEY_ID"):
                kw = dict(aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
                          aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
            self._s3 = boto3.client("s3", **kw) if kw else boto3.client("s3", config=Config(signature_version=UNSIGNED))
        obj = self._s3.get_object(Bucket=self.bucket, Key=self.key_prefix + row["blob_id"])
        with gzip.GzipFile(fileobj=obj["Body"]) as fin:
            return fin.read().decode(row.get("src_encoding") or "utf-8", errors="ignore")

    def open(self, shard, num_shards, seed):
        epoch = 0
        while True:
            for row in self._rows(shard, num_shards, seed + epoch):
                lt = row.get("license_type")
                if lt is not None and lt not in self.license_types:
                    continue
                if row.get("is_vendor") or row.get("is_generated"):
                    continue
                n = row.get("length_bytes")
                if n is not None and not (self.min_bytes <= n <= self.max_bytes):
                    continue
                text = row.get("content")
                if not text:
                    try:
                        text = self._fetch(row)
                    except Exception:
                        continue                       # missing blob / network hiccup: skip the file
                if text and self.min_bytes <= len(text) <= self.max_bytes:
                    yield "File: %s\n%s" % (row.get("path", ""), text)
            epoch += 1


# ----------------------------------------------------------------- weights
def parse_mix(s):
    """'fineweb=0.55,cosmo=0.15' -> {'fineweb': 0.55, 'cosmo': 0.15} (not normalised)."""
    out = {}
    for part in (s or "").split(","):
        if part.strip():
            k, v = part.split("=")
            out[k.strip()] = float(v)
    return out


def normalise(w):
    t = sum(w.values())
    return {k: v / t for k, v in w.items()} if t > 0 else dict(w)


def stage_spans(stages, total_tokens):
    """[(tokens_in_stage, weights)] for stages [(start_fraction, weights)]."""
    out = []
    for i, (start, w) in enumerate(stages):
        end = stages[i + 1][0] if i + 1 < len(stages) else 1.0
        out.append(((end - start) * total_tokens, w))
    return out


def cap_weights(stages, sizes, total_tokens, max_epochs, iterations=20):
    """Limit sources whose implied number of epochs exceeds max_epochs.

    stages: [(start_fraction, {source: weight})]; sizes: {source: tokens in one epoch} (only sources whose size is known).
    Returns (new_stages, report {source: (requested_epochs, final_epochs)}). The weight removed from a capped source is
    redistributed to the other sources of the same stage in proportion to their weights.
    """
    stages = [(s, normalise(w)) for s, w in stages]
    spans = [t for t, _ in stage_spans(stages, total_tokens)]
    report = {k: (sum(w.get(k, 0) * t for t, (_, w) in zip(spans, stages)) / sizes[k], None) for k in sizes}
    capped = set()
    for _ in range(iterations):
        changed = False
        for k, size in sizes.items():
            used = sum(w.get(k, 0) * t for t, (_, w) in zip(spans, stages))
            if used > max_epochs * size * 1.0001:
                f = max_epochs * size / used
                stages = [(s, {**w, k: w.get(k, 0) * f}) for s, w in stages]
                capped.add(k)
                changed = True
        new = []
        for s, w in stages:                      # give the freed weight to the uncapped sources
            free = [x for x in w if x not in capped]
            rest = 1.0 - sum(w[x] for x in capped if x in w)
            tot = sum(w[x] for x in free)
            new.append((s, {**{x: w[x] for x in capped if x in w},
                            **{x: (w[x] / tot * rest if tot else 0.0) for x in free}}))
        stages = new
        if not changed:
            break
    for k in sizes:
        used = sum(w.get(k, 0) * t for t, (_, w) in zip(spans, stages))
        report[k] = (report[k][0], used / sizes[k])
    return stages, report


# ----------------------------------------------------------------- the mixer
class MixedChunker:
    """Yields lists of seq_len + 1 token ids; the source of each chunk is drawn from the weights of the current stage."""

    def __init__(self, sources, stages, total_tokens, seq_len, encode, eos_id, shard=0, num_shards=1, seed=0,
                 start_tokens=0, max_doc_tokens=0):
        self.sources, self.stages = sources, [(s, normalise(w)) for s, w in sorted(stages, key=lambda x: x[0])]
        self.total, self.seq_len, self.encode, self.eos = max(1, total_tokens), seq_len, encode, eos_id
        self.shard, self.num_shards, self.seed = shard, num_shards, seed
        self.start_tokens, self.max_doc = start_tokens, max_doc_tokens
        self.chunks = Counter()

    def weights_at(self, tokens_done):
        frac, cur = tokens_done / self.total, self.stages[0][1]
        for start, w in self.stages:
            if frac >= start:
                cur = w
        return cur

    def __iter__(self):
        rng = random.Random(self.seed * 1000003 + self.shard * 7919 + self.start_tokens % 1000003)
        its, bufs = {}, {}
        produced = 0
        while True:
            done = self.start_tokens + produced * self.seq_len * self.num_shards
            w = self.weights_at(done)
            names = [n for n in w if w[n] > 0]
            name = rng.choices(names, [w[n] for n in names])[0]
            if name not in its:
                its[name] = self.sources[name].open(self.shard, self.num_shards, self.seed * 31 + self.shard)
                bufs[name] = []
            buf = bufs[name]
            while len(buf) < self.seq_len + 1:
                ids = self.encode(next(its[name]))
                if self.max_doc and len(ids) > self.max_doc:
                    ids = ids[:self.max_doc]
                buf.extend(ids)
                buf.append(self.eos)
            chunk = buf[:self.seq_len + 1]
            bufs[name] = buf[self.seq_len:]          # overlap of one token: every token is a target exactly once
            self.chunks[name] += 1
            produced += 1
            yield name, chunk
