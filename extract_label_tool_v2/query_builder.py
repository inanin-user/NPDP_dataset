#!/usr/bin/env python3
"""query_builder.py - resolve SQL that is BUILT over several PHP statements.

Typical legacy pattern handled here:

    $query  = "INSERT INTO `psht_mas` ";        # start (incomplete: no column list)
    $fields = "(";
    while (...) {
        if ($column == "wsm_wsno") { $fields = $fields . "$column,"; ... }   # -> column wsm_wsno
        else if (array_key_exists($column, $_POST)) { $fields = $fields . "$column,"; ... }  # -> dynamic
    }
    $query = $query . $fields . " VALUES " . $values;
    mysql_query($query);                         # end: first execution of $query

The resolver follows the query variable from its start statement to the first mysql_query($query)
(or ->query / mysqli_query ...), collects the columns appended through the "column" variables
(before VALUES for INSERT, before WHERE for UPDATE) and returns them in source order.

Columns taken from a loop variable ($column) are resolved through the enclosing
`if ($column == "name")` / `in_array($column, array(...))` condition. When the column can only be known
at run time the marker  <dynamic:POST[$column]>  (or <dynamic:$column>) is recorded instead.

Heuristic and regex based; no data-flow engine. See README.md for limits.
"""
import re

HTML, CODE, STR, CMT = 0, 1, 2, 3
HEREDOC_RE = re.compile(r"<<<[ \t]*(['\"]?)(\w+)\1\r?\n")
OPEN_RE = re.compile(r"<\?(?:php\b|=)?", re.I)
ASSIGN_RE = re.compile(r"\$(\w+)\s*(\.?)=(?!=)\s*")
TOKEN_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\$[A-Za-z_]\w*", re.S)
LIST_TOK = re.compile(r"(?:^|[\s,(])`?(\$?[A-Za-z_]\w*)`?(?=\s*(?:,|\)|$))")
ASSIGN_TOK = re.compile(r"(?:^|[\s,(])`?(\$?[A-Za-z_]\w*)`?\s*=(?!=)")
IF_RE = re.compile(r"\b(?:if|elseif)\s*\(", re.I)
EXEC_FN = r"(?:mysql_query|mysqli_query|mysql_unbuffered_query|mysql_db_query|pg_query|sqlsrv_query|odbc_exec)"
SQL_KEYWORDS = {"VALUES", "INSERT", "INTO", "SELECT", "SET", "WHERE", "AND", "OR", "NULL", "NOW", "FROM", "UPDATE"}


