#!/usr/bin/env python3
"""extract_ui_terms.py - record the words a USER sees on screen and the PHP file/field behind them.

Emails say "Production Worksheet Maintenance", "Style No", "This W/S# will approve and send ...".
This script extracts those visible terms from the PHP source and links each to a file / column:

  page_title / heading / heading_styled / heading_class / bold_text
        <title>, <h1-h6>, <legend>, and styled headings such as
        <span style="font-size: 15px; font-weight: bold;">SWS Send And Receipt Form - <?= $x ?></span>
        <div style="font-size: 15px; font-weight: bold;">Production Worksheet Maintenance</div>
        (text after the first PHP block, e.g. "- <?= ... ?>", is kept as `dynamic_php`)
  link_text      <a href="prod_main.php">Production Worksheet</a>  -> menu entry -> target file
  button_label   from form_actions.csv (optional)
  alert_message  alert("This W/S# will approve and send at next day 01:00 AM")
  field_label    "Style No:" next to <input name="wsm_styleno"> / <?= $row['wsm_styleno'] ?>
                 (same row cell pairing, <label for>, and header-row <-> data-row pairing)

Outputs (in --out):
  ui_terms.csv      one row per visible term occurrence
  field_labels.csv  label <-> column pairs
  file_titles.csv   best page title per PHP file
  term_bridge.jsonl one record per normalised term with all its targets (input of link_email_terms.py)

Python 3.8+, standard library only. Heuristic (regex) based.
"""
import argparse
import csv
import html
import json
import os
import re
import sys
from collections import defaultdict

import extract_labels as EL
import extract_form_actions as FA
import terms_common as TC

OPEN_RE = re.compile(r"<(title|h[1-6]|legend|caption|span|div|font|b|strong|p|td|th|u|big|a|label|button)\b", re.I)
HEAD_TAGS = {"title": ("page_title", "medium"), "legend": ("heading", "high"), "caption": ("heading", "high")}
HEAD_TAGS.update({"h%d" % i: ("heading", "high") for i in range(1, 7)})
TYPE_RANK = {"heading_styled": 0, "heading": 1, "heading_class": 2, "page_title": 3, "bold_text": 4}
SUPERGLOBAL = {"_POST", "_GET", "_REQUEST", "_SESSION", "_COOKIE", "_SERVER", "_FILES"}


# ----------------------------------------------------------------- text helpers
def inner_text(raw):
    """(term, pattern, dynamic_php) from the inner HTML of an element."""
    cut = re.search(r"<(?:div|table|form|tr|td|th|ul|ol|li|h[1-6]|p)\b", raw, re.I)
    if cut:
        raw = raw[:cut.start()]
    masked, store = FA.mask_php(raw)
    plain = html.unescape(TC.TAG_RE.sub(" ", masked)).replace("\u00a0", " ")
    pattern = " ".join(re.sub(r"\x00\d+\x00", " {php} ", plain).split())
    dyn = [store[int(i)] for i in re.findall(r"\x00(\d+)\x00", plain)]
    term = " ".join(pattern.replace("{php}", " ").split()).strip(" -:\t\u2013\u2014|>")
    return term, pattern, FA.squash("; ".join(dyn), 160)


def style_info(attrs):
    st = attrs.get("style", "").lower()
    size = None
    m = re.search(r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt)", st)
    if m:
        size = float(m.group(1)) * (1.33 if m.group(2) == "pt" else 1)
    bold = bool(re.search(r"font-weight\s*:\s*(?:bold|[6-9]00)", st))
    return size, bold


def words(s):
    return len(re.findall(r"[A-Za-z0-9]+", s))


def has_letters(s):
    return bool(re.search(r"[A-Za-z]", s))


def element_inner(text, end, name):
    cm = re.compile(r"</%s\s*>" % name, re.I).search(text, end + 1, end + 1 + 800)
    return text[end + 1: cm.start()] if cm else text[end + 1: end + 1 + 200]


