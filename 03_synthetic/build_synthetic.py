#!/usr/bin/env python3
"""build_synthetic.py - glossary-conditioned and contrastive synthetic data, grounded in the extracted knowledge.

  plan      knowledge  -> requests.jsonl   one request per (term target x surface x variation): prompt + facts + labels
  generate  requests   -> generated_raw.jsonl   backends: template (offline) | anthropic | openai (any OpenAI-compatible
                                                server, e.g. vLLM / Ollama) | import (responses produced elsewhere)
  contrast  knowledge  -> generated_raw.jsonl   contrastive pairs (deterministic, no LLM needed)

Glossary-conditioned generation: every request carries ONE concrete target (e.g. field mld_gold of table mld_entry), the
surface form that must appear, a context phrase that removes ambiguity, forbidden confusable terms and variation controls
(persona / tone / language level / situation). The LABELS are computed here from the knowledge, never by the LLM.

Contrastive generation: minimal pairs (sibling fields whose labels differ by one word) and context pairs (same label,
different page -> different column/table). The two questions of a pair use the SAME template and differ only in the
distinguishing term, so the only way to answer correctly is to link that term.

Examples:
  python build_synthetic.py plan     --knowledge ../02_knowledge --out requests.jsonl --seed 7
  python build_synthetic.py generate --requests requests.jsonl --out generated_raw.jsonl --backend template
  python build_synthetic.py generate --requests requests.jsonl --out generated_raw.jsonl --backend anthropic --workers 4
  python build_synthetic.py generate --requests requests.jsonl --out generated_raw.jsonl --backend openai \\
         --endpoint http://localhost:8000/v1 --model my-model
  python build_synthetic.py contrast --knowledge ../02_knowledge --out generated_raw.jsonl --append --max-pairs 500
Python 3.8+, standard library only (HTTP via urllib). API keys come from ANTHROPIC_API_KEY / OPENAI_API_KEY.
"""
import argparse
import itertools
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import synth_common as SC
import terms_common as TC

# ----------------------------------------------------------------- variation controls
PERSONAS = ["a merchandiser", "a production planner", "a sales coordinator", "a factory supervisor",
            "a quality controller", "a purchasing officer", "a new employee who is still learning the system"]
TONES = ["polite and formal", "short and direct", "urgent but polite", "casual", "apologetic"]
LEVELS = ["fluent business English", "clear English with one or two small non-native phrasing slips",
          "simple English with short sentences"]
LENGTHS = {"short": "2-3 sentences", "medium": "4-6 sentences"}
ISSUES = {
    "field": {"find": "cannot find where to enter this field", "wrong": "the value of this field looks wrong",
              "update": "wants this field updated because the current entry is outdated",
              "meaning": "wants to know what this field is used for"},
    "page": {"open": "cannot open this function", "where": "does not know where to find this function",
             "error": "gets an error when using this function", "how": "wants to know how to use this function"},
    "button": {"noeffect": "clicked this button but nothing happened", "what": "wants to know what this button does",
               "mistake": "clicked this button by mistake and asks whether it can be undone"},
    "message": {"got": "received this message and does not know what to do",
                "unclear": "finds the wording of this message unclear"},
}
SURFACES = ("email", "ticket", "code_comment")


# ----------------------------------------------------------------- prompts
def facts_for(card, target):
    ui, k = card["term"], target["kind"]
    if k == "field":
        f = ["The system has a screen called '%s' with a field labelled '%s'." % (target["page_title"] or target["file"], ui)]
        if target["readonly"]:
            f.append("The field is read-only (locked).")
        if target["opens"]:
            f.append("Clicking the value opens another screen.")
    elif k == "button":
        f = ["The screen '%s' has a button labelled '%s'; pressing it saves or submits the form." % (target["page_title"] or target["file"], ui)]
    elif k == "page":
        f = ["The system has a function / screen called '%s'." % ui]
    else:
        f = ["The system shows the message '%s' to the user." % ui]
    return f


