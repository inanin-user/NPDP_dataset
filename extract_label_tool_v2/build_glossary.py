#!/usr/bin/env python3
"""build_glossary.py - turn the static-analysis CSVs into the business glossary (Markdown) and "term cards" (JSONL).

The CSVs are the single source of truth for STRUCTURE (which page / column / table / button a term points to).
They cannot say what a term MEANS to the business, so definitions come from an optional human-maintained file
(definitions_confirmed.csv: term, definition, aliases, owner) and everything else is listed in definitions_todo.csv.

Inputs (from --in, the output folder of run_static_analysis.bat; missing files are skipped):
  ui_terms.csv  field_labels.csv  display_calls.csv  link_actions.csv  button_handlers.csv
  table_to_file.csv  session_scope.csv  file_titles.csv

Outputs (in --out):
  01_glossary.md         human-readable glossary generated from the CSVs
  term_cards.jsonl       one card per normalised term: variants, targets, evidence, ambiguity, confusable terms
                         (the seed for synthetic email / code-comment / ticket generation and for verification)
  term_index.json        normalised alias -> canonical key (for the email annotator / verifier)
  definitions_todo.csv   terms that still need a business definition, most important first
  glossary_stats.json    counts per kind and ambiguity level

Ambiguity ("entropy") = number of distinct semantic targets one term points to:
  low = 1 (one page / one column of one table / one button ...), medium = 2-3, high = 4+.

Python 3.8+, standard library only.
"""
import argparse
import csv
import datetime
import json
import os
import sys
from collections import Counter, defaultdict

import terms_common as TC

PAGE_TYPES = {"page_title", "heading", "heading_styled", "heading_class"}
LOW_TYPES = {"bold_text"}


# ----------------------------------------------------------------- helpers
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def split(s):
    return [x for x in (s or "").split(";") if x]


def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def ambiguity_label(n):
    return "low" if n <= 1 else ("medium" if n <= 3 else "high")


def short_list(items, n=3):
    items = list(dict.fromkeys(items))
    return ", ".join(items[:n]) + (" (+%d)" % (len(items) - n) if len(items) > n else "")


# ----------------------------------------------------------------- load structure
class Context:
    def __init__(self, inp):
        self.file_tables = defaultdict(set)
        for r in read_csv(os.path.join(inp, "table_to_file.csv")):
            if r["table"] and not r["table"].startswith(("$", "{")):
                self.file_tables[r["file"]].add(r["table"])
        self.scope_tables = defaultdict(set)
        for r in read_csv(os.path.join(inp, "session_scope.csv")):
            self.scope_tables[r["file"]].update(split(r["tables"]))

    def resolve_tables(self, file, tables_raw, bound=""):
        """(tables, source): never claims a table when the column exists in several and the page does not decide."""
        if bound:
            return split(bound), "bound"
        if not tables_raw:
            return [], "unknown"
        if len(tables_raw) == 1:
            return tables_raw, "schema_unique"
        ctx = self.file_tables.get(file, set()) | self.scope_tables.get(file, set())
        hit = [t for t in tables_raw if t in ctx]
        if len(hit) == 1:
            return hit, "file_context"
        if hit:
            return hit, "file_context_ambiguous"
        return [], "schema_ambiguous"