# ----------------------------------------------------------------- elements, links, alerts
def scan_elements(rel, d, files_lc, base_idx):
    text, starts, funcs = d["text"], d["starts"], d["funcs"]
    rows, label_for = [], []
    for m in OPEN_RE.finditer(text):
        tag = m.group(1).lower()
        end = FA.scan_tag(text, m.start())
        if end < 0:
            continue
        attrs = FA.parse_tag(text[m.start():end + 1])
        line = EL.line_of(starts, m.start())
        func = EL.enclosing(funcs, m.start())
        raw = element_inner(text, end, tag)
        term, pattern, dyn = inner_text(raw)
        if not term or not has_letters(term) or len(term) > 110:
            continue
        base = {"term": term, "file": rel, "line": line, "tag": tag, "pattern": pattern, "dynamic_php": dyn,
                "mode_dependent": "yes" if re.search(r"action|mode", dyn, re.I) else "",
                "href_target": "", "php_function": func}
        if tag in HEAD_TAGS:
            typ, conf = HEAD_TAGS[tag]
        elif tag == "a":
            pm = EL.PATH_RE.search(attrs.get("href", ""))
            if not pm:
                continue
            tgt, status = EL.resolve(rel, pm.group(1), files_lc, base_idx)
            base["href_target"] = tgt or pm.group(1)
            typ, conf = "link_text", ("high" if status in ("yes", "basename") else "medium")
        elif tag == "label":
            if attrs.get("for"):
                label_for.append((term, attrs["for"], line))
            continue
        elif tag == "button":
            typ, conf = "button_label", "high"
            base["href_target"] = attrs.get("name", "")
        else:
            size, bold = style_info(attrs)
            if tag in ("b", "strong"):
                bold = True
            cls_head = bool(re.search(r"title|heading|header", attrs.get("class", ""), re.I))
            if pattern.rstrip().endswith(":") or term.endswith(":"):
                continue                                   # looks like a field label
            if size and size >= 14:
                typ, conf = "heading_styled", "high"
                if words(term) > 12:
                    continue
            elif cls_head:
                typ, conf = "heading_class", "medium"
            elif bold and 2 <= words(term) <= 10:
                typ, conf = "bold_text", "low"
            else:
                continue
        base.update({"term_type": typ, "confidence": conf})
        rows.append(base)
    return rows, label_for


def scan_alerts(rel, d):
    text, starts, funcs = d["text"], d["starts"], d["funcs"]
    rows = []
    for m in re.finditer(r"\b(alert|die|exit)\s*\(", text):
        op = m.end() - 1
        cl = FA.find_close(text, op, "(", ")")
        inner = text[op + 1:cl]
        sm = re.match(r"\s*\\?(['\"])(.*?)\\?\1\s*$", inner, re.S)
        if not sm:
            continue
        msg = " ".join(html.unescape(sm.group(2)).replace("\\n", " ").replace("\\'", "'").replace('\\"', '"').split())
        if words(msg) < 2 or len(msg) > 300:
            continue
        rows.append({"term": msg, "file": rel, "line": EL.line_of(starts, m.start()), "tag": m.group(1),
                     "pattern": msg, "dynamic_php": "", "mode_dependent": "", "href_target": "",
                     "php_function": EL.enclosing(funcs, m.start()), "term_type": "alert_message",
                     "confidence": "high"})
    return rows


# ----------------------------------------------------------------- field labels
def cell_info(raw, colset):
    masked, _ = FA.mask_php(raw)
    static = " ".join(html.unescape(TC.TAG_RE.sub(" ", re.sub(r"\x00\d+\x00", " ", masked))).replace("\u00a0", " ").split())
    refs = []
    for r in re.findall(r"\bname\s*=\s*[\"']?(\w+)", raw):
        refs.append((r, "input"))
    for var, key in re.findall(r"\$(\w+)\s*\[\s*['\"](\w+)['\"]\s*\]", raw):
        if var not in SUPERGLOBAL or True:
            refs.append((key, "row_key"))
    for v in re.findall(r"\$([A-Za-z]\w*)", raw):
        refs.append((v, "php_var"))
    keep, seen = [], set()
    for col, el in refs:
        ok = (col in colset) if colset else (not col.lower().startswith("btn") and col not in ("id", "action", "submit"))
        if ok and col not in seen:
            seen.add(col)
            keep.append((col, el))
    return static.strip(" :\t-"), keep, static


