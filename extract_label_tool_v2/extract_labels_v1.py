#!/usr/bin/env python3
"""extract_labels.py - static-analysis label extractor for legacy PHP projects.

Python 3.8+, standard library only. Regex/heuristic based; see README.md for limits.

Outputs (in --out):
  file_to_file.csv     include/require, links, form actions, redirects, ajax targets
  function_calls.csv   calls to functions defined elsewhere in the project
  table_to_file.csv    table <-> file, with operation, function, columns written
  column_to_file.csv   schema columns mentioned per file (needs --schema)
  session_map.csv      $_SESSION key <-> local variable relations
  labels.jsonl         one JSON record per PHP file (feeds Q/A generation)
"""
import argparse
import bisect
import csv
import json
import os
import re
import sys
from collections import defaultdict

from file_filter import FileFilter

PHP_EXT = (".php", ".inc", ".phtml")
KEYWORDS = {"if", "while", "for", "foreach", "switch", "array", "isset", "empty", "list",
            "echo", "print", "return", "function", "catch", "unset", "exit", "die", "elseif",
            "and", "or", "include", "require", "include_once", "require_once", "new"}
STOP_TABLES = {"the", "a", "an", "this", "that", "table", "dual", "select", "where", "set",
               "values", "it", "which", "here", "there", "your", "our", "my", "all", "each"}


# ----------------------------------------------------------------- helpers
def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def blank(m):
    return re.sub(r"[^\n]", " ", m.group(0))


BLOCK_C = re.compile(r"/\*.*?\*/", re.S)
LINE_C = re.compile(r"^[ \t]*(?://|#).*$", re.M)


def strip_comments(s):
    """Blank out comments but keep offsets/line numbers identical."""
    return LINE_C.sub(blank, BLOCK_C.sub(blank, s))


def line_starts(text):
    starts = [0]
    for m in re.finditer(r"\n", text):
        starts.append(m.end())
    return starts


def line_of(starts, pos):
    return bisect.bisect_right(starts, pos)


def find_block_end(text, open_idx):
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
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


FUNC_RE = re.compile(r"\bfunction\s+&?\s*([A-Za-z_]\w*)\s*\(")


def find_functions(text):
    out = []
    n = len(text)
    for m in FUNC_RE.finditer(text):
        j, depth = m.end(), 1
        while j < n and depth:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        k = j
        while k < n and text[k] not in "{;":
            k += 1
        if k >= n or text[k] == ";":
            continue
        out.append((m.start(), find_block_end(text, k), m.group(1)))
    return out


def enclosing(funcs, pos):
    best = None
    for s, e, name in funcs:
        if s <= pos <= e and (best is None or s > best[0]):
            best = (s, e, name)
    return best[2] if best else ""


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# ----------------------------------------------------------------- schema
def load_schema(paths):
    """Return {table: [columns]} parsed from CREATE TABLE statements (.sql or .md)."""
    schema = {}
    for p in paths:
        text = read_text(p)
        for m in re.finditer(r"CREATE\s+TABLE\s+`?(\w+)`?\s*\(", text, re.I):
            end = re.search(r"\)\s*ENGINE|\)\s*;", text[m.end():], re.I)
            body = text[m.end(): m.end() + (end.start() if end else 20000)]
            cols = re.findall(r"^\s*`(\w+)`\s+\w+", body, re.M)
            schema[m.group(1)] = cols
    return schema


# ----------------------------------------------------------------- file edges
INC_RE = re.compile(r"\b(include_once|include|require_once|require)\b\s*\(?\s*([^;]+?)\)?\s*;", re.I)
PATH_RE = re.compile(r"([A-Za-z0-9_\-./\\{}$]*[A-Za-z0-9_\-]\.php)\b")
CTX = [
    (re.compile(r"\blocation", re.I), "redirect"),
    (re.compile(r"\baction\s*=", re.I), "form_action"),
    (re.compile(r"\bhref\s*=", re.I), "link"),
    (re.compile(r"\bsrc\s*=", re.I), "src"),
    (re.compile(r"window\.open", re.I), "window_open"),
    (re.compile(r"XMLHttpRequest|\.ajax|\$\.(?:post|get)|\bfetch\s*\(|\burl\s*:|\.load\s*\(", re.I), "ajax"),
]


def classify(prefix):
    best, best_end = "reference", -1
    for rx, name in CTX:
        last = None
        for m in rx.finditer(prefix):
            last = m
        if last and last.end() > best_end:
            best, best_end = name, last.end()
    return best


