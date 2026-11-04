#!/usr/bin/env python3
"""extract_form_actions.py - record HTML forms, their buttons, and the server-side handler of each button.

For every <form> and every submit/button element it records WHICH button (name/value) posts to WHICH
file, then finds where that file tests the button (e.g. isset($_POST['btnCheck2'])) and what the handler
block does (tables/columns written, functions called, redirects, $_SESSION keys).

Outputs (in --out):
  form_actions.csv     one row per button (and one row per form that has no button)
  button_handlers.csv  one row per (button, handler location); 'not_found' rows show gaps
  ui_actions.jsonl     one JSON record per button incl. a template-generated summary sentence

Python 3.8+, standard library only. Heuristic (regex) based; see README.md.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

import extract_labels as EL

ATTR_RE = re.compile(r"""([A-Za-z_:][\w:.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
BTN_RE = re.compile(r"<(input|button)\b", re.I)
FORM_RE = re.compile(r"<form\b", re.I)
BUTTON_TYPES = {"submit", "button", "image", "reset"}
SELF_RE = re.compile(r"PHP_SELF|SCRIPT_NAME|REQUEST_URI", re.I)


# ----------------------------------------------------------------- tag parsing
def mask_php(s):
    store = []

    def rep(m):
        store.append(m.group(0))
        return "\x00%d\x00" % (len(store) - 1)
    return re.sub(r"<\?.*?(?:\?>|$)", rep, s, flags=re.S), store


def unmask(s, store):
    return re.sub(r"\x00(\d+)\x00", lambda m: store[int(m.group(1))], s)


def scan_tag(text, start):
    """Index of the '>' that closes the tag starting at `start`; skips quotes and <? ... ?> blocks."""
    i, n, q = start + 1, len(text), None
    while i < n:
        if text.startswith("<?", i):
            j = text.find("?>", i)
            i = n if j < 0 else j + 2
            continue
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == ">":
            return i
        i += 1
    return -1


def parse_tag(tag):
    masked, store = mask_php(tag)
    masked = masked.replace('\\"', '"').replace("\\'", "'")
    attrs = {}
    for m in ATTR_RE.finditer(masked):
        v = next(g for g in (m.group(2), m.group(3), m.group(4), "") if g is not None)
        attrs.setdefault(m.group(1).lower(), unmask(v, store).strip())
    return attrs


def squash(s, n=200):
    return " ".join(s.split())[:n]


# ----------------------------------------------------------------- forms & buttons
def parse_forms(text):
    forms = []
    for m in FORM_RE.finditer(text):
        end = scan_tag(text, m.start())
        if end < 0:
            continue
        attrs = parse_tag(text[m.start():end + 1])
        close = re.search(r"</form\s*>", text[end:], re.I)
        nxt = FORM_RE.search(text, end + 1)
        cands = [end + close.end()] if close else []
        if nxt:
            cands.append(nxt.start())
        forms.append({"start": m.start(), "tag_end": end, "body_end": min(cands) if cands else len(text),
                      "attrs": attrs})
    return forms


def parse_buttons(text, forms):
    buttons, hidden = [], defaultdict(list)
    for m in BTN_RE.finditer(text):
        end = scan_tag(text, m.start())
        if end < 0:
            continue
        attrs = parse_tag(text[m.start():end + 1])
        kind = m.group(1).lower()
        typ = attrs.get("type", "submit" if kind == "button" else "text").lower()
        owner = None
        for i, f in enumerate(forms):
            if f["tag_end"] < m.start() < f["body_end"] and (owner is None or f["start"] > forms[owner]["start"]):
                owner = i
        if kind == "input" and typ == "hidden":
            if owner is not None:
                hidden[owner].append("%s=%s" % (attrs.get("name", ""), attrs.get("value", "")))
            continue
        if kind == "input" and typ not in BUTTON_TYPES:
            continue
        label = attrs.get("value", "")
        if kind == "button" and not label:
            close = re.search(r"</button\s*>", text[end:end + 400], re.I)
            if close:
                inner, _ = mask_php(text[end + 1:end + close.start()])
                label = squash(re.sub(r"<[^>]*>|\x00\d+\x00", " ", inner), 60)
        buttons.append({"pos": m.start(), "element": kind, "type": typ, "name": attrs.get("name", ""),
                        "value": label, "onclick": attrs.get("onclick", ""),
                        "formaction": attrs.get("formaction", ""), "owner": owner})
    return buttons, hidden


def form_target(rel, raw, files_lc, base_idx):
    r = (raw or "").strip()
    if r == "" or SELF_RE.search(r):
        return rel, "self"
    m = EL.PATH_RE.search(r)
    if not m:
        return "", "no_php_target"
    return EL.resolve(rel, m.group(1), files_lc, base_idx)


# ----------------------------------------------------------------- handler search
def find_close(text, open_idx, oc, cc):
    depth, i, n, q = 0, open_idx, len(text), None
    while i < n:
        c = text[i]
        if q:
            if c == "\\":
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == oc:
            depth += 1
        elif c == cc:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def handler_block(text, pos):
    """(kind, start, end, condition) of the if-block that tests the button at `pos`."""
    lo = max(0, pos - 400)
    for m in reversed(list(re.finditer(r"\b(?:if|elseif)\s*\(", text[lo:pos]))):
        open_idx = lo + m.end() - 1
        close_idx = find_close(text, open_idx, "(", ")")
        if close_idx < pos:
            continue
        cond = squash(text[open_idx + 1:close_idx], 160)
        j = close_idx + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if text[j:j + 1] == "{":
            return "if_block", lo + m.start(), find_close(text, j, "{", "}"), cond
        if text[j:j + 1] == ":":
            return "if_alt_syntax", lo + m.start(), min(len(text), j + 2500), cond
        k = text.find(";", j)
        return "if_statement", lo + m.start(), k if k >= 0 else j + 500, cond
    return "window", pos, min(len(text), pos + 1500), ""


def analyse_block(rel, d, start, end, project):
    files_lc, base_idx, defs, func_tables, call_rx = project
    text, starts = d["text"], d["starts"]
    seg = text[start:end + 1]
    tables, cols = defaultdict(set), defaultdict(set)
    for st in d["stmts"]:
        if start <= st["pos"] <= end:
            tables[st["table"]].add(st["op"])
            cols[st["table"]].update(c for c in st["cols"] if c)
    calls, via = [], defaultdict(set)
    if call_rx:
        for m in call_rx.finditer(seg):
            if re.search(r"function\s*&?\s*$", seg[max(0, m.start() - 15):m.start()]):
                continue
            name = m.group(1)
            if name not in calls:
                calls.append(name)
            for cf in defs[name]:
                for t, op in func_tables.get((cf, name), ()):
                    via[t].add("%s via %s()" % (op, name))
    redirects = []
    for m in EL.PATH_RE.finditer(seg):
        if "://" in seg[max(0, m.start() - 10):m.start()]:
            continue
        prefix = seg[max(0, m.start() - 50):m.start()]
        kind = "include" if re.search(r"\b(?:include|require)(?:_once)?\b\s*\(?\s*['\"]?[^'\"]*$", prefix) else EL.classify(prefix)
        tgt, status = EL.resolve(rel, m.group(1), files_lc, base_idx)
        item = "%s:%s" % (kind, tgt or m.group(1))
        if item not in redirects:
            redirects.append(item)
    session = sorted({EL.keypath(m.group(1)) for m in EL.SESS_RE.finditer(seg)})
    return {"tables": {t: sorted(v) for t, v in tables.items()},
            "columns_written": {t: sorted(v) for t, v in cols.items() if v},
            "calls": calls, "tables_via_calls": {t: sorted(v) for t, v in via.items()},
            "targets": redirects, "session_keys": session,
            "line_start": EL.line_of(starts, start), "line_end": EL.line_of(starts, end)}


def find_handlers(rel, d, name, project):
    pats = [(re.compile(r"\$_(?:POST|REQUEST|GET)\s*\[\s*['\"]" + re.escape(name) + r"['\"]\s*\]"), "superglobal")]
    if re.fullmatch(r"[A-Za-z_]\w*", name):
        pats.append((re.compile(r"\$" + re.escape(name) + r"(?!\w)"), "variable"))
    out, seen = [], set()
    for rx, kind in pats:
        for m in rx.finditer(d["text"]):
            bkind, bs, be, cond = handler_block(d["text"], m.start())
            if (bs, be) in seen:
                continue
            seen.add((bs, be))
            info = analyse_block(rel, d, bs, be, project)
            info.update({"file": rel, "line": EL.line_of(d["starts"], m.start()), "match_kind": kind,
                         "block_kind": bkind, "condition": cond})
            out.append(info)
    return out


def summarize(rec):
    f, b, fm = rec["source_file"], rec["button"], rec["form"]
    label = "'%s'" % b["value"] if b["value"] else "(no label)"
    s = "In %s, button %s (name=%s) %s" % (f, label, b["name"] or "-", "is inside form" if fm["index"] is not None else "has no form;")
    if fm["index"] is not None:
        s += " %s which sends %s to %s" % (fm["name"] or "(unnamed)", fm["method"] or "GET", fm["action_target"] or fm["action_raw"] or "?")
    if not b["name"]:
        return s + ". It has no name attribute, so no server-side handler can test it; client-side onclick: %s." % (b["onclick"] or "(none)")
    if not rec["handlers"]:
        return s + ". No handler for this button name was found."
    h = rec["handlers"][0]
    s += ". Handler: %s line %d when %s" % (h["file"], h["line"], h["condition"] or "(no condition)")
    w = ["%s(%s)" % (t, ", ".join(c)) for t, c in h["columns_written"].items()]
    via = ["%s %s" % (t, "/".join(v)) for t, v in h["tables_via_calls"].items()]
    if h["tables"]:
        s += "; tables: " + ", ".join("%s %s" % (t, "/".join(o)) for t, o in h["tables"].items())
    if w:
        s += "; columns written: " + "; ".join(w)
    if via:
        s += "; via functions: " + "; ".join(via)
    if h["targets"]:
        s += "; next: " + ", ".join(h["targets"])
    return s + "."


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip", nargs="*", default=[".git", "node_modules", "vendor"])
    EL.add_filter_args(ap)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    files, excluded = EL.collect_files(args.src, args.skip, EL.make_filter(args))
    if not files:
        print("No PHP files found under", args.src)
        return 1
    files_lc = {k.lower(): k for k in files}
    base_idx = defaultdict(list)
    for k in files:
        base_idx[os.path.basename(k).lower()].append(k)
    data, defs, func_tables = EL.load_project(files)
    names = [n for n in defs if n.lower() not in EL.KEYWORDS and len(n) >= 3]
    call_rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\s*\(") if names else None
    project = (files_lc, base_idx, defs, func_tables, call_rx)

    fa_rows, bh_rows, records = [], [], []
    fa_head = ["source_file", "form_line", "form_name", "form_id", "method", "action_raw", "action_target",
               "target_status", "query_params", "onsubmit", "hidden_fields", "button_element", "button_type",
               "button_name", "button_value", "button_line", "onclick", "onclick_targets", "formaction"]
    bh_head = ["source_file", "form_name", "button_name", "button_value", "handler_file", "handler_line",
               "match_kind", "block_kind", "condition", "block_lines", "tables", "columns_written", "calls",
               "tables_via_calls", "targets", "session_keys", "status"]

    for rel, d in sorted(data.items()):
        text, starts = d["text"], d["starts"]
        forms = parse_forms(text)
        buttons, hidden = parse_buttons(text, forms)
        with_button = {b["owner"] for b in buttons if b["owner"] is not None}

        def form_info(i):
            if i is None:
                return {"index": None, "name": "", "id": "", "method": "", "action_raw": "", "action_target": "",
                        "target_status": "", "query": "", "onsubmit": "", "hidden": "", "line": ""}
            a = forms[i]["attrs"]
            raw = a.get("action", "")
            tgt, status = form_target(rel, raw, files_lc, base_idx)
            q = " | ".join("%s=%s" % kv for kv in re.findall(r"[?&]([A-Za-z_]\w*)=([^&\"]*)", raw))
            return {"index": i, "name": a.get("name", ""), "id": a.get("id", ""), "method": a.get("method", "").upper(),
                    "action_raw": squash(raw), "action_target": tgt, "target_status": status, "query": squash(q),
                    "onsubmit": squash(a.get("onsubmit", "")), "hidden": squash("; ".join(hidden.get(i, [])), 300),
                    "line": EL.line_of(starts, forms[i]["start"])}

        # forms without any button still get a row
        for i in range(len(forms)):
            if i not in with_button:
                fi = form_info(i)
                fa_rows.append([rel, fi["line"], fi["name"], fi["id"], fi["method"], fi["action_raw"], fi["action_target"],
                                fi["target_status"], fi["query"], fi["onsubmit"], fi["hidden"], "", "", "", "", "", "", "", ""])
        for b in buttons:
            fi = form_info(b["owner"])
            line = EL.line_of(starts, b["pos"])
            click_t = ";".join(dict.fromkeys(m.group(1) for m in EL.PATH_RE.finditer(b["onclick"])))
            fa_rows.append([rel, fi["line"], fi["name"], fi["id"], fi["method"], fi["action_raw"], fi["action_target"],
                            fi["target_status"], fi["query"], fi["onsubmit"], fi["hidden"], b["element"], b["type"],
                            b["name"], b["value"], line, squash(b["onclick"]), click_t, squash(b["formaction"])])
            handlers = []
            if b["name"] and b["type"] != "reset":
                targets = []
                if b["formaction"]:
                    t, _ = form_target(rel, b["formaction"], files_lc, base_idx)
                    targets.append(t)
                if fi["action_target"]:
                    targets.append(fi["action_target"])
                if not targets:
                    targets.append(rel)
                for t in dict.fromkeys(x for x in targets if x):
                    if t in data:
                        handlers.extend(find_handlers(t, data[t], b["name"], project))
            if b["name"] and b["type"] != "reset":
                if handlers:
                    for h in handlers:
                        bh_rows.append([rel, fi["name"], b["name"], b["value"], h["file"], h["line"], h["match_kind"],
                                        h["block_kind"], h["condition"], "%d-%d" % (h["line_start"], h["line_end"]),
                                        ";".join("%s:%s" % (t, "/".join(o)) for t, o in h["tables"].items()),
                                        ";".join("%s.%s" % (t, c) for t, cs in h["columns_written"].items() for c in cs),
                                        ";".join(h["calls"]),
                                        ";".join("%s:%s" % (t, "/".join(v)) for t, v in h["tables_via_calls"].items()),
                                        ";".join(h["targets"]), ";".join(h["session_keys"]), "found"])
                else:
                    bh_rows.append([rel, fi["name"], b["name"], b["value"], "", "", "", "", "", "", "", "", "", "", "", "",
                                    "not_found"])
            rec = {"source_file": rel,
                   "form": {"index": fi["index"], "name": fi["name"], "id": fi["id"], "method": fi["method"],
                            "action_raw": fi["action_raw"], "action_target": fi["action_target"],
                            "target_status": fi["target_status"], "query_params": fi["query"],
                            "onsubmit": fi["onsubmit"], "hidden_fields": fi["hidden"], "line": fi["line"]},
                   "button": {"element": b["element"], "type": b["type"], "name": b["name"], "value": b["value"],
                              "line": line, "onclick": squash(b["onclick"]), "formaction": squash(b["formaction"])},
                   "handlers": handlers}
            rec["summary_template"] = summarize(rec)
            records.append(rec)

    EL.write_csv(os.path.join(args.out, "form_actions.csv"), fa_head, fa_rows)
    EL.write_csv(os.path.join(args.out, "button_handlers.csv"), bh_head, bh_rows)
    with open(os.path.join(args.out, "ui_actions.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    nf = sum(1 for r in bh_rows if r[-1] == "not_found")
    print("files=%d excluded=%d form_rows=%d buttons=%d handlers_not_found=%d"
          % (len(files), len(excluded), len(fa_rows), len(records), nf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