def build_prompt(req, card):
    c, t, surf = req["controls"], req["target"], req["required_surface"]
    ctx = req.get("context_phrase")
    forb = req["forbidden_terms"]
    rules = ['The text MUST contain this exact phrase, spelled exactly like this: "%s"' % surf]
    if ctx:
        rules.append('It MUST also mention the screen name "%s" exactly once, so the reader knows which screen is meant.' % ctx)
    if forb:
        rules.append("Do NOT mention any of these other terms: %s." % ", ".join('"%s"' % x for x in forb))
    facts = "\n".join("- " + x for x in req["facts"])
    if req["surface"] == "code_comment":
        names = ["table `%s`" % t["table"], "column `%s`" % t["column"]] if t["kind"] == "field" else \
                (["button `%s`" % t["button"]] if t["kind"] == "button" else ["file `%s`" % t["file"]])
        return ("Write ONE PHP code comment (1-3 lines, every line starting with //) that a developer puts above the code "
                "behind this screen element of a jewelry-production web application.\n"
                "Rules:\n- %s\n- It must also name exactly: %s.\n- Output only the comment lines: no code, no markdown, no explanation.\n"
                "Facts (do not copy literally):\n%s\n" % ("\n- ".join(rules), ", ".join(names), facts))
    common = ("Rules:\n- %s\n- Use business wording only: never mention database tables, columns, PHP files, SQL or code.\n"
              "- Use placeholders for identifiers: worksheet numbers as PS-XXXX, PO numbers as {{PO_NO}}, names as {{NAME}}. "
              "No real names, e-mail addresses or phone numbers.\n- Tone: %s. Language: %s. Length: %s.\n"
              "- Situation: the writer (%s) %s.\nFacts you may rely on (do not copy literally):\n%s\n"
              % ("\n- ".join(rules), c["tone"], c["level"], LENGTHS[c["length"]], c["persona"], ISSUES[t["kind"]][c["issue"]], facts))
    if req["surface"] == "email":
        return ("Write ONE realistic business email from %s at a jewelry manufacturing company to IT support.\n%s"
                "Output format: first line 'Subject: ...', a blank line, then the body. No markdown, no preface.\n" % (c["persona"], common))
    return ("Write ONE IT support ticket raised by %s (a business user, not a developer).\n%s"
            "Output format, exactly these headings, one per line or block: Title: / Description: / Steps to reproduce: / Expected: / Actual:\n"
            "No markdown, no preface.\n" % (c["persona"], common))


# ----------------------------------------------------------------- plan
def cmd_plan(a):
    kn = SC.Knowledge(a.knowledge)
    surfaces = [s for s in a.surfaces.split(",") if s]
    reqs, n = [], 0
    for key, card in sorted(kn.cards.items()):
        if a.kinds and not set(a.kinds.split(",")) & set(card["kinds"]):
            continue
        if card.get("generic") and "field" not in card["kinds"]:
            continue
        if card["evidence_count"] < a.min_evidence:
            continue
        for target in SC.derive_targets(card, kn):
            ambiguous = card["ambiguity"] != "low"
            if ambiguous and not target["page_title"]:
                continue                                   # cannot disambiguate -> do not generate
            for surface in surfaces:
                for i in range(a.per_surface):
                    rng = random.Random("%s|%s|%s|%s|%d" % (a.seed, key, target["id"], surface, i))
                    issue = rng.choice(sorted(ISSUES[target["kind"]]))
                    controls = {"persona": rng.choice(PERSONAS), "tone": rng.choice(TONES), "level": rng.choice(LEVELS),
                                "length": rng.choice(sorted(LENGTHS)), "issue": issue}
                    variants = card["variants"] or [card["term"]]
                    required = card["term"] if surface == "code_comment" else rng.choice(variants)
                    ctx = target["page_title"] if (target["page_title"] and target["kind"] != "page" and
                                                   (ambiguous or rng.random() < 0.5)) else ""
                    forb = [kn.cards[k]["term"] for k in card["confusable_with"] if k in kn.cards][:5]
                    forb = [f for f in forb if not SC.phrase_in_text(f, required + " " + ctx, kn.aliases)]
                    allowed = [key] + ([TC.norm_key(ctx, kn.aliases)] if ctx else [])
                    allowed += [d["key"] for d in kn.detector.detect(ctx)] if ctx else []
                    n += 1
                    req = {"id": "gcg_%06d" % n, "task": "glossary_conditioned_generation", "surface": surface,
                           "term_key": key, "target": target, "required_surface": required, "context_phrase": ctx,
                           "forbidden_terms": forb, "allowed_keys": sorted(set(allowed)), "controls": controls,
                           "facts": facts_for(card, target), "labels": SC.build_labels(card, target, required, kn)}
                    req["prompt"] = build_prompt(req, card)
                    reqs.append(req)
    if a.limit:
        reqs = random.Random(a.seed).sample(reqs, min(a.limit, len(reqs)))
    SC.write_jsonl(a.out, reqs)
    by = defaultdict(int)
    for r in reqs:
        by[r["surface"]] += 1
    print("requests=%d %s -> %s" % (len(reqs), dict(by), a.out))
    return 0