def resolve(rel, raw, files_lc, base_idx):
    t = raw.replace("\\", "/").split("?")[0].strip()
    if not t or "$" in t or "{" in t:
        return "", "dynamic"
    cands = [os.path.normpath(os.path.join(os.path.dirname(rel), t)).replace("\\", "/"),
             os.path.normpath(t.lstrip("/")).replace("\\", "/")]
    for c in cands:
        if c.lower() in files_lc:
            return files_lc[c.lower()], "yes"
    hits = base_idx.get(os.path.basename(t).lower(), [])
    if len(hits) == 1:
        return hits[0], "basename"
    if len(hits) > 1:
        return ";".join(hits[:5]), "ambiguous"
    return "", "no"


def file_edges(rel, text, starts):
    edges, inc_spans = {}, []
    for m in INC_RE.finditer(text):
        expr = m.group(2).strip()
        lits = re.findall(r"['\"]([^'\"]+)['\"]", expr)
        if re.fullmatch(r"\(?\s*['\"][^'\"]+['\"]\s*\)?", expr):
            raw, kind = lits[0], "include"
        elif lits:
            raw, kind = lits[-1], "include_partial"
        else:
            raw, kind = expr, "include_dynamic"
        inc_spans.append((m.start(), m.end()))
        edges.setdefault((kind, raw), line_of(starts, m.start()))
    for m in PATH_RE.finditer(text):
        if any(s <= m.start() < e for s, e in inc_spans):
            continue
        raw = m.group(1)
        if "://" in text[max(0, m.start() - 10):m.start()] or raw.lower().startswith(("www.", "http")):
            continue
        kind = classify(text[max(0, m.start() - 50):m.start()])
        edges.setdefault((kind, raw), line_of(starts, m.start()))
    return edges


# ----------------------------------------------------------------- SQL / tables
TBL = r"(?:`?[\w$]+`?\s*\.\s*)?(`?(?:\{?\$\w+\}?|[A-Za-z_]\w*)`?)"
SQL_PATS = [
    ("DELETE", re.compile(r"\bDELETE\s+FROM\s+" + TBL, re.I)),
    ("INSERT", re.compile(r"\b(?:INSERT|REPLACE)\s+(?:IGNORE\s+)?INTO\s+" + TBL, re.I)),
    ("UPDATE", re.compile(r"\bUPDATE\s+(?:LOW_PRIORITY\s+|IGNORE\s+)*" + TBL + r"(?=[^;]{0,400}?\bSET\b)", re.I | re.S)),
    ("SELECT", re.compile(r"\b(?:FROM|JOIN)\s+" + TBL, re.I)),
]


def sql_statements(text, starts, funcs):
    found, delete_spans = [], []
    for op, rx in SQL_PATS:
        for m in rx.finditer(text):
            table = m.group(1).strip("`")
            if table.lower() in STOP_TABLES:
                continue
            if op == "DELETE":
                delete_spans.append((m.start(), m.end()))
            if op == "SELECT":
                if any(s <= m.start() < e for s, e in delete_spans):
                    continue
                if not re.search(r"\bSELECT\b|\bDELETE\b", text[max(0, m.start() - 600):m.start()], re.I):
                    continue
            cols = []
            if op == "UPDATE":
                win = re.split(r"\bWHERE\b", text[m.end(): m.end() + 1500], 1, flags=re.I)[0]
                cols = re.findall(r"(?:\bSET|,)\s*`?(\w+)`?\s*=(?!=)", win, re.I)
            elif op == "INSERT":
                cm = re.match(r"\s*\(([^)]*)\)\s*VALUES", text[m.end(): m.end() + 2000], re.I)
                if cm:
                    cols = [c.strip(" `\n\r\t") for c in cm.group(1).split(",")]
            found.append({"op": op, "table": table, "cols": cols, "pos": m.start(),
                          "line": line_of(starts, m.start()), "func": enclosing(funcs, m.start())})
    return found


# ----------------------------------------------------------------- $_SESSION
SESSKEY = r"(?:\s*\[\s*(?:'[^']*'|\"[^\"]*\"|[\w$]+)\s*\])+"
SESS_RE = re.compile(r"\$_SESSION(" + SESSKEY + r")")
ALIAS_RE = re.compile(r"\$(\w+)\s*[.+\-*]?=(?!=)\s*([^;]*?\$_SESSION[^;]*);")
WRITE_RE = re.compile(r"\$_SESSION(" + SESSKEY + r")\s*=(?!=)\s*([^;]+);")


def keypath(g):
    return ".".join(k for k in re.findall(r"\[\s*['\"]?([^\]'\"]*?)['\"]?\s*\]", g))