def collect_entries(inp, aliases, ctx):
    """key -> {'variants': Counter, 'entries': [..]} from every CSV."""
    cards = defaultdict(lambda: {"variants": Counter(), "entries": []})

    def add(term, kind, useful_only=True, **kw):
        term = " ".join((term or "").split())
        if not term:
            return
        toks = TC.norm_tokens(term, aliases)
        key = " ".join(toks)
        if not key:
            return
        generic = not TC.is_useful_key(toks)
        c = cards[key]
        c["variants"][term] += 1
        kw.update({"kind": kind, "generic": generic})
        c["entries"].append(kw)

    has_buttons = os.path.exists(os.path.join(inp, "button_handlers.csv"))
    for r in read_csv(os.path.join(inp, "ui_terms.csv")):
        t = r["term_type"]
        base = {"source": "ui_terms.csv", "file": r["file"], "line": r["line"], "confidence": r["confidence"]}
        if t in PAGE_TYPES or t in LOW_TYPES:
            add(r["term"], "page", ui_type=t, dynamic_php=r.get("dynamic_php", ""), **base)
        elif t == "link_text":
            add(r["term"], "menu", target=r["href_target"], **base)
        elif t == "alert_message":
            add(r["term"], "message", **base)
        elif t == "button_label" and not has_buttons:
            add(r["term"], "button", name=r.get("href_target", ""), **base)

    for r in read_csv(os.path.join(inp, "field_labels.csv")):
        tables, tsrc = ctx.resolve_tables(r["file"], split(r["tables"]))
        add(r["label"], "field", source="field_labels.csv", file=r["file"], line=r["line"], confidence="high",
            column=r["column"], tables=tables, table_source=tsrc, pair_kind=r["kind"],
            link_target=r.get("link_target", ""), link_mode=r.get("link_mode", ""), flag="", readonly="")

    for r in read_csv(os.path.join(inp, "display_calls.csv")):
        if not r["ui_term"] or not r["column"]:
            continue
        tables, tsrc = ctx.resolve_tables(r["file"], split(r["tables"]), r.get("table", ""))
        add(r["ui_term"], "field", source="display_calls.csv", file=r["file"], line=r["line"], confidence="high",
            column=r["column"], tables=tables, table_source=tsrc, pair_kind=r["helper"], link_target="",
            link_mode="", flag=r["flag_session"], readonly=r["readonly"])

    for r in read_csv(os.path.join(inp, "link_actions.csv")):
        label = r["header_label"] or r["row_label"] or r["link_text"]
        if label and r["target_file"]:
            add(label, "link", source="link_actions.csv", file=r["source_file"], line=r["line"], confidence="high",
                target=r["target_file"], mode=r["action_mode"], handler_line=r["target_mode_line"],
                handler_tables=r["target_mode_tables"], columns=r["link_columns"])

    for r in read_csv(os.path.join(inp, "button_handlers.csv")):
        if r["button_value"]:
            add(r["button_value"], "button", source="button_handlers.csv", file=r["source_file"], line="",
                confidence="high" if r["status"] == "found" else "medium", name=r["button_name"],
                handler_file=r["handler_file"], handler_line=r["handler_line"], tables=r["tables"],
                columns_written=r["columns_written"], status=r["status"], targets=r["targets"])
    return cards


