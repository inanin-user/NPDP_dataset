#!/usr/bin/env python3
"""display_calls.py - understand the two UI helper functions  displayField(...)  and  displayText(...).

    <td>Status</td>
    <td colspan="2">:
      <?php displayField("wsm_stage", 15, $_SESSION['pro_pws']['wsm_stage'],
                         $_SESSION['wsm_stage'] ? $_SESSION['wsm_stage'] : $wsm_stage, "style='...' readonly ", "", ""); ?></td>
    <td>Received Time</td>
    <td colspan="2">:&nbsp; <?php displayText($_SESSION['pro_pws']['wsm_receivedbytime1'],
                                              formatDateTime($wsm_receivedbytime1, 's')) ?></td>

For every call we record:
  ui_term        text of the nearest preceding <td> in the same row  ("Status", "Received Time") = the word the
                 user sees and writes in emails
  column         displayField: literal 1st argument; displayText: last key of the session flag
  flag_session   the $_SESSION[...][...] argument that is normally 1 (field display permission), e.g. pro_pws.wsm_stage
  value_expr     the value argument; value_vars / value_session / formatter are parsed out of it

Parameter roles are configurable (JSON, --display-config):
    {"displayField": {"column": 0, "size": 1, "flag": 2, "value": 3, "attrs": 4},
     "displayText":  {"flag": 0, "value": 1}}
(0-based positions). The defaults above follow the examples you gave; adjust if the real signatures differ.
Dependency-free except query_builder.find_close. Heuristic (regex) based.
"""
import html
import json
import re

import query_builder as QB

DEFAULT_SIGNATURES = {
    "displayField": {"column": 0, "size": 1, "flag": 2, "value": 3, "attrs": 4},
    "displayText": {"flag": 0, "value": 1},
}
SESS_PATH = re.compile(r"\$_SESSION((?:\s*\[\s*(?:'[^']*'|\"[^\"]*\"|[\w$]+)\s*\])+)")
CELL_OPEN = re.compile(r"<t[dh]\b", re.I)
NOT_FUNCS = {"isset", "empty", "array", "list", "if", "elseif", "count", "is_array"}


def load_signatures(path=None):
    sigs = {k: dict(v) for k, v in DEFAULT_SIGNATURES.items()}
    if path:
        with open(path, "r", encoding="utf-8-sig") as f:
            for name, roles in json.load(f).items():
                sigs[name] = {k: int(v) for k, v in roles.items()}
    return sigs


def keypath(g):
    return ".".join(re.findall(r"\[\s*['\"]?([^\]'\"]*?)['\"]?\s*\]", g))


def split_args(s):
    """Split a PHP argument list on top-level commas (string / bracket aware)."""
    args, cur, depth, q, i = [], [], 0, None, 0
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == "\\" and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
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
    raw = re.sub(r"<\?.*?(?:\?>|$)", " ", raw, flags=re.S)
    raw = html.unescape(re.sub(r"<[^>]*>", " ", raw)).replace("\u00a0", " ")
    return " ".join(raw.split()).strip(" :-\t")


def find_label(text, pos, call_rx):
    """Static text of the nearest preceding <td>/<th> (same row) that is not itself a value cell."""
    lo = max(0, pos - 3000)
    cs = None
    for m in CELL_OPEN.finditer(text, lo, pos):
        cs = m.start()
    if cs is None or re.search(r"</t[dh]\s*>", text[cs:pos], re.I):
        return ""
    rs = None
    for m in re.finditer(r"<tr\b", text[max(0, cs - 3000):cs], re.I):
        rs = max(0, cs - 3000) + m.start()
    if rs is None or re.search(r"</tr\s*>", text[rs:cs], re.I):
        return ""
    seg = text[rs:cs]
    ms = list(CELL_OPEN.finditer(seg))
    cells = [seg[m.start(): ms[i + 1].start() if i + 1 < len(ms) else len(seg)] for i, m in enumerate(ms)]
    for raw in reversed(cells):
        if call_rx.search(raw):
            continue                                   # another field's value cell
        label = static_text(raw)
        if re.search(r"[A-Za-z]", label) and len(re.findall(r"[A-Za-z0-9]+", label)) <= 8:
            return label
    return ""


def find_display_calls(text, line_of, enclosing, col_tables=None, signatures=None):
    sigs = signatures or DEFAULT_SIGNATURES
    names = "|".join(sorted(map(re.escape, sigs), key=len, reverse=True))
    call_rx = re.compile(r"\b(" + names + r")\s*\(")
    col_tables = col_tables or {}
    rows = []
    for m in call_rx.finditer(text):
        if re.search(r"function\s*&?\s*$", text[max(0, m.start() - 15):m.start()]):
            continue                                   # the definition, not a call
        op = m.end() - 1
        cl = QB.find_close(text, op, "(", ")")
        args = split_args(text[op + 1:cl])
        sig = sigs[m.group(1)]

        def arg(role):
            i = sig.get(role)
            return args[i] if i is not None and i < len(args) else ""

        lit = re.fullmatch(r"\s*['\"]([^'\"]+)['\"]\s*", arg("column"))
        fm = SESS_PATH.search(arg("flag"))
        flag = keypath(fm.group(1)) if fm else ""
        if lit:
            column, csrc = lit.group(1), "argument"
        elif flag:
            column, csrc = flag.split(".")[-1], "flag_key"
        else:
            column, csrc = "", ""
        value = arg("value")
        vsess = [keypath(x.group(1)) for x in SESS_PATH.finditer(value)]
        vvars = [v for v in dict.fromkeys(re.findall(r"\$([A-Za-z]\w*)", value))]
        fmt = [f for f in dict.fromkeys(re.findall(r"\b([A-Za-z_]\w*)\s*\(", value)) if f.lower() not in NOT_FUNCS]
        attrs = arg("attrs")
        parts = flag.split(".") if flag else []
        rows.append({
            "helper": m.group(1), "pos": m.start(), "end": cl + 1, "line": line_of(m.start()),
            "function": enclosing(m.start()), "column": column, "column_source": csrc,
            "ui_term": find_label(text, m.start(), call_rx), "flag_session": flag,
            "flag_scope": ".".join(parts[:-1]), "value_expr": " ".join(value.split())[:160],
            "value_vars": ";".join(vvars), "value_session": ";".join(dict.fromkeys(vsess)),
            "formatter": ";".join(fmt), "readonly": "yes" if re.search(r"readonly", attrs, re.I) else "",
            "tables": ";".join(col_tables.get(column, [])), "args": len(args),
        })
    return rows