# ----------------------------------------------------------------- code mask
def code_mask(text):
    """bytearray, one byte per char: 0 html, 1 code, 2 string, 3 comment."""
    n = len(text)
    k = bytearray(n)
    i, state = 0, HTML
    while i < n:
        if state == HTML:
            m = OPEN_RE.search(text, i)
            if not m:
                break
            i, state = m.end(), CODE
            continue
        if text.startswith("?>", i):
            state = HTML
            i += 2
            continue
        c = text[i]
        if c in "'\"":
            k[i] = STR
            i += 1
            while i < n:
                k[i] = STR
                if text[i] == "\\":
                    if i + 1 < n:
                        k[i + 1] = STR
                    i += 2
                    continue
                if text[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n" and not text.startswith("?>", i):
                k[i] = CMT
                i += 1
            continue
        if text.startswith("<<<", i):
            hm = HEREDOC_RE.match(text, i)
            if hm:
                end = re.compile(r"\n[ \t]*" + re.escape(hm.group(2)) + r"\b").search(text, hm.end())
                stop = end.end() if end else n
                for j in range(i, stop):
                    k[j] = STR
                i = stop
                continue
        k[i] = CODE
        i += 1
    return k


def stmt_start(kinds, text, pos):
    i = pos
    while i > 0:
        kd = kinds[i - 1]
        if kd == HTML or (kd == CODE and text[i - 1] in ";{}"):
            break
        i -= 1
    return i


def stmt_end(kinds, text, pos):
    n, i = len(text), pos
    while i < n:
        if kinds[i] == HTML or (kinds[i] == CODE and text[i] == ";"):
            return i
        i += 1
    return n


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


def func_range(funcs, pos, n):
    best = None
    for s, e, _ in funcs:
        if s <= pos <= e and (best is None or s > best[0]):
            best = (s, e)
    return best if best else (0, n)


# ----------------------------------------------------------------- variable events
def var_events(text, kinds, v, lo, hi):
    """Appends/resets of $v in [lo, hi): list of (pos, 'append'|'reset', rhs_text)."""
    rx = re.compile(r"\$" + re.escape(v) + r"(?!\w)\s*(\.?)=(?!=)\s*")
    self_rx = re.compile(r"\$" + re.escape(v) + r"(?!\w)")
    out = []
    for m in rx.finditer(text, lo, hi):
        if kinds[m.start()] != CODE:
            continue
        se = stmt_end(kinds, text, m.end())
        rhs = text[m.end():se]
        if m.group(1):                                   # $v .= ...
            out.append((m.start(), "append", rhs))
            continue
        sm = re.match(r"\$" + re.escape(v) + r"(?!\w)\s*\.\s*", rhs)
        if sm:                                           # $v = $v . ...
            out.append((m.start(), "append", rhs[sm.end():]))
        elif not self_rx.search(rhs):                    # $v = <something not using $v>
            out.append((m.start(), "reset", rhs))
        # else: transformation such as $v = substr($v, 0, -1) -> ignored
    return out


# ----------------------------------------------------------------- column extraction
def literals(rhs):
    for m in re.finditer(r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'", rhs, re.S):
        yield m.group(1) if m.group(1) is not None else m.group(2)


def resolve_loop_token(text, lo, apos, name):
    """Column(s) for an append that uses loop variable $name, from the enclosing if-condition."""
    dynamic = None
    for im in reversed(list(IF_RE.finditer(text, lo, apos))):
        op = im.end() - 1
        cl = find_close(text, op, "(", ")")
        j = cl + 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if text[j:j + 1] != "{":
            continue
        be = find_close(text, j, "{", "}")
        if not (j < apos <= be):
            continue
        cond = text[op + 1:cl]
        m = re.search(r"\$%s\s*==\s*[\"'](\w+)[\"']|[\"'](\w+)[\"']\s*==\s*\$%s(?!\w)" % (name, name), cond)
        if m:
            return [m.group(1) or m.group(2)]
        m = re.search(r"in_array\(\s*\$%s\s*,\s*(?:array\s*\(|\[)([^)\]]*)" % name, cond)
        if m:
            lits = re.findall(r"[\"'](\w+)[\"']", m.group(1))
            if lits:
                return lits
        if dynamic is None and re.search(
                r"array_key_exists\(\s*\$%s\s*,\s*\$_(?:POST|REQUEST|GET)|isset\(\s*\$_(?:POST|REQUEST|GET)\[\s*\$%s\s*\]" % (name, name), cond):
            dynamic = ["<dynamic:POST[$%s]>" % name]
    return dynamic or ["<dynamic:$%s>" % name]


def extract_cols(text, lo, apos, rhs, style):
    cols = []
    for lit in literals(rhs):
        lit = re.sub(r"\{(\$\w+)\}", r"\1", lit)
        if style == "assign":
            part = re.split(r"\bWHERE\b", lit, 1, flags=re.I)[0]
            tokens = ASSIGN_TOK.findall(part)
        else:
            part = re.split(r"\bVALUES\b", lit, 1, flags=re.I)[0]
            tokens = LIST_TOK.findall(part)
        for t in tokens:
            if t.startswith("$"):
                cols.extend(resolve_loop_token(text, lo, apos, t[1:]))
            elif t.upper() not in SQL_KEYWORDS:
                cols.append(t)
    return cols


# ----------------------------------------------------------------- resolver
def resolve_built(text, starts, kinds, funcs, st):
    """Fill st['cols'] for an INSERT/UPDATE whose columns are appended in later statements."""
    op, pos, n = st["op"], st["pos"], len(text)
    ss, se = stmt_start(kinds, text, pos), stmt_end(kinds, text, pos)
    stmt = text[ss:se]
    am = ASSIGN_RE.search(stmt)
    var = am.group(1) if am and ss + am.start() < pos else None
    fs, fe = func_range(funcs, pos, n)

    exec_pos, hi, found = None, se, False
    if var:
        win_end = min(fe, se + 30000)
        er = re.compile(r"\b" + EXEC_FN + r"\s*\([^;]*?\$" + var + r"(?!\w)|->\s*(?:query|exec|execute|real_query|multi_query)\s*\([^;]*?\$" + var + r"(?!\w)", re.I).search(text, se, win_end)
        nr = re.compile(r"\$%s(?!\w)\s*=(?!=)\s*[\"']\s*(?:INSERT|UPDATE|DELETE|SELECT|REPLACE)\b" % re.escape(var), re.I).search(text, se, win_end)
        if er and (not nr or er.start() < nr.start()):
            exec_pos, hi, found = er.start(), er.end(), True
        else:
            hi = nr.start() if nr else win_end
    else:
        em = re.search(EXEC_FN + r"\s*\(|->\s*(?:query|exec|execute)\s*\(", stmt, re.I)
        if em:
            exec_pos, found = ss + em.start(), True

    chunks = [("init", ss + (am.end() if var else 0), stmt[(am.end() if var else 0):])]
    if var:
        for p, kind, rhs in var_events(text, kinds, var, se, hi):
            if kind == "append":
                chunks.append(("append", p, rhs))

    boundary = re.compile(r"\bVALUES\b|\bSELECT\b" if op == "INSERT" else r"\bWHERE\b", re.I)
    passed, set_vars, direct = False, [], []
    for kind, cpos, rhs in chunks:
        start_passed = passed
        for tm in TOKEN_RE.finditer(rhs):
            t = tm.group(0)
            if t[0] in "\"'":
                if boundary.search(t):
                    passed = True
            else:
                name = t[1:]
                if not passed and not name.startswith("_") and name not in (var, "this") and name not in set_vars:
                    set_vars.append(name)
        if kind == "append" and not start_passed:
            direct.append((cpos, rhs))

    style = "assign" if op == "UPDATE" else "list"
    cols = []
    for cpos, rhs in direct:
        cols += extract_cols(text, se, cpos, rhs, style)
    for v in set_vars:
        evs, base = var_events(text, kinds, v, se, hi), se
        if not evs:                                      # variable was built BEFORE the query statement
            base = max(fs, ss - 8000)
            evs = var_events(text, kinds, v, base, ss)
        last_reset = max((i for i, e in enumerate(evs) if e[1] == "reset"), default=0)
        for p, kind, rhs in evs[last_reset:]:
            cols += extract_cols(text, base, p, rhs, style)

    st["cols"] = list(dict.fromkeys(cols))
    st["built"] = True
    st["exec_found"] = found
    st["exec_line"] = (bisect_line(starts, exec_pos) if exec_pos is not None else "")
    return st


def bisect_line(starts, pos):
    import bisect
    return bisect.bisect_right(starts, pos)


def enrich_built_queries(text, starts, funcs, stmts, kinds=None):
    """For each INSERT/UPDATE without columns in its own statement, follow the query to its execution."""
    kinds = kinds if kinds is not None else code_mask(text)
    for st in stmts:
        if st["op"] in ("INSERT", "UPDATE") and not st["cols"]:
            try:
                resolve_built(text, starts, kinds, funcs, st)
            except Exception:          # never let one odd statement stop the whole run
                st["built"], st["exec_found"], st["exec_line"] = True, False, ""
    return stmts
