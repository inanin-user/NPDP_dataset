#!/usr/bin/env python3
"""extract_labels.py - static-analysis label extractor for legacy PHP projects.

Python 3.8+, standard library only. Regex/heuristic based; see README.md for limits.

Outputs (in --out):
  file_to_file.csv     include/require, links, form actions, redirects, ajax targets
  function_calls.csv   calls to functions defined elsewhere in the project
  table_to_file.csv    table <-> file, with operation, function, columns written
  column_to_file.csv   schema columns mentioned per file (needs --schema)
  session_map.csv      $_SESSION key <-> local variable relations (+ the table the key belongs to)
  session_bindings.csv allFormSession(table, 'INIT', extra) calls: which table a page's session keys belong to
  session_scope.csv    effective session table per file (own call, or inherited from the parent page)
  display_calls.csv    displayField/displayText UI terms, session flags and values
  labels.jsonl         one JSON record per PHP file (feeds Q/A generation)
"""
import argparse
import bisect
import csv
import html
import json
import os
import re
import sys
from collections import defaultdict

from file_filter import FileFilter
import query_builder as QB
from query_builder import code_mask, stmt_end, enrich_built_queries

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
    """Blank out comments (whole-line, block and trailing // or #) but keep offsets/line numbers identical."""
    s = LINE_C.sub(blank, BLOCK_C.sub(blank, s))
    kinds = code_mask(s)
    if 3 in kinds:                                   # 3 = comment inside PHP code
        s = "".join(" " if kinds[i] == 3 else c for i, c in enumerate(s))
    return s


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
    kinds = code_mask(text)
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
            se = stmt_end(kinds, text, m.start())
            if op == "UPDATE":
                win = re.split(r"\bWHERE\b", text[m.end(): se], 1, flags=re.I)[0]
                cols = re.findall(r"(?:\bSET|,)\s*`?(\w+)`?\s*=(?!=)", win, re.I)
            elif op == "INSERT":
                cm = re.match(r"\s*\(([^)]*)\)\s*VALUES", text[m.end(): se], re.I)
                if cm:
                    items = [c.strip(" `\n\r\t") for c in cm.group(1).split(",")]
                    if all(re.fullmatch(r"\w+", c) for c in items):   # plain column list only
                        cols = items
            found.append({"op": op, "table": table, "cols": cols, "pos": m.start(),
                          "line": line_of(starts, m.start()), "func": enclosing(funcs, m.start())})
    enrich_built_queries(text, starts, funcs, found, kinds)
    return found


# ----------------------------------------------------------------- $_SESSION
SESSKEY = r"(?:\s*\[\s*(?:'[^']*'|\"[^\"]*\"|[\w$]+)\s*\])+"
SESS_RE = re.compile(r"\$_SESSION(" + SESSKEY + r")")
ALIAS_RE = re.compile(r"\$(\w+)\s*[.+\-*]?=(?!=)\s*([^;]*?\$_SESSION[^;]*);")
WRITE_RE = re.compile(r"\$_SESSION(" + SESSKEY + r")\s*=(?!=)\s*([^;]+);")


def keypath(g):
    return ".".join(k for k in re.findall(r"\[\s*['\"]?([^\]'\"]*?)['\"]?\s*\]", g))


def session_rows(rel, text, starts, funcs, lines, skip_spans=()):
    """skip_spans: (start, end) ranges whose plain $_SESSION uses are already recorded elsewhere
    (displayField/displayText calls -> display_calls.csv)."""
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
        if any(s <= m.start() < e for s, e in spans) or any(s <= m.start() < e for s, e in skip_spans):
            continue
        ln = line_of(starts, m.start())
        line = lines[ln - 1]
        covars = [v for v in re.findall(r"\$([A-Za-z_]\w*)", line) if not v.startswith("_") and v != "this"]
        rows.append([rel, ln, enclosing(funcs, m.start()), keypath(m.group(1)),
                     ";".join(dict.fromkeys(covars)), "use", line.strip()[:160]])
    return rows


