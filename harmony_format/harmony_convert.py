#!/usr/bin/env python3
"""harmony_convert.py - turn dataset rows into chat conversations (lists of {role, channel?, content}) for harmony_render.

Only data that REALLY is a conversation is converted:
  squad       context + question  ->  user;  answer span -> assistant final
  structured  support email / ticket (user)  ->  assistant: optional `analysis` (the reasoning chain term -> screen -> table.column,
              built from the evidence in the record) + `final` (a plain-English resolution that names screen, table and column, or a
              clarifying question when the term is ambiguous). Everything the assistant says comes from the knowledge-derived record,
              never from an LLM; wording varies deterministically with the record id.
  contrastive two questions in one conversation; the second answer is preceded by a short analysis that points at the one changed term.
  chat        generic public chat data: {"messages": [...]}, ShareGPT {"conversations": [...]}, Alpaca {instruction, input, output}
Web text and code are NOT conversations and stay plain text.
"""
import hashlib

import harmony_render as HR


def _h(key):
    return int(hashlib.blake2b(str(key).encode("utf-8"), digest_size=4).hexdigest(), 16)


def _pick(items, h, salt=0):
    return items[(h + salt) % len(items)]


def _one(s):
    return " ".join(str(s or "").split())


def _with_prob(h, prob):
    return (h % 1000) < prob * 1000


# ----------------------------------------------------------------- squad
def conv_squad(row):
    answers = row.get("answers") or {}
    a = _one((answers.get("text") or [""])[0])
    ctx, q = _one(row.get("context")), _one(row.get("question"))
    if not (a and ctx and q):
        return None
    h = _h(row.get("id") or q)
    user = _pick(["Context: %s\nQuestion: %s", "Read the text and answer the question.\n\n%s\n\nQuestion: %s",
                  "%s\n\n%s"], h) % (ctx, q)
    return [{"role": "user", "content": user}, {"role": "assistant", "channel": "final", "content": a}]


# ----------------------------------------------------------------- structured term linking
FIELD_FINAL = [
    "\"{ui}\" is the {ui} field on the {title} screen. It is stored in column `{col}` of table `{table}`.",
    "On the {title} screen ({page}), \"{ui}\" is saved in `{table}.{col}`.",
    "That is the {ui} field. The system keeps it in the `{col}` column of the `{table}` table (screen: {title}).",
    "You mean {ui} on {title}: the value lives in `{table}.{col}`.",
    "{ui} -> `{table}.{col}` (screen {title}, file {page}).",
    "Short answer: `{col}` in table `{table}`. It is the {ui} field on {title}.",
]
FIELD_ANALYSIS = [
    "The writer says \"{surf}\". In the glossary this is the label \"{ui}\" on the screen \"{title}\" ({page}). The label belongs to "
    "column {col} of table {table} (evidence: {source}).",
    "Term: {surf}. It matches the UI label {ui} on {title}. That field is saved in {table}.{col}. Source: {source}.",
    "Check the glossary: {ui} on {title} -> {table}.{col}. The page is {page}. {extra}",
]
AMB_FINAL = [
    "\"{ui}\" exists on more than one screen, so I want to be sure: do you mean {opts}? Tell me which screen you are on and I will "
    "point you to the right field.",
    "{question} The candidates are {opts}.",
    "I cannot tell which \"{ui}\" you mean. It is used on {opts}. Which screen are you working in?",
]
AMB_ANALYSIS = [
    "The term \"{ui}\" appears on several screens: {opts}. The message does not say which screen, so any answer would be a guess. "
    "I should ask which screen is meant.",
    "\"{ui}\" is ambiguous here ({opts}) and the text gives no screen name. Ask for clarification instead of choosing.",
]


def _fmt(tpl, **kw):
    return tpl.format(**kw)


def _tables_of(rec, page):
    return sorted({r["object"] for r in rec.get("relationships", []) if r.get("relation") == "touches_table" and r.get("subject") == page})