# ----------------------------------------------------------------- cards
def build_cards(raw, definitions, aliases):
    cards = {}
    for key, c in raw.items():
        e = c["entries"]
        kinds = sorted({x["kind"] for x in e})
        pages = [x["file"] for x in e if x["kind"] == "page"]
        menu_targets = [x["target"] for x in e if x["kind"] == "menu" and x["target"]]
        link_targets = [x["target"] for x in e if x["kind"] == "link"]
        fields = {}
        for x in e:
            if x["kind"] != "field":
                continue
            k = (x["column"], tuple(x["tables"]))
            f = fields.setdefault(k, {"column": x["column"], "tables": list(x["tables"]),
                                      "table_source": x["table_source"], "files": [], "pair_kinds": set(),
                                      "link_targets": set(), "flags": set(), "readonly": False})
            f["files"].append("%s:%s" % (x["file"], x["line"]))
            f["pair_kinds"].add(x["pair_kind"])
            if x["link_target"]:
                f["link_targets"].add("%s%s" % (x["link_target"], " (action=%s)" % x["link_mode"] if x["link_mode"] else ""))
            if x["flag"]:
                f["flags"].add(x["flag"])
            f["readonly"] = f["readonly"] or x["readonly"] == "yes"
        buttons = {}
        for x in e:
            if x["kind"] != "button":
                continue
            b = buttons.setdefault(x["name"], {"name": x["name"], "files": [], "handlers": [], "tables": set(),
                                               "columns_written": set(), "next": set(), "status": set()})
            b["files"].append(x["file"])
            if x.get("handler_file"):
                b["handlers"].append("%s:%s" % (x["handler_file"], x["handler_line"]))
            b["tables"].update(split(x.get("tables", "")))
            b["columns_written"].update(split(x.get("columns_written", "")))
            b["next"].update(split(x.get("targets", "")))
            b["status"].add(x.get("status", ""))

        objects = {("page", f) for f in pages} | {("page", t) for t in menu_targets} | \
                  {("field", k[0], k[1]) for k in fields} | \
                  {("button", n) for n in buttons} | ({("message", key)} if "message" in kinds else set())
        if not objects:                       # only a link label: the page it opens is then its meaning
            objects = {("page", t) for t in link_targets}
        # menu entry + page title of the same page = ONE object; "the page this value links to" is navigation,
        # not a competing meaning, so it does not raise the ambiguity
        n_obj = len(objects)
        non_low = [x for x in e if x.get("confidence") != "low"]
        generic = all(x["generic"] for x in e)
        card = {
            "key": key, "term": c["variants"].most_common(1)[0][0], "variants": [v for v, _ in c["variants"].most_common()],
            "kinds": kinds, "generic": generic, "ambiguity": ambiguity_label(n_obj), "ambiguity_n": n_obj,
            "confidence": "UNKNOWN" if not e else ("CODE" if non_low else "INFERRED"),
            "targets": {
                "pages": sorted(set(pages)), "menu_links_to": sorted(set(menu_targets)),
                "fields": [{**f, "files": list(dict.fromkeys(f["files"])), "pair_kinds": sorted(f["pair_kinds"]),
                            "link_targets": sorted(f["link_targets"]), "flags": sorted(f["flags"])}
                           for f in sorted(fields.values(), key=lambda z: z["column"])],
                "buttons": [{**b, "files": sorted(set(b["files"])), "handlers": list(dict.fromkeys(b["handlers"])),
                             "tables": sorted(b["tables"]), "columns_written": sorted(b["columns_written"]),
                             "next": sorted(b["next"]), "status": sorted(b["status"])} for b in buttons.values()],
                "links": sorted({"%s -> %s%s" % (x["file"], x["target"], " (action=%s)" % x["mode"] if x["mode"] else "")
                                 for x in e if x["kind"] == "link"}),
                "messages_in": sorted({x["file"] for x in e if x["kind"] == "message"}),
            },
            "evidence": [{k: v for k, v in x.items() if k in ("source", "file", "line", "ui_type", "pair_kind", "confidence")}
                         for x in e][:12],
            "evidence_count": len(e), "definition": None, "definition_source": None, "confusable_with": [],
            "_objects": objects,
        }
        cards[key] = card

    # confirmed definitions / extra aliases
    index = {}
    for key, card in cards.items():
        index[key] = key
    for d in definitions:
        dkey = TC.norm_key(d.get("term", ""), aliases)
        target = index.get(dkey)
        if not target:
            continue
        cards[target]["definition"] = (d.get("definition") or "").strip() or None
        cards[target]["definition_source"] = "confirmed" if cards[target]["definition"] else None
        if cards[target]["definition"]:
            cards[target]["confidence"] = "CONFIRMED"
        for a in split(d.get("aliases", "")):
            akey = TC.norm_key(a, aliases)
            if akey and akey not in index:
                index[akey] = target
                cards[target]["variants"].append(a)

    # confusable terms: share a meaningful token but point to different objects (hard negatives for synthesis)
    by_tok = defaultdict(set)
    for key in cards:
        for t in set(key.split()):
            if len(t) >= 3 and t not in TC.GENERIC_SINGLE:
                by_tok[t].add(key)
    for key, card in cards.items():
        if card["generic"]:
            continue
        toks = set(key.split())
        score = Counter()
        for t in toks:
            if len(by_tok.get(t, ())) > 200:
                continue
            for other in by_tok.get(t, ()):
                if other != key and cards[other]["_objects"] != card["_objects"]:
                    score[other] += 1
        ranked = sorted(score, key=lambda o: (-score[o] / len(toks | set(o.split())), o))
        card["confusable_with"] = ranked[:5]
    return cards, index