# ----------------------------------------------------------------- offline template backend
SUBJECTS = ["{s}", "Question about {s}", "Issue with {s}", "Help needed: {s}"]
SENT = {
    "field": {"find": ["I cannot find where to enter {s}{w}.", "Could you tell me where {s} is entered{w}?"],
              "wrong": ["The {s}{w} looks wrong for the worksheet I checked this morning.", "The value of {s}{w} is not what we agreed."],
              "update": ["Please help us update {s}{w}; the current entry is outdated.", "We need to change {s}{w} today."],
              "meaning": ["Could you explain what {s}{w} is used for?", "What does {s}{w} mean exactly?"]},
    "page": {"open": ["I cannot open {s} since this morning.", "The {s} function is not responding."],
             "where": ["Where can I find {s} in the system?", "I am looking for {s} but cannot see it in the menu."],
             "error": ["{s} shows an error when I try to save.", "I get an error in {s} when I confirm."],
             "how": ["How do I use {s}? I am new.", "Could you guide me how to use {s}?"]},
    "button": {"noeffect": ["I clicked {s}{w} but nothing happened.", "The {s} button{w} does not respond."],
               "what": ["What does the {s} button{w} do exactly?", "Could you explain the {s} button{w}?"],
               "mistake": ["I clicked {s}{w} by mistake. Can it be undone?", "I pressed {s}{w} too early, please advise."]},
    "message": {"got": ['I got this message{w}: "{s}". What should I do?', 'The system says "{s}"{w}. Is that normal?'],
                "unclear": ['The message "{s}"{w} is not clear to me.', 'Could you explain the message "{s}"{w}?']},
}
EXTRA = ["This is blocking our work today.", "We have a customer deadline this week.", "A colleague had the same problem yesterday.",
         "I checked again after logging out and in, but it is the same.", "Please let me know if you need more details from me."]
OPEN = ["Hi team,", "Hello,", "Dear IT support,", "Hi,"]
CLOSE = ["Thanks.", "Thank you for your help.", "Please advise.", "Appreciate your support."]
COMMENTS = {
    "field": ["// {ui}: stored in {table}.{column}", "// Field '{ui}' ({column}) of table {table}", "// {ui} -> column {column} in {table}"],
    "button": ["// {ui} button ({button}) is handled in {handler}", "// Handler for the '{ui}' button: {button}"],
    "page": ["// {ui} screen: {file}", "// Page '{ui}' ({file})"],
    "message": ["// Message shown to the user: {ui}", "// User-facing text: '{ui}' ({file})"],
}


def template_text(req):
    rng = random.Random(req["id"])
    t, c, s = req["target"], req["controls"], req["required_surface"]
    w = " on the %s screen" % req["context_phrase"] if req["context_phrase"] else ""
    if req["surface"] == "code_comment":
        ui = req["labels"]["term_mapping"][0]["ui_term"]
        return rng.choice(COMMENTS[t["kind"]]).format(ui=ui, table=t.get("table", ""), column=t.get("column", ""),
                                                    button=t.get("button", ""), handler=t.get("handler", ""), file=t["file"])
    sent = rng.choice(SENT[t["kind"]][c["issue"]]).format(s=s, w=w if t["kind"] != "page" else "")
    detail = " ".join(rng.sample(EXTRA, 2))
    if req["surface"] == "email":
        return "Subject: %s\n\n%s %s %s %s {{NAME}}" % (rng.choice(SUBJECTS).format(s=s), rng.choice(OPEN), sent,
                                                          " ".join(rng.sample(EXTRA, 2)), rng.choice(CLOSE))
    return ("Title: %s\nDescription: %s %s\nSteps to reproduce:\n1. Open the system.\n2. Go to %s.\nExpected: it works as before.\n"
            "Actual: %s" % (rng.choice(SUBJECTS).format(s=s), sent, detail, req["context_phrase"] or s, ISSUES[t["kind"]][c["issue"]]))