def session_rows(rel, text, starts, funcs, lines):
    rows, spans = [], []
    for m in ALIAS_RE.finditer(text):
        spans.append(m.span())
        expr = m.group(2).strip()
        kind = "alias" if re.fullmatch(r"\$_SESSION" + SESSKEY, expr) else "derived"
        for km in SESS_RE.finditer(expr):
            rows.append([rel, line_of(starts, m.start()), enclosing(funcs, m.start()),
                         keypath(km.group(1)), m.group(1), kind, " ".join(m.group(0).split())[:160]])
    for m in WRITE_RE.finditer(text):
        spans.append(m.span())
        rhs_vars = [v for v in re.findall(r"\$([A-Za-z_]\w*)", m.group(2)) if not v.startswith("_")]
        rows.append([rel, line_of(starts, m.start()), enclosing(funcs, m.start()),
                     keypath(m.group(1)), ";".join(dict.fromkeys(rhs_vars)), "write",
                     " ".join(m.group(0).split())[:160]])
    for m in SESS_RE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        ln = line_of(starts, m.start())
        line = lines[ln - 1]
        covars = [v for v in re.findall(r"\$([A-Za-z_]\w*)", line) if not v.startswith("_") and v != "this"]
        rows.append([rel, ln, enclosing(funcs, m.start()), keypath(m.group(1)),
                     ";".join(dict.fromkeys(covars)), "use", line.strip()[:160]])
    return rows


# ----------------------------------------------------------------- project loading (shared)
def collect_files(src, skip, flt=None):
    """Walk src; return ({rel: abs_path}, [(rel, reason)]). flt = FileFilter or None."""
    files, excluded = {}, []
    for root, dirs, names in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip]
        for n in names:
            if not n.lower().endswith(PHP_EXT):
                continue
            rel = os.path.relpath(os.path.join(root, n), src).replace("\\", "/")
            why = flt.reasons(n) if flt else []
            if why:
                excluded.append((rel, ";".join(why)))
            else:
                files[rel] = os.path.join(root, n)
    return files, excluded


def load_project(files):
    """Pass 1: comment-stripped text, functions, SQL statements. Returns (data, defs, func_tables)."""
    data, defs, func_tables = {}, defaultdict(list), defaultdict(set)
    for rel, path in sorted(files.items()):
        text = strip_comments(read_text(path))
        starts = line_starts(text)
        funcs = find_functions(text)
        stmts = sql_statements(text, starts, funcs)
        data[rel] = {"text": text, "starts": starts, "funcs": funcs, "stmts": stmts,
                     "lines": text.split("\n")}
        for s_, e_, name in funcs:
            defs[name].append(rel)
        for st in stmts:
            if st["func"]:
                func_tables[(rel, st["func"])].add((st["table"], st["op"]))
    return data, defs, func_tables


def add_filter_args(ap):
    ap.add_argument("--filter-config", help="JSON rules for file_filter.py (optional)")
    ap.add_argument("--no-filter", action="store_true", help="scan backup/draft/dated files too")