def conv_structured(row, analysis_prob=0.6):
    """structured.jsonl record (to_structured output) -> conversation, or None."""
    if row.get("task") == "contrastive_term_linking":
        return conv_pair(row, analysis_prob)
    user = _one(row.get("user_input")) if "\n" not in (row.get("user_input") or "") else (row.get("user_input") or "").strip()
    if not user:
        return None
    h = _h(row.get("id") or user)
    decision = row.get("expected_decision")
    ev = row.get("evidence") or []
    source = ev[0].get("source", "the glossary") if ev else "the glossary"
    msgs = [{"role": "user", "content": user}]

    if decision == "ask_clarification":
        cands = (row.get("ambiguity") or {}).get("candidates") or []
        if len(cands) < 2:
            return None
        ui = next((r["subject"] for r in row.get("relationships", []) if r.get("subject")), "that term")
        opts = " or ".join("'%s'%s" % (c["page_title"], " (`%s.%s`)" % (c["table"], c["column"]) if c.get("column") else "") for c in cands)
        if _with_prob(h, analysis_prob):
            msgs.append({"role": "assistant", "channel": "analysis", "content": _fmt(_pick(AMB_ANALYSIS, h), ui=ui, opts=opts)})
        final = _fmt(_pick(AMB_FINAL, h, 1), ui=ui, opts=opts, question=row.get("clarifying_question", "Which screen do you mean?"))
        msgs.append({"role": "assistant", "channel": "final", "content": final})
        return msgs

    ents = row.get("resolved_entities") or []
    if decision != "resolve" or not ents:
        return None
    e = ents[0]
    kind = e.get("entity_type")
    if kind == "field":
        d = dict(ui=e.get("ui_term") or e.get("business_term"), surf=e.get("business_term"), col=e["column"], table=e["table"],
                 page=e["page"], title=e.get("page_title") or e["page"], source=source)
        d["extra"] = ("The screen name in the message removes any ambiguity." if (row.get("ambiguity") or {}).get("disambiguated_by")
                      else "Nothing else matches, so I can answer directly.")
        analysis, final = _pick(FIELD_ANALYSIS, h), _pick(FIELD_FINAL, h, 3)
        if _with_prob(h, analysis_prob):
            msgs.append({"role": "assistant", "channel": "analysis", "content": _fmt(analysis, **d)})
        msgs.append({"role": "assistant", "channel": "final", "content": _fmt(final, **d)})
        return msgs
    if kind == "button":
        handler = e.get("handler", "")
        tables = sorted({r["object"] for r in row.get("relationships", []) if r.get("relation") == "touches_table"})
        final = "The \"%s\" button on %s is handled in %s%s." % (e["business_term"], e["page"], handler,
                                                                  "; it works on table %s" % ", ".join("`%s`" % t for t in tables) if tables else "")
    elif kind == "page":
        tables = _tables_of(row, e.get("file"))
        final = "\"%s\" is the screen implemented in %s%s." % (e["business_term"], e["file"],
                                                              "; it uses table %s" % ", ".join("`%s`" % t for t in tables) if tables else "")
    elif kind == "message":
        final = "That text is shown to the user by %s." % e["file"]
    else:
        return None
    msgs.append({"role": "assistant", "channel": "final", "content": final})
    return msgs


# ----------------------------------------------------------------- contrastive pairs
PAIR_FINAL = ["That value is stored in column `{field}` of table `{table}`.", "It is the `{field}` column in `{table}`.",
              "Look at `{table}.{field}`."]


def conv_pair(row, analysis_prob=0.6):
    ex = row.get("examples") or []
    if len(ex) != 2:
        return None
    h = _h(row.get("id") or row.get("pair_id") or ex[0]["input"])
    ctx = row.get("pair_type") == "context_pair"
    msgs = []
    for i, e in enumerate(ex):
        o = e["output"]
        final = _fmt(_pick(PAIR_FINAL, h, i), field=o["field"], table=o["table"])
        if ctx and o.get("page"):
            final += " (screen: %s)" % ((row.get("contexts") or [None, None])[i] or o["page"])
        msgs.append({"role": "user", "content": e["input"]})
        if i == 1 and _with_prob(h, analysis_prob):
            t1, t2 = row["distinguishing_terms"]
            what = "screen" if ctx else "term"
            msgs.append({"role": "assistant", "channel": "analysis", "content":
                         "This question has the same wording as the previous one, but the %s changed from \"%s\" to \"%s\". "
                         "A different %s maps to a different field, so the answer is different." % (what, t1, t2, what)})
        msgs.append({"role": "assistant", "channel": "final", "content": final})
    return msgs


# ----------------------------------------------------------------- generic chat data
_ROLE_MAP = {"human": "user", "user": "user", "gpt": "assistant", "assistant": "assistant", "bot": "assistant", "system": "system"}


def conv_chat(row):
    if isinstance(row.get("messages"), list):
        raw = [(m.get("role"), m.get("content")) for m in row["messages"]]
    elif isinstance(row.get("conversations"), list):
        raw = [(m.get("from"), m.get("value")) for m in row["conversations"]]
    elif row.get("instruction") is not None and row.get("output") is not None:
        inst = _one(row["instruction"]) if not row.get("input") else row["instruction"].strip() + "\n\n" + str(row["input"]).strip()
        raw = [("user", inst), ("assistant", row["output"])]
    else:
        return None
    msgs = []
    for role, content in raw:
        role = _ROLE_MAP.get(str(role).lower())
        if role and content and str(content).strip():
            msgs.append({"role": role, "channel": "final", "content": str(content)} if role == "assistant"
                         else {"role": role, "content": str(content)})
    if not any(m["role"] == "assistant" for m in msgs) or not any(m["role"] == "user" for m in msgs):
        return None
    return msgs