# ----------------------------------------------------------------- http backends
def post_json(url, headers, body, retries, timeout=180):
    data = json.dumps(body).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (408, 429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("network error: %s" % e)


def call_anthropic(prompt, a):
    key = os.environ.get(a.api_key_env or "ANTHROPIC_API_KEY", "")
    d = post_json(a.endpoint or "https://api.anthropic.com/v1/messages",
                  {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                  {"model": a.model or "claude-sonnet-5-5", "max_tokens": a.max_tokens, "temperature": a.temperature,
                   "messages": [{"role": "user", "content": prompt}]}, a.max_retries)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


def call_openai(prompt, a):
    key = os.environ.get(a.api_key_env or "OPENAI_API_KEY", "")
    base = (a.endpoint or "https://api.openai.com/v1").rstrip("/")
    d = post_json(base + "/chat/completions", {"Authorization": "Bearer " + key, "content-type": "application/json"},
                  {"model": a.model, "max_tokens": a.max_tokens, "temperature": a.temperature,
                   "messages": [{"role": "user", "content": prompt}]}, a.max_retries)
    return d["choices"][0]["message"]["content"]


def clean_output(text):
    text = re.sub(r"^```[a-z]*\s*$", "", text.strip(), flags=re.M).strip()
    return text


def cmd_generate(a):
    reqs = list(SC.read_jsonl(a.requests))
    done = set()
    if a.resume and os.path.exists(a.out):
        done = {r["id"] for r in SC.read_jsonl(a.out)}
    todo = [r for r in reqs if r["id"] not in done]
    imported = {}
    if a.backend == "import":
        imported = {r["id"]: r["text"] for r in SC.read_jsonl(a.import_file)}
    if a.backend in ("anthropic", "openai") and a.backend == "openai" and not a.model:
        print("--model is required for the openai backend")
        return 2

    def one(r):
        try:
            if a.backend == "template":
                text, src = template_text(r), "template"
            elif a.backend == "import":
                text, src = imported[r["id"]], "import"
            else:
                text = (call_anthropic if a.backend == "anthropic" else call_openai)(r["prompt"], a)
                src = "llm"
                if a.sleep:
                    time.sleep(a.sleep)
            return r, clean_output(text), src, None
        except Exception as e:                       # keep going; the record is written with its error
            return r, "", a.backend, str(e)[:300]

    out = open(a.out, "a" if a.resume else "w", encoding="utf-8")
    ok = bad = 0
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = [ex.submit(one, r) for r in todo]
        for f in as_completed(futs):
            r, text, src, err = f.result()
            rec = {k: r[k] for k in ("id", "task", "surface", "term_key", "target", "required_surface", "context_phrase",
                                    "forbidden_terms", "allowed_keys", "controls", "labels")}
            rec.update({"generated": text, "source": src, "backend": a.backend, "model": a.model or "", "error": err})
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            ok, bad = (ok + 1, bad) if not err else (ok, bad + 1)
    out.close()
    print("generated=%d errors=%d skipped(resume)=%d -> %s" % (ok, bad, len(done), a.out))
    return 0


# ----------------------------------------------------------------- contrastive pairs
MIN_TEMPLATES = ["Which field stores the {x} value?", "What is the database column for the {x}?", "Where is the {x} saved?",
                 "In which table and field is the {x} kept?", "Which column does the {x} map to?"]
CTX_TEMPLATES = ["On the {c} screen, which field stores the {x} value?", "In {c}, what is the database column for the {x}?",
                 "Where is the {x} saved when you use {c}?", "In which table and field is the {x} kept on the {c} page?"]


def field_items(kn):
    seen, items = set(), []
    for f in kn.rel["fields"]:
        if not f["table"]:
            continue
        key = TC.norm_key(f["label"], kn.aliases)
        k = (key, f["column"], f["table"])
        if not key or k in seen:
            continue
        seen.add(k)
        items.append({"key": key, "label": f["label"], "tokens": TC.norm_tokens(f["label"], kn.aliases), "column": f["column"],
                      "table": f["table"], "file": f["file"], "title": kn.title(f["file"]), "source": f["source"]})
    return items


def cmd_contrast(a):
    kn = SC.Knowledge(a.knowledge)
    items = field_items(kn)
    rng = random.Random(a.seed)
    cands = []
    by_table = defaultdict(list)
    for it in items:
        by_table[it["table"]].append(it)
    meanings = defaultdict(set)                                 # label -> distinct (column, table) over the whole system
    for it in items:
        meanings[it["key"]].add((it["column"], it["table"]))
    for table, group in by_table.items():                       # minimal pairs
        for x, y in itertools.combinations(group, 2):
            A, B = set(x["tokens"]), set(y["tokens"])
            if x["column"] == y["column"] or A == B:
                continue
            if len(meanings[x["key"]]) > 1 or len(meanings[y["key"]]) > 1:
                continue                                        # an ambiguous label needs a context pair, not a bare question
            if (len(A - B) == 1 and len(B - A) == 1 and A & B) or (len(A) == 1 and len(B) == 1):
                for ti in rng.sample(range(len(MIN_TEMPLATES)), min(a.templates_per_pair, len(MIN_TEMPLATES))):
                    cands.append(("minimal_pair", x, y, ti))
    by_label = defaultdict(list)
    for it in items:
        by_label[it["key"]].append(it)
    for key, group in by_label.items():                         # context pairs: same label, different column/table
        for x, y in itertools.combinations(group, 2):
            if (x["column"], x["table"]) != (y["column"], y["table"]) and x["title"] and y["title"] and x["title"] != y["title"]:
                for ti in rng.sample(range(len(CTX_TEMPLATES)), min(a.templates_per_pair, len(CTX_TEMPLATES))):
                    cands.append(("context_pair", x, y, ti))
    rng.shuffle(cands)
    cands = cands[:a.max_pairs]
    start = a.start_id
    if a.append and os.path.exists(a.out):
        start += sum(1 for r in SC.read_jsonl(a.out) if r.get("task") == "contrastive_term_linking")
    rows = []
    for n, (ptype, x, y, ti) in enumerate(cands, start):
        if ptype == "minimal_pair":
            fx, fy = x["label"].lower(), y["label"].lower()
            qa, qb = MIN_TEMPLATES[ti].format(x=fx), MIN_TEMPLATES[ti].format(x=fy)
            outs = [{"field": x["column"], "table": x["table"]}, {"field": y["column"], "table": y["table"]}]
            dist, ctxs = [fx, fy], []
        else:
            fx = x["label"].lower()
            qa, qb = (CTX_TEMPLATES[ti].format(x=fx, c=x["title"]), CTX_TEMPLATES[ti].format(x=fx, c=y["title"]))
            outs = [{"field": x["column"], "table": x["table"], "page": x["file"]},
                    {"field": y["column"], "table": y["table"], "page": y["file"]}]
            dist, ctxs = [x["title"], y["title"]], [x["title"], y["title"]]
        rows.append({"id": "contrast_%06d" % n, "task": "contrastive_term_linking", "pair_id": "contrast_%06d" % n,
                     "pair_type": ptype, "template": ti,
                     "examples": [{"input": qa, "output": outs[0]}, {"input": qb, "output": outs[1]}],
                     "distinguishing_terms": dist, "contexts": ctxs, "label_form": fx if ptype == "context_pair" else "",
                     "evidence": [{"label": x["label"], "file": x["file"], "source": x["source"]},
                                  {"label": y["label"], "file": y["file"], "source": y["source"]}],
                     "source": "deterministic"})
    SC.write_jsonl(a.out, rows, append=a.append)
    print("pairs=%d (minimal=%d context=%d) -> %s" % (len(rows), sum(r["pair_type"] == "minimal_pair" for r in rows),
                                                     sum(r["pair_type"] == "context_pair" for r in rows), a.out))
    return 0


# ----------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--knowledge", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--surfaces", default="email,ticket,code_comment")
    p.add_argument("--per-surface", type=int, default=1, help="variations per (target, surface)")
    p.add_argument("--kinds", default="", help="only terms of these kinds, e.g. field,page")
    p.add_argument("--min-evidence", type=int, default=1)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_plan)

    g = sub.add_parser("generate")
    g.add_argument("--requests", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--backend", choices=["template", "anthropic", "openai", "import"], default="template")
    g.add_argument("--model", default="")
    g.add_argument("--endpoint", default="", help="API URL (anthropic: full messages URL; openai: base URL incl. /v1)")
    g.add_argument("--api-key-env", default="")
    g.add_argument("--import-file", help="JSONL {id, text} for --backend import")
    g.add_argument("--temperature", type=float, default=0.9)
    g.add_argument("--max-tokens", type=int, default=600)
    g.add_argument("--workers", type=int, default=1)
    g.add_argument("--sleep", type=float, default=0.0, help="seconds to wait after each call")
    g.add_argument("--max-retries", type=int, default=3)
    g.add_argument("--resume", action="store_true", help="append and skip ids already in --out")
    g.set_defaults(fn=cmd_generate)

    c = sub.add_parser("contrast")
    c.add_argument("--knowledge", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--append", action="store_true")
    c.add_argument("--max-pairs", type=int, default=1000)
    c.add_argument("--templates-per-pair", type=int, default=2)
    c.add_argument("--start-id", type=int, default=1)
    c.add_argument("--seed", type=int, default=7)
    c.set_defaults(fn=cmd_contrast)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