# ----------------------------------------------------------------- UI display helpers (SESSION / visible field mapping)
DEFAULT_SIGNATURES = {
    "displayField": {"column": 0, "size": 1, "flag": 2, "value": 3, "attrs": 4},
    "displayText": {"flag": 0, "value": 1},
}
SESS_PATH = re.compile(
    r"\$_SESSION((?:\s*\[\s*(?:'[^']*'|\"[^\"]*\"|[\w$]+)\s*\])+)"
)
CELL_OPEN = re.compile(r"<t[dh]\b", re.I)
NOT_FUNCS = {"isset", "empty", "array", "list", "if", "elseif", "count", "is_array"}


def load_signatures(path=None):
    """Load configurable 0-based argument positions for display helpers."""
    sigs = {name: dict(roles) for name, roles in DEFAULT_SIGNATURES.items()}
    if not path:
        return sigs
    with open(path, "r", encoding="utf-8-sig") as f:
        configured = json.load(f)
    if not isinstance(configured, dict):
        raise ValueError("--display-config must contain a JSON object")
    for name, roles in configured.items():
        if not isinstance(roles, dict):
            raise ValueError("display signature for {!r} must be a JSON object".format(name))
        parsed = {}
        for role, position in roles.items():
            try:
                position = int(position)
            except (TypeError, ValueError):
                raise ValueError("argument position for {}.{} must be an integer".format(name, role))
            if position < 0:
                raise ValueError("argument position for {}.{} cannot be negative".format(name, role))
            parsed[str(role)] = position
        # A supplied helper definition replaces that helper's defaults, so roles
        # omitted from the config are intentionally treated as unavailable.
        sigs[str(name)] = parsed
    return sigs


def split_args(s):
    """Split a PHP argument list on top-level commas (string/bracket aware)."""
    args, cur, depth, quote, i = [], [], 0, None, 0
    while i < len(s):
        c = s[i]
        if quote:
            cur.append(c)
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip() or args:
        args.append("".join(cur).strip())
    return args


def static_text(raw):
    """Return readable static cell text, excluding embedded PHP and HTML tags."""
    raw = re.sub(r"<\?.*?(?:\?>|$)", " ", raw, flags=re.S)
    raw = html.unescape(re.sub(r"<[^>]*>", " ", raw)).replace("\u00a0", " ")
    return " ".join(raw.split()).strip(" :-\t")


def find_label(text, pos, call_rx):
    """Find the nearest preceding static label cell in the same table row."""
    lo = max(0, pos - 3000)
    cell_start = None
    for match in CELL_OPEN.finditer(text, lo, pos):
        cell_start = match.start()
    if cell_start is None or re.search(r"</t[dh]\s*>", text[cell_start:pos], re.I):
        return ""

    row_start = None
    row_lo = max(0, cell_start - 3000)
    for match in re.finditer(r"<tr\b", text[row_lo:cell_start], re.I):
        row_start = row_lo + match.start()
    if row_start is None or re.search(r"</tr\s*>", text[row_start:cell_start], re.I):
        return ""

    segment = text[row_start:cell_start]
    cell_matches = list(CELL_OPEN.finditer(segment))
    cells = [segment[m.start(): cell_matches[i + 1].start()
                     if i + 1 < len(cell_matches) else len(segment)]
             for i, m in enumerate(cell_matches)]
    for raw in reversed(cells):
        if call_rx.search(raw):
            continue  # Do not mistake a previous field's value cell for a label.
        label = static_text(raw)
        if re.search(r"[A-Za-z]", label) and len(re.findall(r"[A-Za-z0-9]+", label)) <= 8:
            return label
    return ""


