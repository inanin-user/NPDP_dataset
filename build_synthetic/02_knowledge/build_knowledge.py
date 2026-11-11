#!/usr/bin/env python3
"""build_knowledge.py - the CSVs + term cards  ->  02_knowledge/{glossary.json, glossary.md, relationships.json}

glossary.json       {"meta", "terms": {key: term card}, "index": {alias key: key}}   (from build_glossary.py)
glossary.md         copy of 01_glossary.md
relationships.json  {"meta", "pages", "tables", "fields", "edges"}  - the facts a synthetic example may rely on:
    pages[file]   title, tables touched (+ops), fields (label -> column -> table), links_to, buttons (+handler),
                  includes, flows_to, session_tables
    tables[name]  pages that use it (+ops), columns written
    fields[]      label, column, table (only when ONE table is certain), file, line, source csv
    edges[]       page_links_to_page / page_touches_table / button_handled_by / ...  (each with its source csv)

Usage:
  python build_knowledge.py --csv-dir <extraction output> --glossary-dir <build_glossary output> --out ../02_knowledge
Python 3.8+, standard library only. Needs build_glossary.py / terms_common.py importable (same folder or ../../static_analysis).
"""
import argparse
import datetime
import json
import os
import shutil
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.join(HERE, ".."), os.path.join(HERE, "..", "01_glossary"),
           os.path.join(HERE, "..", "..", "static_analysis"), os.path.join(HERE, "..", "..", "01_glossary")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import build_glossary as BG  # noqa: E402

read_csv, split = BG.read_csv, BG.split