def scan_field_labels(rel, d, colset, col_tables, label_for):
    text, starts = d["text"], d["starts"]
    out, header = [], None

    def add(label, col, el, kind, line):
        if label and has_letters(label) and len(label) <= 60 and words(label) <= 8:
            out.append({"label": label, "column": col, "tables": ";".join(col_tables.get(col, [])), "file": rel,
                        "line": line, "kind": kind, "element": el})

    for rm in re.finditer(r"<tr\b", text, re.I):
        rend = FA.scan_tag(text, rm.start())
        if rend < 0:
            continue
        cl = re.compile(r"</tr\s*>", re.I).search(text, rend + 1)
        inner = text[rend + 1: cl.start() if cl else rend + 4000]
        cells = []
        for cm in re.finditer(r"<t([dh])\b", inner, re.I):
            cend = FA.scan_tag(inner, cm.start())
            if cend < 0:
                continue
            nxt = re.compile(r"<t[dh]\b", re.I).search(inner, cend + 1)
            cells.append(cell_info(inner[cend + 1: nxt.start() if nxt else len(inner)], colset))
        line = EL.line_of(starts, rm.start())
        labels = [(i, c[0]) for i, c in enumerate(cells) if c[0] and not c[1] and has_letters(c[0])]
        data = any(c[1] for c in cells)
        if len(labels) >= 2 and not data:                          # header row
            header = {i: t for i, t in labels}
            continue
        if data:
            for i, (label, refs, _) in enumerate(cells):
                if refs and label and has_letters(label):                 # "Style No: <input name=..>"
                    for col, el in refs:
                        add(label, col, el, "same_cell", line)
            pending = None
            for i, (label, refs, _) in enumerate(cells):
                if label and not refs and has_letters(label):
                    pending = label
                elif refs and pending:
                    for col, el in refs:
                        add(pending, col, el, "td_next", line)
                    pending = None
                elif refs and header and i in header and not (label and has_letters(label)):
                    for col, el in refs:
                        add(header[i], col, el, "header_index", line)
    for label, target, line in label_for:
        if (target in colset) if colset else True:
            add(label, target, "label_for", "label_for", line)
    return out


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--schema", nargs="*", default=[], help="files with CREATE TABLE (labels are kept only for real columns)")
    ap.add_argument("--skip", nargs="*", default=[".git", "node_modules", "vendor"])
    ap.add_argument("--aliases", help="JSON term aliases (optional)")
    ap.add_argument("--form-actions", help="form_actions.csv from extract_form_actions.py (adds button labels)")
    EL.add_filter_args(ap)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    aliases = TC.load_aliases(args.aliases)

    schema = EL.load_schema(args.schema)
    colset = {c for cols in schema.values() for c in cols}
    col_tables = defaultdict(list)
    for t, cols in schema.items():
        for c in cols:
            col_tables[c].append(t)

    files, excluded = EL.collect_files(args.src, args.skip, EL.make_filter(args))
    if not files:
        print("No PHP files found under", args.src)
        return 1
    files_lc = {k.lower(): k for k in files}
    base_idx = defaultdict(list)
    for k in files:
        base_idx[os.path.basename(k).lower()].append(k)

    terms, labels = [], []
    for rel, path in sorted(files.items()):
        text = re.sub(r"<!--.*?-->", EL.blank, EL.strip_comments(EL.read_text(path)), flags=re.S)
        d = {"text": text, "starts": EL.line_starts(text), "funcs": EL.find_functions(text)}
        rows, label_for = scan_elements(rel, d, files_lc, base_idx)
        terms.extend(rows)
        terms.extend(scan_alerts(rel, d))
        labels.extend(scan_field_labels(rel, d, colset, col_tables, label_for))

    # button labels from form_actions.csv
    if args.form_actions and os.path.exists(args.form_actions):
        with open(args.form_actions, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("button_value"):
                    terms.append({"term": r["button_value"], "file": r["source_file"], "line": r["button_line"],
                                  "tag": r["button_element"], "pattern": r["button_value"], "dynamic_php": "",
                                  "mode_dependent": "", "href_target": r.get("action_target", ""),
                                  "php_function": "", "term_type": "button_label", "confidence": "high",
                                  "button_name": r.get("button_name", "")})

    # de-duplicate
    seen, uniq = set(), []
    for t in terms:
        k = (t["term"], t["term_type"], t["file"], t["line"])
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    terms = uniq
    for t in terms:
        t["norm_key"] = TC.norm_key(t["term"], aliases)
    seen, ul = set(), []
    for l in labels:
        k = (l["label"].lower(), l["column"], l["file"])
        if k not in seen:
            seen.add(k)
            l["norm_key"] = TC.norm_key(l["label"], aliases)
            ul.append(l)
    labels = ul

    EL.write_csv(os.path.join(args.out, "ui_terms.csv"),
                 ["term", "term_type", "confidence", "file", "line", "tag", "pattern", "dynamic_php", "mode_dependent",
                  "href_target", "php_function", "norm_key"],
                 [[t["term"], t["term_type"], t["confidence"], t["file"], t["line"], t["tag"], t["pattern"],
                   t["dynamic_php"], t["mode_dependent"], t["href_target"], t["php_function"], t["norm_key"]] for t in terms])
    EL.write_csv(os.path.join(args.out, "field_labels.csv"),
                 ["label", "column", "tables", "file", "line", "kind", "element", "norm_key"],
                 [[l["label"], l["column"], l["tables"], l["file"], l["line"], l["kind"], l["element"], l["norm_key"]] for l in labels])

    best = {}
    for t in terms:
        if t["term_type"] in TYPE_RANK:
            rank = (TYPE_RANK[t["term_type"]], int(t["line"]))
            if t["file"] not in best or rank < best[t["file"]][0]:
                best[t["file"]] = (rank, t)
    EL.write_csv(os.path.join(args.out, "file_titles.csv"), ["file", "title", "term_type", "line", "dynamic_php"],
                 [[f, b[1]["term"], b[1]["term_type"], b[1]["line"], b[1]["dynamic_php"]] for f, b in sorted(best.items())])

    bridge = defaultdict(lambda: {"terms": defaultdict(int), "entries": []})
    for t in terms:
        b = bridge[t["norm_key"]]
        b["terms"][t["term"]] += 1
        e = {"type": t["term_type"], "confidence": t["confidence"], "file": t["file"], "line": t["line"]}
        if t["href_target"]:
            e["target"] = t["href_target"]
        if t.get("button_name"):
            e["name"] = t["button_name"]
        if t["dynamic_php"]:
            e["dynamic_php"] = t["dynamic_php"]
        b["entries"].append(e)
    for l in labels:
        b = bridge[l["norm_key"]]
        b["terms"][l["label"]] += 1
        b["entries"].append({"type": "field_label", "confidence": "high", "file": l["file"], "line": l["line"],
                             "column": l["column"], "tables": l["tables"].split(";") if l["tables"] else [],
                             "kind": l["kind"]})
    with open(os.path.join(args.out, "term_bridge.jsonl"), "w", encoding="utf-8") as f:
        for key, b in sorted(bridge.items()):
            if not key:
                continue
            term = max(b["terms"], key=b["terms"].get)
            f.write(json.dumps({"key": key, "term": term, "variants": sorted(b["terms"]), "entries": b["entries"]},
                               ensure_ascii=False) + "\n")
    print("files=%d terms=%d field_labels=%d titles=%d bridge_keys=%d"
          % (len(files), len(terms), len(labels), len(best), len([k for k in bridge if k])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