def find_display_calls(text, line_of, enclosing, col_tables=None, signatures=None):
    """Extract displayField/displayText calls and infer their visible UI labels."""
    sigs = signatures or DEFAULT_SIGNATURES
    names = "|".join(sorted((re.escape(n) for n in sigs), key=len, reverse=True))
    if not names:
        return []
    call_rx = re.compile(r"\b(" + names + r")\s*\(")
    col_tables = col_tables or {}
    rows = []

    for match in call_rx.finditer(text):
        if re.search(r"function\s*&?\s*$", text[max(0, match.start() - 15):match.start()]):
            continue  # Function definition, not a call.
        open_pos = match.end() - 1
        close_pos = QB.find_close(text, open_pos, "(", ")")
        if close_pos is None or close_pos < open_pos:
            continue
        args = split_args(text[open_pos + 1:close_pos])
        signature = sigs[match.group(1)]

        def arg(role):
            index = signature.get(role)
            return args[index] if index is not None and index < len(args) else ""

        literal_column = re.fullmatch(r"\s*['\"]([^'\"]+)['\"]\s*", arg("column"))
        flag_match = SESS_PATH.search(arg("flag"))
        flag = keypath(flag_match.group(1)) if flag_match else ""
        if literal_column:
            column, column_source = literal_column.group(1), "argument"
        elif flag:
            column, column_source = flag.split(".")[-1], "flag_key"
        else:
            column, column_source = "", ""

        value = arg("value")
        value_sessions = [keypath(m.group(1)) for m in SESS_PATH.finditer(value)]
        value_vars = [v for v in dict.fromkeys(re.findall(r"\$([A-Za-z]\w*)", value))]
        formatters = [
            name for name in dict.fromkeys(re.findall(r"\b([A-Za-z_]\w*)\s*\(", value))
            if name.lower() not in NOT_FUNCS
        ]
        attrs = arg("attrs")
        flag_parts = flag.split(".") if flag else []

        rows.append({
            "helper": match.group(1),
            "pos": match.start(),
            "end": close_pos + 1,
            "line": line_of(match.start()),
            "function": enclosing(match.start()),
            "column": column,
            "column_source": column_source,
            "ui_term": find_label(text, match.start(), call_rx),
            "flag_session": flag,
            "flag_scope": ".".join(flag_parts[:-1]),
            "value_expr": " ".join(value.split())[:160],
            "value_vars": ";".join(value_vars),
            "value_session": ";".join(dict.fromkeys(value_sessions)),
            "formatter": ";".join(formatters),
            "readonly": "yes" if re.search(r"readonly", attrs, re.I) else "",
            "tables": ";".join(col_tables.get(column, [])),
            "args": len(args),
        })
    return rows


# ----------------------------------------------------------------- session <-> table binding (allFormSession)
# allFormSession($tablename, 'INIT', $extra) creates one $_SESSION[<column>] per column of ONE table
# (INIT: empty values; otherwise: copies $_POST). So in a given PHP file every $_SESSION['column'] belongs to
# that one table. The binding is inherited by the pages the file links / posts / redirects / includes.
DEFAULT_BINDERS = {"allFormSession": {"table": 0, "init": 1, "extra": 2}}
NAV_KINDS = {"link", "form_action", "redirect", "ajax", "window_open", "src"}
MAX_AMBIGUOUS = 3        # an inherited scope with more tables than this is reported as 'too many' (no table claimed)


def load_binders(path=None):
    """Argument positions (0-based) of the session binder functions; same JSON shape as --display-config."""
    binders = {name: dict(roles) for name, roles in DEFAULT_BINDERS.items()}
    if not path:
        return binders
    with open(path, "r", encoding="utf-8-sig") as f:
        configured = json.load(f)
    if not isinstance(configured, dict):
        raise ValueError("--binder-config must contain a JSON object")
    for name, roles in configured.items():
        if not isinstance(roles, dict):
            raise ValueError("binder signature for {!r} must be a JSON object".format(name))
        parsed = {}
        for role, position in roles.items():
            try:
                position = int(position)
            except (TypeError, ValueError):
                raise ValueError("argument position for {}.{} must be an integer".format(name, role))
            if position < 0:
                raise ValueError("argument position for {}.{} cannot be negative".format(name, role))
            parsed[str(role)] = position
        binders[str(name)] = parsed
    return binders