def build_relationships(inp, ctx):
    pages = defaultdict(lambda: {"title": "", "title_type": "", "tables": {}, "fields": [], "links_to": [], "buttons": [],
                                 "includes": [], "flows_to": [], "session_tables": []})
    tables = defaultdict(lambda: {"pages": {}, "columns_written": set()})
    fields, seen_fields, edges, seen_edges = [], set(), [], set()

    def edge(kind, src, dst, source, **kw):
        k = (kind, src, dst, tuple(sorted(kw.items())))
        if k not in seen_edges:
            seen_edges.add(k)
            edges.append({"type": kind, "from": src, "to": dst, "source": source, **kw})

    for r in read_csv(os.path.join(inp, "file_titles.csv")):
        pages[r["file"]]["title"] = r["title"]
        pages[r["file"]]["title_type"] = r["term_type"]

    for r in read_csv(os.path.join(inp, "table_to_file.csv")):
        t = r["table"]
        if not t or t.startswith(("$", "{")):
            continue
        pages[r["file"]]["tables"].setdefault(t, set()).add(r["operation"])
        tables[t]["pages"].setdefault(r["file"], set()).add(r["operation"])
        tables[t]["columns_written"].update(c for c in split(r["columns_written"]) if not c.startswith("<"))
        edge("page_touches_table", r["file"], t, "table_to_file.csv", operation=r["operation"])

    def add_field(label, column, file, line, tables_raw, bound, source):
        if not label or not column:
            return
        tbls, tsrc = ctx.resolve_tables(file, tables_raw, bound)
        k = (label.lower(), column, tuple(tbls), file)
        if k in seen_fields:
            return
        seen_fields.add(k)
        f = {"label": label, "column": column, "table": tbls[0] if len(tbls) == 1 else "", "tables": tbls,
             "table_source": tsrc, "file": file, "line": line, "source": source}
        fields.append(f)
        pages[file]["fields"].append({k2: f[k2] for k2 in ("label", "column", "table", "table_source")})
        if len(tbls) == 1:
            edge("field_stored_in_table", "%s.%s" % (tbls[0], column), tbls[0], source, label=label, page=file)

    for r in read_csv(os.path.join(inp, "field_labels.csv")):
        add_field(r["label"], r["column"], r["file"], r["line"], split(r["tables"]), "", "field_labels.csv")
    for r in read_csv(os.path.join(inp, "display_calls.csv")):
        add_field(r["ui_term"], r["column"], r["file"], r["line"], split(r["tables"]), r.get("table", ""), "display_calls.csv")

    for r in read_csv(os.path.join(inp, "link_actions.csv")):
        item = {"to": r["target_file"], "via": r["header_label"] or r["row_label"] or r["link_text"],
                "mode": r["action_mode"], "columns": split(r["link_columns"]), "handler_tables": r["target_mode_tables"]}
        pages[r["source_file"]]["links_to"].append(item)
        edge("page_links_to_page", r["source_file"], r["target_file"], "link_actions.csv", via=item["via"], mode=r["action_mode"])

    for r in read_csv(os.path.join(inp, "button_handlers.csv")):
        item = {"name": r["button_name"], "label": r["button_value"], "handler": "%s:%s" % (r["handler_file"], r["handler_line"])
                if r["handler_file"] else "", "tables": split(r["tables"]), "columns_written": split(r["columns_written"]),
                "next": split(r["targets"]), "status": r["status"]}
        pages[r["source_file"]]["buttons"].append(item)
        if r["handler_file"]:
            edge("button_handled_by", "%s#%s" % (r["source_file"], r["button_name"]), r["handler_file"], "button_handlers.csv")

    for r in read_csv(os.path.join(inp, "file_to_file.csv")):
        if r["resolved"] not in ("yes", "basename") or not r["target_resolved"]:
            continue
        if r["edge_type"].startswith("include"):
            pages[r["source"]]["includes"].append(r["target_resolved"])
            edge("page_includes_page", r["source"], r["target_resolved"], "file_to_file.csv")
        elif r["edge_type"] in ("form_action", "redirect", "ajax", "window_open"):
            pages[r["source"]]["flows_to"].append({"to": r["target_resolved"], "how": r["edge_type"]})
            edge("page_%s_page" % r["edge_type"], r["source"], r["target_resolved"], "file_to_file.csv")

    for r in read_csv(os.path.join(inp, "session_scope.csv")):
        pages[r["file"]]["session_tables"] = split(r["tables"])
        for t in split(r["tables"]):
            edge("session_bound_to_table", r["file"], t, "session_scope.csv", scope=r["source"])

    def jsonable(o):
        if isinstance(o, dict):
            return {k: jsonable(v) for k, v in o.items()}
        if isinstance(o, (set, tuple)):
            return sorted(jsonable(x) for x in o)
        if isinstance(o, list):
            return [jsonable(x) for x in o]
        return o

    return jsonable({"pages": dict(pages), "tables": dict(tables), "fields": fields, "edges": edges})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv-dir", required=True, help="folder with the extraction CSVs")
    ap.add_argument("--glossary-dir", required=True, help="folder with term_cards.jsonl / term_index.json / 01_glossary.md")
    ap.add_argument("--out", required=True, help="02_knowledge output folder")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    cards = {}
    with open(os.path.join(args.glossary_dir, "term_cards.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                c = json.loads(line)
                cards[c["key"]] = c
    index_path = os.path.join(args.glossary_dir, "term_index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {k: k for k in cards}
    meta = {"generated": datetime.date.today().isoformat(), "csv_dir": args.csv_dir, "terms": len(cards)}
    with open(os.path.join(args.out, "glossary.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "terms": cards, "index": index}, f, ensure_ascii=False, indent=1)
    md = os.path.join(args.glossary_dir, "01_glossary.md")
    if os.path.exists(md):
        shutil.copyfile(md, os.path.join(args.out, "glossary.md"))

    rel = build_relationships(args.csv_dir, BG.Context(args.csv_dir))
    rel["meta"] = {"generated": meta["generated"], "csv_dir": args.csv_dir, "pages": len(rel["pages"]),
                   "tables": len(rel["tables"]), "fields": len(rel["fields"]), "edges": len(rel["edges"])}
    with open(os.path.join(args.out, "relationships.json"), "w", encoding="utf-8") as f:
        json.dump(rel, f, ensure_ascii=False, indent=1)
    print(json.dumps(rel["meta"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