def make_filter(args):
    return None if args.no_filter else FileFilter(config_path=args.filter_config)


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="PHP project root")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--schema", nargs="*", default=[], help="files containing CREATE TABLE statements")
    ap.add_argument("--skip", nargs="*", default=[".git", "node_modules", "vendor"], help="directory names to skip")
    add_filter_args(ap)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    schema = load_schema(args.schema)
    files, excluded = collect_files(args.src, args.skip, make_filter(args))
    if not files:
        print("No PHP files found under", args.src)
        return 1
    write_csv(os.path.join(args.out, "excluded_files.csv"), ["file", "reason"], excluded)
    files_lc = {k.lower(): k for k in files}
    base_idx = defaultdict(list)
    for k in files:
        base_idx[os.path.basename(k).lower()].append(k)
    data, defs, func_tables = load_project(files)

    # --- file_to_file + session + tables
    ff_rows, tf_rows, sess_rows = [], [], []
    labels = {}
    reverse = defaultdict(set)
    written = defaultdict(set)  # file -> {(table, col)}
    for rel, d in data.items():
        lab = labels[rel] = {"file": rel, "includes": [], "links_to": [], "tables": {},
                             "columns_written": {}, "functions_defined": [], "calls": [],
                             "tables_via_calls": {}, "session_keys": [], "session_aliases": {}}
        for (kind, raw), ln in file_edges(rel, d["text"], d["starts"]).items():
            target, status = resolve(rel, raw, files_lc, base_idx)
            ff_rows.append([rel, kind, raw, target, status, ln])
            if status in ("yes", "basename"):
                reverse[(target, "included_by" if kind.startswith("include") else "linked_from")].add(rel)
            bucket = "includes" if kind.startswith("include") else "links_to"
            lab[bucket].append({"type": kind, "target": target or raw, "resolved": status})
        grouped = defaultdict(lambda: {"ops": set(), "cols": set(), "lines": [], "funcs": set()})
        for st in d["stmts"]:
            g = grouped[(st["table"], st["op"])]
            g["cols"].update(st["cols"]); g["lines"].append(st["line"]); g["funcs"].add(st["func"])
            for c in st["cols"]:
                written[rel].add((st["table"], c))
        for (table, op), g in sorted(grouped.items()):
            known = "unknown" if not schema else ("yes" if table in schema else "no")
            tf_rows.append([table, rel, op, ";".join(sorted(f for f in g["funcs"] if f)),
                            ";".join(map(str, sorted(set(g["lines"])))), ";".join(sorted(g["cols"])), known])
            lab["tables"].setdefault(table, []).append(op)
            if g["cols"]:
                lab["columns_written"].setdefault(table, []).extend(sorted(g["cols"]))
        lab["functions_defined"] = [n for _, _, n in d["funcs"]]
        srows = session_rows(rel, d["text"], d["starts"], d["funcs"], d["lines"])
        sess_rows.extend(srows)
        lab["session_keys"] = sorted({r[3] for r in srows})
        lab["session_aliases"] = {r[4]: r[3] for r in srows if r[5] in ("alias", "derived")}

    # --- function calls
    fc_rows = []
    names = [n for n in defs if n.lower() not in KEYWORDS and len(n) >= 3]
    call_rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\s*\(") if names else None
    for rel, d in data.items():
        if not call_rx:
            break
        seen = set()
        for m in call_rx.finditer(d["text"]):
            if re.search(r"function\s*&?\s*$", d["text"][max(0, m.start() - 15):m.start()]):
                continue
            name = m.group(1)
            caller = enclosing(d["funcs"], m.start())
            key = (caller, name)
            if key in seen:
                continue
            seen.add(key)
            where = defs[name]
            amb = "yes" if len(where) > 1 else "no"
            fc_rows.append([rel, caller, name, ";".join(where), amb, line_of(d["starts"], m.start())])
            labels[rel]["calls"].append({"function": name, "defined_in": where, "ambiguous": amb})
            for cf in where:
                for table, op in func_tables.get((cf, name), ()):
                    labels[rel]["tables_via_calls"].setdefault(table, set()).add(f"{op} via {name}()")
    for lab in labels.values():
        lab["tables_via_calls"] = {t: sorted(v) for t, v in lab["tables_via_calls"].items()}
        lab["included_by"] = sorted(reverse.get((lab["file"], "included_by"), []))
        lab["linked_from"] = sorted(reverse.get((lab["file"], "linked_from"), []))

    # --- column mentions (needs schema)
    col_rows = []
    if schema:
        col_tables = defaultdict(list)
        for t, cols in schema.items():
            for c in cols:
                col_tables[c].append(t)
        rx = re.compile(r"(?P<pre>[`'\"$\[])(?P<col>" + "|".join(sorted(map(re.escape, col_tables), key=len, reverse=True)) + r")(?!\w)")
        kinds = {"$": "variable", "[": "bare_index"}
        for rel, d in data.items():
            hits = defaultdict(lambda: {"kinds": set(), "n": 0, "first": 0})
            for m in rx.finditer(d["text"]):
                h = hits[m.group("col")]
                h["kinds"].add(kinds.get(m.group("pre"), "quoted")); h["n"] += 1
                h["first"] = h["first"] or line_of(d["starts"], m.start())
            for col, h in hits.items():
                for t in col_tables[col]:
                    col_rows.append([t, col, rel, ";".join(sorted(h["kinds"])), h["first"], h["n"],
                                     "yes" if (t, col) in written[rel] else "no"])

    write_csv(os.path.join(args.out, "file_to_file.csv"),
              ["source", "edge_type", "target_raw", "target_resolved", "resolved", "line"], ff_rows)
    write_csv(os.path.join(args.out, "function_calls.csv"),
              ["caller_file", "caller_function", "callee", "defined_in", "ambiguous", "line"], fc_rows)
    write_csv(os.path.join(args.out, "table_to_file.csv"),
              ["table", "file", "operation", "function", "lines", "columns_written", "in_schema"], tf_rows)
    write_csv(os.path.join(args.out, "column_to_file.csv"),
              ["table", "column", "file", "mention_kinds", "first_line", "count", "written"], col_rows)
    write_csv(os.path.join(args.out, "session_map.csv"),
              ["file", "line", "function", "session_key", "local_vars", "kind", "snippet"], sess_rows)
    with open(os.path.join(args.out, "labels.jsonl"), "w", encoding="utf-8") as f:
        for lab in labels.values():
            f.write(json.dumps(lab, ensure_ascii=False) + "\n")

    print(f"files={len(files)} excluded={len(excluded)} file_edges={len(ff_rows)} call_edges={len(fc_rows)} "
          f"table_rows={len(tf_rows)} column_rows={len(col_rows)} session_rows={len(sess_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