def literal_or_var(text, pos, raw):
    """(value|None, source) for an argument: quoted literal, or a $var assigned a literal earlier in the file."""
    raw = (raw or "").strip()
    if not raw:
        return None, "missing"
    m = re.fullmatch(r"['\"]([^'\"]*)['\"]", raw)
    if m:
        return m.group(1), "literal"
    vm = re.fullmatch(r"\$([A-Za-z_]\w*)", raw)
    if vm:
        last = None
        for am in re.finditer(r"\$" + re.escape(vm.group(1)) + r"\s*=(?!=)\s*['\"]([^'\"]*)['\"]\s*;", text[:pos]):
            last = am
        if last:
            return last.group(1), "variable_assignment"
        return None, "unresolved_variable"
    return None, "unresolved_expression"


def find_session_bindings(text, line_of, enclosing, binders, schema=None):
    """Calls such as allFormSession('psht_mas', 'INIT', array(...)) -> one row per call."""
    if not binders:
        return []
    names = "|".join(sorted((re.escape(n) for n in binders), key=len, reverse=True))
    call_rx = re.compile(r"\b(" + names + r")\s*\(")
    rows = []
    for m in call_rx.finditer(text):
        if re.search(r"function\s*&?\s*$", text[max(0, m.start() - 15):m.start()]):
            continue                                   # the definition, not a call
        op = m.end() - 1
        cl = QB.find_close(text, op, "(", ")")
        args = split_args(text[op + 1:cl])
        sig = binders[m.group(1)]

        def arg(role):
            i = sig.get(role)
            return args[i] if i is not None and i < len(args) else ""

        table, tsrc = literal_or_var(text, m.start(), arg("table"))
        init, _ = literal_or_var(text, m.start(), arg("init"))
        mode = "dynamic" if init is None else ("INIT" if init.strip().upper() == "INIT" else "POST")
        extra = re.findall(r"['\"]([A-Za-z_]\w*)['\"]", arg("extra"))
        known = "unknown" if not schema or not table else ("yes" if table in schema else "no")
        rows.append({"file": "", "line": line_of(m.start()), "function": enclosing(m.start()), "call": m.group(1),
                     "table": table or "", "table_source": tsrc, "mode": mode,
                     "extra_fields": list(dict.fromkeys(extra)), "in_schema": known, "inherited_from": [],
                     "snippet": " ".join(text[m.start():cl + 1].split())[:160]})
    return rows


def build_session_scope(brows_by_file, ff_rows, depth=1, skip_files=()):
    """Effective session table(s) per file.

    own        : file calls a binder with a resolvable table
    inherited  : file has no resolvable binder call but is linked / posted / redirected to / included by a file
                 whose binder call is INIT ("the next php uses the same data source of the parent").
    Returns {file: {"tables": [...], "source": own|inherited|inherited_ambiguous|inherited_too_many,
                    "from": [...], "extra": [...], "depth": n}}
    """
    own, extra, seeds = {}, {}, {}
    for rel, brows in brows_by_file.items():
        tabs = list(dict.fromkeys(b["table"] for b in brows if b["table"]))
        if tabs:
            own[rel] = tabs
        ex = list(dict.fromkeys(x for b in brows for x in b["extra_fields"]))
        if ex:
            extra[rel] = ex
        init_tabs = list(dict.fromkeys(b["table"] for b in brows if b["table"] and b["mode"] == "INIT"))
        if init_tabs:
            seeds[rel] = init_tabs
    children = defaultdict(set)
    for src, kind, raw, target, status, ln in ff_rows:
        if status in ("yes", "basename") and target and target != src and (kind in NAV_KINDS or kind.startswith("include")):
            children[src].add(target)

    inherited = {}
    frontier = {rel: (tabs, extra.get(rel, [])) for rel, tabs in seeds.items()}
    for level in range(1, max(1, depth) + 1):
        reached = {}
        for parent, (tabs, ex) in frontier.items():
            for child in sorted(children.get(parent, ())):
                if child in own or child in skip_files:
                    continue                      # its own binder call wins / shared helper file
                rec = inherited.setdefault(child, {"tables": [], "from": [], "extra": [], "depth": level})
                for t in tabs:
                    if t not in rec["tables"]:
                        rec["tables"].append(t)
                for x in ex:
                    if x not in rec["extra"]:
                        rec["extra"].append(x)
                if parent not in rec["from"]:
                    rec["from"].append(parent)
                reached[child] = (rec["tables"][:], rec["extra"][:])
        frontier = reached
        if not frontier:
            break

    scope = {}
    for rel, tabs in own.items():
        scope[rel] = {"tables": tabs, "source": "own", "from": [], "extra": extra.get(rel, []), "depth": 0}
    for rel, rec in inherited.items():
        n = len(rec["tables"])
        src = "inherited" if n == 1 else ("inherited_ambiguous" if n <= MAX_AMBIGUOUS else "inherited_too_many")
        scope[rel] = {"tables": rec["tables"] if n <= MAX_AMBIGUOUS else [], "source": src,
                      "from": rec["from"], "extra": rec["extra"], "depth": rec["depth"],
                      "n_tables": n}
    return scope