# ----------------------------------------------------------------- markdown
def md_table(header, rows):
    if not rows:
        return ["_(none found)_"]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return out


def render_markdown(cards, inp, max_rows, include_generic):
    def visible(c, kind):
        if include_generic or not c["generic"] or kind == "field":
            return True                       # 'Status' -> two different columns is exactly what must be listed
        return kind == "button" and any(b["handlers"] for b in c["targets"]["buttons"])

    def pick(kind):
        sel = [c for c in cards.values() if kind in c["kinds"] and visible(c, kind)]
        return sorted(sel, key=lambda c: (-c["evidence_count"], c["key"]))[:max_rows]

    lines = ["# Business Glossary (generated from static analysis)", "",
             "_Generated %s from the CSVs in `%s`. Structure comes from code; a definition appears only when a "
             "business owner confirmed it (`definitions_confirmed.csv`)._" % (datetime.date.today().isoformat(), inp), "",
             "Ambiguity: **low** = the term points to one page / column / button, **medium** = 2-3, **high** = 4+. "
             "Confidence: CODE = found in source, INFERRED = only weak evidence, CONFIRMED = definition confirmed by an owner.", ""]
    de = lambda c: c["definition"] or "—"
    aliases_of = lambda c: ", ".join(v for v in c["variants"] if v != c["term"]) or "—"

    lines += ["## A. Pages / functions (page titles, headings, menu entries)", ""]
    rows = []
    for c in pick("page") + [x for x in pick("menu") if "page" not in x["kinds"]]:
        t = c["targets"]
        rows.append([c["term"], aliases_of(c), short_list(t["pages"]) or "—", short_list(t["menu_links_to"]) or "—",
                     c["ambiguity"], c["confidence"], de(c)])
    lines += md_table(["Term", "Aliases", "Page title in", "Menu link to", "Ambiguity", "Conf.", "Definition"], rows) + [""]

    lines += ["## B. Field labels (what the user sees next to a value)", ""]
    rows = []
    for c in pick("field"):
        for f in c["targets"]["fields"]:
            tbl = ", ".join(f["tables"]) if f["tables"] else "? (%s)" % f["table_source"]
            extra = []
            if f["flags"]:
                extra.append("permission flag " + ", ".join(f["flags"]))
            if f["readonly"]:
                extra.append("read-only")
            rows.append([c["term"], aliases_of(c), "%s -> %s" % (f["column"], tbl), short_list(f["files"], 2),
                         short_list(f["link_targets"], 2) or "—", "; ".join(extra) or "—", c["ambiguity"], de(c)])
    lines += md_table(["Term", "Aliases", "Column -> table", "Seen in", "Value links to", "Notes", "Ambiguity", "Definition"], rows) + [""]

    lines += ["## C. Buttons", ""]
    rows = []
    for c in pick("button"):
        for b in c["targets"]["buttons"]:
            rows.append([c["term"], b["name"] or "—", short_list(b["files"], 2), short_list(b["handlers"], 2) or "not found",
                         ", ".join(b["tables"]) or "—", short_list(b["columns_written"], 6) or "—", c["ambiguity"]])
    lines += md_table(["Button label", "Name", "On page", "Handled at", "Tables", "Columns written", "Ambiguity"], rows) + [""]

    lines += ["## D. Links that open another page", ""]
    rows = []
    for c in pick("link"):
        rows.append([c["term"], "; ".join(c["targets"]["links"][:3]), c["ambiguity"]])
    lines += md_table(["Label / column header", "From -> to", "Ambiguity"], rows) + [""]

    lines += ["## E. System messages users quote", ""]
    rows = [[c["term"], short_list(c["targets"]["messages_in"])] for c in pick("message")]
    lines += md_table(["Message", "Shown in"], rows) + [""]

    tf = read_csv(os.path.join(inp, "table_to_file.csv"))
    if tf:
        by = defaultdict(lambda: {"read": set(), "write": set(), "cols": []})
        for r in tf:
            if r["table"].startswith(("$", "{")):
                continue
            b = by[r["table"]]
            (b["read"] if r["operation"] == "SELECT" else b["write"]).add("%s(%s)" % (r["file"], r["operation"]) if r["operation"] != "SELECT" else r["file"])
            b["cols"] += split(r["columns_written"])
        lines += ["## F. Tables and the pages that use them", ""]
        rows = [[t, short_list(sorted(b["read"]), 4) or "—", short_list(sorted(b["write"]), 4) or "—",
                 short_list([c for c in b["cols"] if not c.startswith("<")], 8) or "—"] for t, b in sorted(by.items())]
        lines += md_table(["Table", "Read by", "Written by", "Columns written"], rows[:max_rows]) + [""]
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="folder with the extracted CSVs")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--definitions", help="definitions_confirmed.csv (term, definition, aliases, owner)")
    ap.add_argument("--aliases", help="JSON term aliases (same file as for the other scripts)")
    ap.add_argument("--max-rows", type=int, default=2000, help="rows per Markdown section (cards are never truncated)")
    ap.add_argument("--include-generic", action="store_true", help="list generic one-word terms (Save, Date ...) in the Markdown")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    aliases = TC.load_aliases(args.aliases)
    ctx = Context(args.inp)
    raw = collect_entries(args.inp, aliases, ctx)
    if not raw:
        print("No terms found in", args.inp)
        return 1
    definitions = read_csv(args.definitions) if args.definitions else []
    cards, index = build_cards(raw, definitions, aliases)

    with open(os.path.join(args.out, "01_glossary.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(cards, args.inp, args.max_rows, args.include_generic))
    with open(os.path.join(args.out, "term_cards.jsonl"), "w", encoding="utf-8") as f:
        for c in sorted(cards.values(), key=lambda c: c["key"]):
            f.write(json.dumps({k: v for k, v in c.items() if not k.startswith("_")}, ensure_ascii=False, default=list) + "\n")
    with open(os.path.join(args.out, "term_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=0, sort_keys=True)

    todo = [c for c in cards.values() if not c["definition"] and (not c["generic"] or "field" in c["kinds"])]
    todo.sort(key=lambda c: ({"high": 0, "medium": 1, "low": 2}[c["ambiguity"]], -c["evidence_count"], c["key"]))
    with open(os.path.join(args.out, "definitions_todo.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["term", "kinds", "ambiguity", "evidence_count", "points_to", "definition", "aliases", "owner"])
        for c in todo:
            pts = [("%s -> %s" % (x["column"], ",".join(x["tables"]) or "?")) for x in c["targets"]["fields"]] + \
                  c["targets"]["pages"] + c["targets"]["menu_links_to"]
            w.writerow([c["term"], ";".join(c["kinds"]), c["ambiguity"], c["evidence_count"], " | ".join(pts[:3]), "", "", ""])

    stats = {"cards": len(cards), "generic": sum(1 for c in cards.values() if c["generic"]),
             "by_kind": dict(Counter(k for c in cards.values() for k in c["kinds"])),
             "by_ambiguity": dict(Counter(c["ambiguity"] for c in cards.values() if not c["generic"])),
             "with_definition": sum(1 for c in cards.values() if c["definition"]), "definitions_todo": len(todo)}
    with open(os.path.join(args.out, "glossary_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())