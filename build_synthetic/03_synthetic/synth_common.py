#!/usr/bin/env python3
"""synth_common.py - shared by build_synthetic.py and validate_synthetic.py.

Design rule: LABELS ARE NEVER WRITTEN BY THE LLM. They are derived from the knowledge files
(glossary.json + relationships.json) by derive_targets() / build_labels(). The LLM only writes the natural-language
surface (email / ticket / code comment); validate_synthetic.py re-derives the labels independently and rejects any
record whose text or labels do not match the knowledge.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, ".."), os.path.join(HERE, "..", "01_glossary"),
           os.path.join(HERE, "..", "..", "static_analysis"), os.path.join(HERE, "..", "..", "01_glossary")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import terms_common as TC  # noqa: E402


# ----------------------------------------------------------------- io
def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path, rows, append=False):
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class Knowledge:
    def __init__(self, kdir, aliases_path=None):
        g = json.load(open(os.path.join(kdir, "glossary.json"), encoding="utf-8"))
        self.cards = g["terms"]
        self.index = g.get("index", {})
        self.rel = json.load(open(os.path.join(kdir, "relationships.json"), encoding="utf-8"))
        ap = aliases_path or (os.path.join(kdir, "aliases.json") if os.path.exists(os.path.join(kdir, "aliases.json")) else None)
        self.aliases = TC.load_aliases(ap)
        self.detector = TermDetector(self.cards, self.index, self.aliases)

    def title(self, file):
        return self.rel["pages"].get(file, {}).get("title", "")

    def tech_regex(self):
        """Every technical name (columns, tables, php files) - must not leak into business-language text."""
        names = set()
        for f in self.rel["fields"]:
            names.add(f["column"])
        names.update(self.rel["tables"])
        for p in self.rel["pages"]:
            names.add(p)
            names.add(os.path.basename(p))
        names = sorted((n for n in names if len(n) >= 4), key=len, reverse=True)
        if not names:
            return None
        return re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_])", re.I)


# ----------------------------------------------------------------- term detection
class TermDetector:
    """Longest-match n-gram detector over the normalised glossary keys (generic one-word keys are not detected)."""

    def __init__(self, cards, index, aliases, max_len=12):
        self.aliases, self.max_len = aliases, max_len
        self.table = {}
        for key, card in cards.items():
            if card.get("generic"):
                continue
            toks = tuple(key.split())
            if 0 < len(toks) <= max_len:
                self.table[toks] = key
        for alias_key, key in index.items():
            toks = tuple(alias_key.split())
            if key in cards and not cards[key].get("generic") and 0 < len(toks) <= max_len:
                self.table.setdefault(toks, key)

    def detect(self, text):
        toks = TC.tokenize(TC.clean_text(text), self.aliases)
        words = [t for t, _, _ in toks]
        out, i = [], 0
        while i < len(words):
            hit = None
            for n in range(min(self.max_len, len(words) - i), 0, -1):
                k = self.table.get(tuple(words[i:i + n]))
                if k:
                    hit = (k, n)
                    break
            if hit:
                out.append({"key": hit[0], "tokens": hit[1]})
                i += hit[1]
            else:
                i += 1
        return out


def phrase_in_text(phrase, text, aliases):
    p, t = TC.norm_tokens(phrase, aliases), TC.norm_tokens(text, aliases)
    n = len(p)
    return bool(p) and any(t[i:i + n] == p for i in range(len(t) - n + 1))


def remove_phrase(tokens, phrase_tokens):
    n = len(phrase_tokens)
    for i in range(len(tokens) - n + 1):
        if tokens[i:i + n] == phrase_tokens:
            return tokens[:i] + tokens[i + n:]
    return tokens


# ----------------------------------------------------------------- targets and labels (deterministic)
def derive_targets(card, kn):
    """Concrete, single-meaning targets of a term card. A field target exists only when its table is certain."""
    t, out = card["targets"], []
    for f in t["fields"]:
        if len(f["tables"]) != 1:
            continue
        files = [x.rsplit(":", 1)[0] for x in f["files"]]
        pf = files[0] if files else ""
        out.append({"kind": "field", "id": "field|%s|%s" % (f["column"], f["tables"][0]), "column": f["column"],
                    "table": f["tables"][0], "file": pf, "files": files, "page_title": kn.title(pf),
                    "readonly": bool(f["readonly"]), "opens": f["link_targets"]})
    for b in t["buttons"]:
        if b["handlers"] and b["files"]:
            pf = b["files"][0]
            out.append({"kind": "button", "id": "button|%s|%s" % (b["name"], pf), "button": b["name"], "file": pf,
                        "handler": b["handlers"][0], "tables": b["tables"], "columns_written": b["columns_written"],
                        "page_title": kn.title(pf)})
    pages = list(t["pages"]) + [p for p in t["menu_links_to"] if p not in t["pages"]]
    for p in pages:
        out.append({"kind": "page", "id": "page|" + p, "file": p, "page_title": kn.title(p),
                    "tables": sorted(kn.rel["pages"].get(p, {}).get("tables", {}))})
    if t["messages_in"]:
        out.append({"kind": "message", "id": "message|" + card["key"], "file": t["messages_in"][0],
                    "files": t["messages_in"], "page_title": kn.title(t["messages_in"][0])})
    return out


def derive_ambiguous(card, kn):
    """A term that means different things on different screens: the candidate targets, or None.
    Used for 'insufficient context' examples where the right behaviour is to ask which screen is meant."""
    seen = {}
    for t in derive_targets(card, kn):
        if t["kind"] in ("field", "page", "button") and t["page_title"]:
            seen.setdefault(t["id"], t)
    cands = sorted(seen.values(), key=lambda x: x["id"])
    if len(cands) < 2 or len({c["page_title"] for c in cands}) < 2:
        return None
    return {"kind": "ambiguous", "id": "ambiguous|" + card["key"], "candidates": cands, "issue_kind": cands[0]["kind"],
            "file": cands[0]["file"], "page_title": ""}


def build_ambiguous_labels(card, amb, surface, kn):
    cands = amb["candidates"]
    desc = []
    fields, tables, files = [], [], []
    for c in cands:
        if c["kind"] == "field":
            fields.append({"label": card["term"], "column": c["column"], "table": c["table"]})
            tables.append(c["table"])
            desc.append("%s(%s) on '%s'" % (c["column"], c["table"], c["page_title"]))
        else:
            tables += list(c.get("tables", []))
            desc.append("%s '%s'" % (c["kind"], c["page_title"]))
        files.append(c["file"])
    mapping = {"surface": surface, "ui_term": card["term"], "kind": "ambiguous", "target": "candidates: " + "; ".join(desc),
               "source": source_of(card, cands[0]["file"]), "ambiguity": card["ambiguity"]}
    return {"term_mapping": [mapping], "related_fields": fields, "related_tables": list(dict.fromkeys(tables)),
            "related_pages": [{"title": c["page_title"], "file": c["file"]} for c in cands],
            "related_files": list(dict.fromkeys(files)), "decision": "ask_clarification",
            "clarifying_question": "Which screen do you mean: %s?" % " or ".join("'%s'" % c["page_title"] for c in cands),
            "candidates": [{"kind": c["kind"], "column": c.get("column", ""), "table": c.get("table", ""),
                            "page": c["file"], "page_title": c["page_title"]} for c in cands]}


def source_of(card, file):
    for e in card.get("evidence", []):
        if e.get("file") == file and e.get("source"):
            return e["source"]
    return (card.get("evidence") or [{}])[0].get("source", "")


def build_labels(card, target, surface, kn):
    """The label block a document carries. Pure function of (card, target, knowledge)."""
    k = target["kind"]
    src = source_of(card, target.get("file", ""))
    ui = card["term"]
    mapping = {"surface": surface, "ui_term": ui, "kind": k, "source": src, "ambiguity": card["ambiguity"]}
    fields, tables, files = [], [], []
    if k == "field":
        mapping["target"] = target["column"]
        fields.append({"label": ui, "column": target["column"], "table": target["table"]})
        tables = [target["table"]]
        files = [target["file"]] + [x for x in target["files"][1:]] + [o.split(" ")[0] for o in target["opens"]]
    elif k == "button":
        mapping["target"] = target["button"]
        tables = list(target["tables"])
        files = [target["file"], target["handler"].rsplit(":", 1)[0]]
        fields = [{"label": ui, "column": c.split(".")[-1], "table": c.split(".")[0] if "." in c else ""}
                  for c in target["columns_written"]]
    elif k == "page":
        mapping["target"] = target["file"]
        tables = list(target["tables"])
        files = [target["file"]]
    else:
        mapping["target"] = target["file"]
        files = list(target.get("files") or [target["file"]])
    files = list(dict.fromkeys(f for f in files if f))
    pages = [{"title": kn.title(f), "file": f} for f in files if kn.title(f)]
    return {"term_mapping": [mapping], "related_fields": fields, "related_tables": list(dict.fromkeys(tables)),
            "related_pages": pages, "related_files": files}


def render_labels(labels):
    m = labels["term_mapping"][0]
    lines = ['Term mapping: "%s" -> UI term "%s" -> %s %s (from %s)' % (m["surface"], m["ui_term"], m["kind"], m.get("target", ""), m["source"])]
    if labels["related_fields"]:
        lines.append("Related fields: " + "; ".join("%s -> %s%s" % (f["label"], f["column"], " (%s)" % f["table"] if f["table"] else "")
                                                     for f in labels["related_fields"][:8]))
    if labels["related_tables"]:
        lines.append("Related tables: " + ", ".join(labels["related_tables"]))
    if labels["related_pages"]:
        lines.append("Related php files: " + ", ".join("%s (title: %s)" % (p["file"], p["title"]) for p in labels["related_pages"]))
    elif labels["related_files"]:
        lines.append("Related php files: " + ", ".join(labels["related_files"]))
    if labels.get("decision"):
        lines.append("Decision: %s. %s" % (labels["decision"].replace("_", " "), labels.get("clarifying_question", "")))
    lines.append("Entropy: " + m["ambiguity"])
    return "\n".join(lines)


def render_document(rec):
    """Pre-training text of a validated record."""
    if rec["task"] == "contrastive_term_linking":
        ex = rec["examples"]
        lines = []
        for e in ex:
            out = "; ".join("%s=%s" % (k, v) for k, v in e["output"].items() if v)
            lines += ["Question: " + e["input"], "Answer: " + out]
        lines.append("Difference: %s vs %s -> different fields." % tuple(rec["distinguishing_terms"]))
        return "\n".join(lines)
    text = rec["generated"].strip()
    if rec["surface"] == "code_comment":
        text = "```php\n" + text + "\n```"
    return text + "\n\n" + render_labels(rec["labels"])


def to_structured(rec, kn):
    """Record in the schema task / user_input / resolved_entities / relationships / evidence / ambiguity / expected_decision.
    Everything except user_input is derived from the knowledge (never from the LLM)."""
    if rec["task"] == "contrastive_term_linking":
        return rec
    card = kn.cards[rec["term_key"]]
    lab, t = rec["labels"], rec["target"]
    m = lab["term_mapping"][0]
    files = set(lab.get("related_files", []))
    evidence = [{"source": e.get("source", ""), "file": e.get("file", ""), "line": e.get("line", "")}
                for e in card.get("evidence", []) if e.get("file") in files][:6] or \
               [{"source": m["source"], "file": f} for f in list(files)[:3]]
    out = {"id": rec["id"], "task": "term_linking" if rec["task"] == "glossary_conditioned_generation" else "ambiguous_term_resolution",
           "surface": rec["surface"], "user_input": rec["generated"].strip(), "evidence": evidence}
    if rec["task"] == "ambiguous_term_resolution":
        rels = []
        for c in lab["candidates"]:
            if c["column"]:
                rels.append({"subject": card["term"], "relation": "label_of_column", "object": "%s.%s" % (c["table"], c["column"]),
                             "page": c["page"], "source": m["source"]})
        out.update({"resolved_entities": [], "relationships": rels,
                    "ambiguity": {"level": card["ambiguity"], "candidates": lab["candidates"]},
                    "expected_decision": "ask_clarification", "clarifying_question": lab["clarifying_question"]})
        return out
    ents, rels = [], []
    if t["kind"] == "field":
        ents.append({"entity_type": "field", "business_term": m["surface"], "ui_term": card["term"], "column": t["column"],
                     "table": t["table"], "page": t["file"], "page_title": t["page_title"]})
        rels += [{"subject": card["term"], "relation": "label_of_column", "object": "%s.%s" % (t["table"], t["column"]), "source": m["source"]},
                 {"subject": t["file"], "relation": "contains_field", "object": card["term"], "source": m["source"]}]
    elif t["kind"] == "button":
        ents.append({"entity_type": "button", "business_term": m["surface"], "name": t["button"], "page": t["file"], "handler": t["handler"]})
        rels.append({"subject": t["button"], "relation": "handled_by", "object": t["handler"], "source": m["source"]})
        rels += [{"subject": t["handler"].rsplit(":", 1)[0], "relation": "touches_table", "object": x, "source": "button_handlers.csv"} for x in t["tables"]]
    elif t["kind"] == "page":
        ents.append({"entity_type": "page", "business_term": m["surface"], "file": t["file"], "title": t["page_title"]})
        rels += [{"subject": t["file"], "relation": "touches_table", "object": x, "source": "table_to_file.csv"} for x in t["tables"]]
    else:
        ents.append({"entity_type": "message", "business_term": m["surface"], "file": t["file"]})
    out.update({"resolved_entities": ents, "relationships": rels,
                "ambiguity": {"level": card["ambiguity"], "disambiguated_by": rec.get("context_phrase", "")},
                "expected_decision": "resolve"})
    return out