def resolve_session_table(rel, key, scope, schema_sets, col_tables):
    """(table, source) for a $_SESSION key path such as 'wsm_stage' or 'pro_pws.wsm_stage'."""
    if not key:
        return "", ""
    if key.startswith("$"):
        return "", "dynamic_key"
    parts = key.split(".")
    col = parts[-1]
    sc = scope.get(rel)
    if sc and sc["tables"]:
        label = "bound" if sc["source"] == "own" else sc["source"]
        if col in sc["extra"] or parts[0] in sc["extra"]:
            return ";".join(sc["tables"]), "extra_field"
        if not schema_sets:
            return ";".join(sc["tables"]), label + "_unverified"
        hits = [t for t in sc["tables"] if col in schema_sets.get(t, ())]
        if hits:
            return ";".join(hits), label
        return "", "not_in_bound_table"          # e.g. username / login.level: not a column of the form's table
    cands = col_tables.get(col, [])
    if len(cands) == 1:
        return cands[0], "schema_unique"
    if len(cands) > 1:
        return "", "schema_ambiguous"
    return "", ""


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
DISPLAY_FIELDS = [
    "helper", "pos", "end", "line", "function", "column", "column_source",
    "ui_term", "flag_session", "flag_scope", "value_expr", "value_vars",
    "value_session", "formatter", "readonly", "tables", "args",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, help="PHP project root")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--schema", nargs="*", default=[], help="files containing CREATE TABLE statements")
    ap.add_argument("--skip", nargs="*", default=[".git", "node_modules", "vendor"], help="directory names to skip")
    ap.add_argument("--display-config", help="JSON argument positions for displayField/displayText (optional)")
    ap.add_argument("--binder-config", help="JSON argument positions for session binder functions such as "
                                            "allFormSession (optional)")
    ap.add_argument("--bind-depth", type=int, default=1,
                    help="link/form/redirect/include hops over which a parent's INIT table is inherited (default 1)")
    add_filter_args(ap)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    schema = load_schema(args.schema)
    schema_sets = {t: set(c) for t, c in schema.items()}
    display_signatures = load_signatures(args.display_config)
    binders = load_binders(args.binder_config)
    col_tables = defaultdict(list)
    for table_name, columns in schema.items():
        for column_name in columns:
            col_tables[column_name].append(table_name)

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

    # --- file_to_file + tables + (pending) session / display / binder rows
    ff_rows, tf_rows, sess_rows, display_rows, bind_rows, scope_rows = [], [], [], [], [], []
    labels = {}
    reverse = defaultdict(set)
    written = defaultdict(set)  # file -> {(table, col)}
    pending = {}
    for rel, d in data.items():
        lab = labels[rel] = {"file": rel, "includes": [], "links_to": [], "tables": {},
                             "columns_written": {}, "functions_defined": [], "calls": [],
                             "tables_via_calls": {}, "session_keys": [], "session_aliases": {},
                             "display_calls": [], "session_bindings": [], "session_scope": {}}
        for (kind, raw), ln in file_edges(rel, d["text"], d["starts"]).items():
            target, status = resolve(rel, raw, files_lc, base_idx)
            ff_rows.append([rel, kind, raw, target, status, ln])
            if status in ("yes", "basename"):
                reverse[(target, "included_by" if kind.startswith("include") else "linked_from")].add(rel)
            bucket = "includes" if kind.startswith("include") else "links_to"
            lab[bucket].append({"type": kind, "target": target or raw, "resolved": status})
        grouped = defaultdict(lambda: {"ops": set(), "cols": set(), "lines": [], "funcs": set()})
        built = []
        for st in d["stmts"]:
            if st.get("built"):
                built.append(st)                       # one CSV row per built query
            else:
                g = grouped[(st["table"], st["op"])]   # one-line queries are aggregated: lines "32;41"
                g["cols"].update(st["cols"]); g["lines"].append(st["line"]); g["funcs"].add(st["func"])
            for c in st["cols"]:
                written[rel].add((st["table"], c))
        for (table, op), g in sorted(grouped.items()):
            known = "unknown" if not schema else ("yes" if table in schema else "no")
            tf_rows.append([table, rel, op, ";".join(sorted(f for f in g["funcs"] if f)),
                            ";".join(map(str, sorted(set(g["lines"])))), ";".join(sorted(g["cols"])), known,
                            "single_line", ""])
            lab["tables"].setdefault(table, []).append(op)
            if g["cols"]:
                lab["columns_written"].setdefault(table, []).extend(sorted(g["cols"]))
        for st in sorted(built, key=lambda x: x["pos"]):
            known = "unknown" if not schema else ("yes" if st["table"] in schema else "no")
            kind = "built" if st["cols"] else "built_unresolved"
            if not st.get("exec_found"):
                kind += "_no_exec"
            tf_rows.append([st["table"], rel, st["op"], st["func"], str(st["line"]), ";".join(st["cols"]), known,
                            kind, st.get("exec_line", "")])
            lab["tables"].setdefault(st["table"], []).append(st["op"])
            if st["cols"]:
                lab["columns_written"].setdefault(st["table"], []).extend(st["cols"])
        lab["functions_defined"] = [n for _, _, n in d["funcs"]]

        line_fn = lambda pos, d=d: line_of(d["starts"], pos)
        func_fn = lambda pos, d=d: enclosing(d["funcs"], pos)
        drows = find_display_calls(d["text"], line_fn, func_fn, col_tables=col_tables, signatures=display_signatures)
        srows = session_rows(rel, d["text"], d["starts"], d["funcs"], d["lines"],
                             skip_spans=[(r["pos"], r["end"]) for r in drows])
        brows = find_session_bindings(d["text"], line_fn, func_fn, binders, schema)
        for b in brows:
            b["file"] = rel
        pending[rel] = (srows, drows, brows)

    # --- second pass: which table does $_SESSION['column'] belong to in each file?
    binder_files = {f for name in binders for f in defs.get(name, [])}     # files that DEFINE allFormSession
    scope = build_session_scope({rel: p[2] for rel, p in pending.items()}, ff_rows, args.bind_depth,
                                skip_files=binder_files)
    for rel, (srows, drows, brows) in pending.items():
        lab = labels[rel]
        sc = scope.get(rel)
        for b in brows:                                    # binder call whose table is a variable of the parent
            if not b["table"] and sc and sc["source"].startswith("inherited") and sc["tables"]:
                b["table"] = ";".join(sc["tables"])
                b["table_source"] = sc["source"]
                b["inherited_from"] = sc["from"]
                b["in_schema"] = "unknown" if not schema else ("yes" if all(t in schema for t in sc["tables"]) else "no")
            bind_rows.append([rel, b["line"], b["function"], b["call"], b["table"], b["table_source"], b["mode"],
                              ";".join(b["extra_fields"]), b["in_schema"], ";".join(b["inherited_from"]), b["snippet"]])
        lab["session_bindings"] = [{k: v for k, v in b.items() if k != "file"} for b in brows]
        if sc:
            scope_rows.append([rel, ";".join(sc["tables"]), sc["source"], ";".join(sc["from"]), sc["depth"],
                               ";".join(sc["extra"])])
            lab["session_scope"] = {"tables": sc["tables"], "source": sc["source"], "from": sc["from"],
                                    "extra_fields": sc["extra"]}

        for r in srows:                                    # r[3] = session key path
            tbl, tsrc = resolve_session_table(rel, r[3], scope, schema_sets, col_tables)
            sess_rows.append(r + [tbl, tsrc])
        lab["session_aliases"] = {r[4]: r[3] for r in srows if r[5] in ("alias", "derived")}

        for row in drows:
            tbl, tsrc = resolve_session_table(rel, row["column"], scope, schema_sets, col_tables)
            row["table"], row["table_source"] = tbl, tsrc
            if tbl:                                        # a bound table replaces the long schema candidate list
                row["tables"] = tbl
        lab["display_calls"] = drows
        display_rows.extend([[rel] + [row[f] for f in DISPLAY_FIELDS] + [row["table"], row["table_source"]]
                             for row in drows])

        # Keep every SESSION path (generic, display flags / values) discoverable in the file-level summary.
        display_session_keys = set()
        for row in drows:
            if row["flag_session"]:
                display_session_keys.add(row["flag_session"])
            display_session_keys.update(k for k in row["value_session"].split(";") if k)
        lab["session_keys"] = sorted({r[3] for r in srows} | display_session_keys)

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
        lab["tables"] = {t: list(dict.fromkeys(v)) for t, v in lab["tables"].items()}
        lab["columns_written"] = {t: list(dict.fromkeys(v)) for t, v in lab["columns_written"].items()}
        lab["tables_via_calls"] = {t: sorted(v) for t, v in lab["tables_via_calls"].items()}
        lab["included_by"] = sorted(reverse.get((lab["file"], "included_by"), []))
        lab["linked_from"] = sorted(reverse.get((lab["file"], "linked_from"), []))

    # --- column mentions (needs schema)
    col_rows = []
    if schema and col_tables:
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
              ["table", "file", "operation", "function", "lines", "columns_written", "in_schema",
               "statement_kind", "exec_line"], tf_rows)
    write_csv(os.path.join(args.out, "column_to_file.csv"),
              ["table", "column", "file", "mention_kinds", "first_line", "count", "written"], col_rows)
    write_csv(os.path.join(args.out, "session_map.csv"),
              ["file", "line", "function", "session_key", "local_vars", "kind", "snippet", "table", "table_source"],
              sess_rows)
    write_csv(os.path.join(args.out, "session_bindings.csv"),
              ["file", "line", "function", "call", "table", "table_source", "mode", "extra_fields", "in_schema",
               "inherited_from", "snippet"], bind_rows)
    write_csv(os.path.join(args.out, "session_scope.csv"),
              ["file", "tables", "source", "inherited_from", "depth", "extra_fields"], scope_rows)
    write_csv(os.path.join(args.out, "display_calls.csv"),
              ["file"] + DISPLAY_FIELDS + ["table", "table_source"], display_rows)
    with open(os.path.join(args.out, "labels.jsonl"), "w", encoding="utf-8") as f:
        for lab in labels.values():
            f.write(json.dumps(lab, ensure_ascii=False) + "\n")

    print(f"files={len(files)} excluded={len(excluded)} file_edges={len(ff_rows)} call_edges={len(fc_rows)} "
          f"table_rows={len(tf_rows)} column_rows={len(col_rows)} session_rows={len(sess_rows)} "
          f"display_calls={len(display_rows)} session_bindings={len(bind_rows)} session_scopes={len(scope_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
