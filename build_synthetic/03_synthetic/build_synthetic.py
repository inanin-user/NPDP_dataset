#!/usr/bin/env python3
"""build_synthetic.py - glossary-conditioned and contrastive synthetic data, grounded in the extracted knowledge.

  plan      knowledge  -> requests.jsonl   one request per (term target x surface x variation): prompt + facts + labels
  generate  requests   -> generated_raw.jsonl   backends: template (offline) | anthropic | openai | ollama | llamacpp |
                                                import (responses produced elsewhere)
  contrast  knowledge  -> generated_raw.jsonl   contrastive pairs; template backend = built-in wording, LLM backend =
                                                the LLM writes a bank of question SKELETONS ({X} = term, {C} = screen)
                                                that the script fills, so a pair stays parallel and labels deterministic
  server-cmd                                   print a llama-server launch command (-ngl, -np slots, KV cache type)

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
  python build_synthetic.py generate --requests requests.jsonl --out generated_raw.jsonl --backend ollama \\
         --model qwen2.5:7b-instruct --num-ctx 8192          # endpoint defaults to http://localhost:11434
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
import threading
import time
import zlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import diversity as DV
import synth_common as SC
import terms_common as TC
from llm_backends import LLM, LLMError

# ----------------------------------------------------------------- variation controls
PERSONAS = ["a merchandiser", "a production planner", "a sales coordinator", "a factory supervisor",
            "a quality controller", "a purchasing officer", "a new employee who is still learning the system"]
TONES = ["polite and formal", "short and direct", "urgent but polite", "casual", "apologetic"]
LEVELS = ["fluent business English", "clear English with one or two small non-native phrasing slips",
          "simple English with short sentences"]
LENGTHS = {"short": "2-3 sentences", "medium": "4-6 sentences"}
ISSUES = {
    "field": {"find": "cannot find where to enter this field", "wrong": "notices that the value of this field looks wrong",
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

SYSTEM_PROMPT = """You write ONE piece of synthetic business text for training a software-support language model.
You receive a task, one phrase that must appear, and a few facts.
Rules:
1. Output ONLY the requested text in the requested format. No JSON, no explanations, no preface, no closing remarks.
2. Use only the facts you are given. Never invent or mention PHP files, database tables, columns, functions, screens or
   relationships that are not in the facts.
3. Do not assume that two similarly named fields are the same thing.
4. If the task does not name a screen, do not add one: keep the uncertainty of the writer.
5. Write like a real colleague: natural wording, no template phrases such as "Dear valued customer".
"""


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
    elif k == "ambiguous":
        f = ["The word '%s' is used on more than one screen of the system, for different data." % ui,
             "The writer does not say which screen; the reader cannot tell from the text."]
    else:
        f = ["The system shows the message '%s' to the user." % ui]
    return f


STATIC_RULES = ("- Use business wording only: never mention database tables, columns, PHP files, SQL or code.\n"
                "- Use placeholders for identifiers: worksheet numbers as PS-XXXX, PO numbers as {{PO_NO}}, names as {{NAME}}. "
                "No real names, e-mail addresses or phone numbers.\n"
                "- Use only the facts given in the request; do not invent screens, fields or relationships.\n")
STATIC_HEAD = {
    "email": ("Write ONE realistic business email to IT support, written by a business user of the internal web system of a "
              "jewelry-manufacturing company.\nOutput format: first line 'Subject: ...', a blank line, then the body. "
              "No markdown, no preface.\nRules for every text:\n" + STATIC_RULES),
    "ticket": ("Write ONE IT support ticket raised by a business user (not a developer) of the internal web system of a "
               "jewelry-manufacturing company.\nOutput format, exactly these headings: Title: / Description: / Steps to reproduce: / "
               "Expected: / Actual:\nNo markdown, no preface.\nRules for every text:\n" + STATIC_RULES),
    "code_comment": ("Write ONE PHP code comment (1-3 lines, every line starting with //) that a developer puts above the code "
                     "behind a screen element of a jewelry-production web application.\nOutput only the comment lines: no code, "
                     "no markdown, no explanation.\n"),
}


def build_prompt(req, card):
    """Cache-friendly layout: everything identical for all requests of a surface comes FIRST, the request-specific block
    (persona, phrase, facts ...) comes LAST, so llama.cpp can re-use the KV cache of the static prefix (prompt_version 2)."""
    c, t, surf = req["controls"], req["target"], req["required_surface"]
    ctx = req.get("context_phrase")
    forb = req["forbidden_terms"]
    rules = ['The text MUST contain this exact phrase, spelled exactly like this: "%s"' % surf]
    if ctx:
        rules.append('It MUST also mention the screen name "%s" exactly once, so the reader knows which screen is meant.' % ctx)
    if req["task"] == "ambiguous_term_resolution":
        rules.append("Do NOT say which screen, page or function you mean; a reader must not be able to tell.")
    if forb:
        rules.append("Do NOT mention any of these other terms: %s." % ", ".join('"%s"' % x for x in forb))
    facts = "\n".join("- " + x for x in req["facts"])
    block = "\n=== REQUEST ===\n"
    if req["surface"] == "code_comment":
        names = ["table `%s`" % t["table"], "column `%s`" % t["column"]] if t["kind"] == "field" else \
                (["button `%s`" % t["button"]] if t["kind"] == "button" else ["file `%s`" % t["file"]])
        return (STATIC_HEAD["code_comment"] + block + "- %s\n- It must also name exactly: %s.\nFacts (do not copy literally):\n%s\n"
                % ("\n- ".join(rules), ", ".join(names), facts))
    issue = ISSUES[c.get("issue_kind", t["kind"])][c["issue"]]
    return (STATIC_HEAD[req["surface"]] + block +
            "- Writer: %s. Tone: %s. Language: %s. Length: %s.\n- Situation: the writer %s.\n- %s\n"
            "Facts you may rely on (do not copy literally):\n%s\n"
            % (c["persona"], c["tone"], c["level"], LENGTHS[c["length"]], issue, "\n- ".join(rules), facts))


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
                                                   surface != "code_comment" and       # a comment names column + table itself
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
                    req["prompt_version"] = 2
                    reqs.append(req)
    if a.ambiguous:                       # insufficient-context examples: the right behaviour is to ask which screen
        for key, card in sorted(kn.cards.items()):
            amb = SC.derive_ambiguous(card, kn)
            if not amb or card["evidence_count"] < a.min_evidence:
                continue
            for surface in [x for x in surfaces if x in ("email", "ticket")]:
                for i in range(a.per_surface):
                    rng = random.Random("%s|%s|ambiguous|%s|%d" % (a.seed, key, surface, i))
                    ik = amb["issue_kind"]
                    controls = {"persona": rng.choice(PERSONAS), "tone": rng.choice(TONES), "level": rng.choice(LEVELS),
                                "length": rng.choice(sorted(LENGTHS)), "issue": rng.choice(sorted(ISSUES[ik])), "issue_kind": ik}
                    required = rng.choice(card["variants"] or [card["term"]])
                    titles = [c["page_title"] for c in amb["candidates"]]
                    forb = titles + [kn.cards[k]["term"] for k in card["confusable_with"] if k in kn.cards][:3]
                    forb = [f for f in forb if not SC.phrase_in_text(f, required, kn.aliases)]
                    n += 1
                    req = {"id": "amb_%06d" % n, "task": "ambiguous_term_resolution", "surface": surface, "term_key": key,
                           "target": amb, "required_surface": required, "context_phrase": "", "forbidden_terms": forb,
                           "allowed_keys": [key], "controls": controls, "facts": facts_for(card, amb),
                           "labels": SC.build_ambiguous_labels(card, amb, required, kn)}
                    req["prompt"] = build_prompt(req, card)
                    reqs.append(req)
    if a.limit:
        reqs = random.Random(a.seed).sample(reqs, min(a.limit, len(reqs)))
    SC.write_jsonl(a.out, reqs)
    by = defaultdict(int)
    for r in reqs:
        by[r["task"].split("_")[0] + "/" + r["surface"]] += 1
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
    ik = c.get("issue_kind", t["kind"])
    sent = rng.choice(SENT[ik][c["issue"]]).format(s=s, w=w if ik != "page" else "")
    detail = " ".join(rng.sample(EXTRA, 2))
    if req["surface"] == "email":
        return "Subject: %s\n\n%s %s %s %s {{NAME}}" % (rng.choice(SUBJECTS).format(s=s), rng.choice(OPEN), sent,
                                                          " ".join(rng.sample(EXTRA, 2)), rng.choice(CLOSE))
    return ("Title: %s\nDescription: %s %s\nSteps to reproduce:\n1. Open the system.\n2. Go to %s.\nExpected: it works as before.\n"
            "Actual: %s" % (rng.choice(SUBJECTS).format(s=s), sent, detail, req["context_phrase"] or s, ISSUES[ik][c["issue"]]))


# ----------------------------------------------------------------- generate (uses the backend)
def clean_output(text):
    text = re.sub(r"^```[a-z]*\s*$", "", text.strip(), flags=re.M).strip()
    return text


LLM_BACKENDS = ("anthropic", "openai", "ollama", "llamacpp")


def make_llm(a, system):
    return LLM(a.backend, model=a.model, endpoint=a.endpoint, api_key_env=a.api_key_env, system=system,
               temperature=a.temperature, top_p=a.top_p, top_k=a.top_k, min_p=a.min_p, repeat_penalty=a.repeat_penalty,
               max_tokens=a.max_tokens, max_retries=a.max_retries, timeout=a.timeout, num_ctx=a.num_ctx,
               keep_alive=a.keep_alive, no_think=a.no_think, cache_prompt=not a.no_cache_prompt)


def cmd_generate(a):
    reqs = list(SC.read_jsonl(a.requests))
    done = set()
    if a.resume and os.path.exists(a.out):
        done = {r["id"] for r in SC.read_jsonl(a.out)}
    todo = [r for r in reqs if r["id"] not in done]
    imported = {}
    if a.backend == "import":
        imported = {r["id"]: r["text"] for r in SC.read_jsonl(a.import_file)}
    if a.backend in ("openai", "ollama") and not a.model:
        print("--model is required for the %s backend" % a.backend)
        return 2

    llm, srv = None, {}
    if a.backend in LLM_BACKENDS:
        llm = make_llm(a, open(a.system_file, encoding="utf-8").read() if a.system_file else SYSTEM_PROMPT)
        try:
            srv = llm.ready(a.wait_server)
        except LLMError as e:
            print("ERROR:", e)
            return 2
    workers = a.workers or srv.get("slots") or 1
    if a.backend == "llamacpp" and srv:
        print("llama-server: %s slot(s), context per slot %s tokens" % (srv.get("slots") or "?", srv.get("n_ctx_slot") or "?"))
        if srv.get("slots") and workers > srv["slots"]:
            print("note: %d workers > %d slots; using %d (extra requests would only queue on the server)" % (workers, srv["slots"], srv["slots"]))
            workers = srv["slots"]
        need = max((len(r["prompt"]) for r in todo), default=0) // 3 + len(SYSTEM_PROMPT) // 3 + a.max_tokens
        if srv.get("n_ctx_slot") and need > srv["n_ctx_slot"]:
            print("WARNING: the longest prompt needs about %d tokens but a slot holds %d; raise -c or lower -np" % (need, srv["n_ctx_slot"]))

    tracker, stats, lock = DV.OpeningTracker(), Counter(), threading.Lock()
    tps = []

    def one(r):
        meta = {}
        try:
            if a.backend == "template":
                return r, template_text(r), "template", None, meta
            if a.backend == "import":
                return r, imported[r["id"]], "import", None, meta
            seed0 = (zlib.crc32(r["id"].encode("utf-8")) ^ (a.seed * 2654435761)) & 0x7FFFFFFF
            hinted = a.diversify and r["surface"] != "code_comment"
            attempts = 1 + (a.diversity_retries if (a.max_opening_repeat and hinted) else 0)
            text, key = "", ""
            for attempt in range(attempts):
                rng = random.Random(seed0 + attempt)
                prompt = r["prompt"] + (DV.variation_hint(rng, tracker, attempt) if hinted else "")
                temp = a.temperature + (rng.random() * 2 - 1) * a.temp_jitter + 0.15 * attempt
                text, info = llm.complete(prompt, seed=seed0 + attempt, temperature=max(0.1, min(1.5, temp)))
                text = clean_output(text)
                key = DV.opening_key(text, r["surface"], mask=[r["required_surface"], r.get("context_phrase") or ""]) if hinted else ""
                with lock:
                    for k in ("prompt_n", "cache_n", "predicted_n"):
                        stats[k] += info.get(k) or 0
                    if info.get("predicted_per_second"):
                        tps.append(info["predicted_per_second"])
                if a.max_opening_repeat and hinted and tracker.count(key) >= a.max_opening_repeat and attempt < attempts - 1:
                    with lock:
                        stats["resampled"] += 1
                    continue
                break
            tracker.add(key)
            meta = {"attempts": attempt + 1, "seed": seed0 + attempt}
            if a.sleep:
                time.sleep(a.sleep)
            return r, text, "llm", None, meta
        except Exception as e:                       # keep going; the record is written with its error
            return r, "", a.backend, str(e)[:300], meta

    out = open(a.out, "a" if a.resume else "w", encoding="utf-8")
    ok = bad = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(one, r) for r in todo]
        for f in as_completed(futs):
            r, text, src, err, meta = f.result()
            rec = {k: r[k] for k in ("id", "task", "surface", "term_key", "target", "required_surface", "context_phrase",
                                    "forbidden_terms", "allowed_keys", "controls", "labels")}
            rec.update({"generated": text, "source": src, "backend": a.backend, "model": a.model or "", "error": err, "gen": meta})
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            ok, bad = (ok + 1, bad) if not err else (ok, bad + 1)
    out.close()
    print("generated=%d errors=%d skipped(resume)=%d workers=%d -> %s" % (ok, bad, len(done), workers, a.out))
    if a.backend in LLM_BACKENDS and ok:
        total_prompt = stats["prompt_n"] + stats["cache_n"]
        if total_prompt and stats["cache_n"]:
            print("KV cache: %d of %d prompt tokens re-used (%.0f%%)" % (stats["cache_n"], total_prompt, 100.0 * stats["cache_n"] / total_prompt))
        if tps:
            print("generation speed: %.1f tokens/s per request (mean of %d)" % (sum(tps) / len(tps), len(tps)))
        print("opening diversity: %d distinct openings in %d texts; re-sampled %d" %
              (len(tracker.counts), sum(tracker.counts.values()), stats["resampled"]))
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


CONTRAST_SYSTEM = ("You write short question templates for a dataset that links business terms to database fields. "
                   "Output only the questions, one per line, with no numbering and no explanations.")


def skeleton_prompt(kind, n, style, recent):
    """Static instructions first, batch-specific part last (KV-cache friendly)."""
    p = ("Write %d DIFFERENT questions in which a person asks which database field (and table) stores or updates a business value.\n"
         "Rules:\n- Each question contains the placeholder {X} exactly once, where the business term goes. Never write a real term.\n" % n)
    if kind == "context":
        p += "- Each question also contains the placeholder {C} exactly once, where the name of the screen goes.\n"
    p += ("- Vary the wording a lot; 6-28 words each.\n"
          "- Vary the intent: which field stores it / which column and table keep it / where it is saved when the form is submitted / "
          "which database field changes when it is edited on screen / where the screen reads the value from.\n"
          "- No quotation marks, numbering, markdown or explanations. One question per line.\n")
    p += "\n=== BATCH ===\nStyle of this batch: %s.\n" % style
    if recent:
        p += "Do not repeat or closely imitate these: %s\n" % " | ".join(recent)
    return p


def fill_bank(llm, bank, kind, target, banned, rng, a):
    calls = stale = 0
    while bank.size(kind) < target and calls < a.max_skeleton_calls and stale < 3:
        calls += 1
        before = bank.size(kind)
        prompt = skeleton_prompt(kind, a.skeleton_batch, rng.choice(DV.SKELETON_STYLES), bank.items[kind][-8:])
        try:
            text, _ = llm.complete(prompt, seed=rng.randrange(1 << 30), temperature=max(a.temperature, 1.0), max_tokens=700)
        except LLMError as e:
            print("WARNING: skeleton request failed: %s" % e)
            break
        for line in text.splitlines():
            s = DV.clean_skeleton(line)
            if DV.skeleton_ok(s, kind, banned):
                bank.add(kind, s)
        stale = stale + 1 if bank.size(kind) == before else 0      # stop when the LLM only repeats itself
    print("skeleton bank [%s]: %d skeletons from %d request(s)" % (kind, bank.size(kind), calls))
    return calls


def cmd_contrast(a):
    kn = SC.Knowledge(a.knowledge)
    items = field_items(kn)
    rng = random.Random(a.seed)
    base = []                                                   # (pair_type, x, y)
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
                base.append(("minimal_pair", x, y))
    by_label = defaultdict(list)
    for it in items:
        by_label[it["key"]].append(it)
    for key, group in by_label.items():                         # context pairs: same label, different column/table
        for x, y in itertools.combinations(group, 2):
            if (x["column"], x["table"]) != (y["column"], y["table"]) and x["title"] and y["title"] and x["title"] != y["title"]:
                base.append(("context_pair", x, y))
    cands = [c for c in base for _ in range(max(1, a.templates_per_pair))]
    rng.shuffle(cands)
    cands = cands[:a.max_pairs]

    bank, source, model = DV.SkeletonBank(), "deterministic", ""
    if a.backend != "template":
        llm = make_llm(a, CONTRAST_SYSTEM)
        try:
            llm.ready(a.wait_server)
        except LLMError as e:
            print("ERROR:", e)
            return 2
        if a.bank_file and os.path.exists(a.bank_file):
            bank = DV.SkeletonBank.from_json(json.load(open(a.bank_file, encoding="utf-8")))
        stop = {t for it in items for t in it["tokens"]} | {t for it in items for t in TC.norm_tokens(it["title"], kn.aliases)}
        for kind, ptype in (("minimal", "minimal_pair"), ("context", "context_pair")):
            if any(c[0] == ptype for c in cands):
                fill_bank(llm, bank, kind, a.skeletons, stop, rng, a)
        if a.bank_file:
            json.dump(bank.to_json(), open(a.bank_file, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        source, model = "llm-skeleton", a.model
    start = a.start_id
    if a.append and os.path.exists(a.out):
        start += sum(1 for r in SC.read_jsonl(a.out) if r.get("task") == "contrastive_term_linking")

    rows, fallback, used_for_pair = [], 0, defaultdict(set)
    for n, (ptype, x, y) in enumerate(cands, start):
        kind = "minimal" if ptype == "minimal_pair" else "context"
        pair_key = (ptype, x["column"], x["table"], x["file"], y["column"], y["table"], y["file"])
        if bank.size(kind) >= 3:
            sk, ti, src = bank.pick(kind, rng, exclude=used_for_pair[pair_key]), -1, source
            if sk is None:
                continue                                        # this field pair already has every skeleton
            used_for_pair[pair_key].add(sk)
        else:                                                   # no LLM skeletons (template backend or the LLM failed)
            pool = MIN_TEMPLATES if kind == "minimal" else CTX_TEMPLATES
            ti = rng.randrange(len(pool))
            sk, src = pool[ti].replace("{x}", "{X}").replace("{c}", "{C}"), "deterministic"
            fallback += a.backend != "template"
        cap = lambda q: q[:1].upper() + q[1:]
        if ptype == "minimal_pair":
            fx, fy = x["label"].lower(), y["label"].lower()
            qa, qb = cap(sk.replace("{X}", fx)), cap(sk.replace("{X}", fy))
            outs = [{"field": x["column"], "table": x["table"]}, {"field": y["column"], "table": y["table"]}]
            dist, ctxs = [fx, fy], []
        else:
            fx = x["label"].lower()
            qa, qb = cap(sk.replace("{X}", fx).replace("{C}", x["title"])), cap(sk.replace("{X}", fx).replace("{C}", y["title"]))
            outs = [{"field": x["column"], "table": x["table"], "page": x["file"]},
                    {"field": y["column"], "table": y["table"], "page": y["file"]}]
            dist, ctxs = [x["title"], y["title"]], [x["title"], y["title"]]
        rows.append({"id": "contrast_%06d" % n, "task": "contrastive_term_linking", "pair_id": "contrast_%06d" % n,
                     "pair_type": ptype, "template": ti, "skeleton": sk,
                     "examples": [{"input": qa, "output": outs[0]}, {"input": qb, "output": outs[1]}],
                     "distinguishing_terms": dist, "contexts": ctxs, "label_form": fx if ptype == "context_pair" else "",
                     "evidence": [{"label": x["label"], "file": x["file"], "source": x["source"]},
                                  {"label": y["label"], "file": y["file"], "source": y["source"]}],
                     "source": src, "backend": a.backend, "model": model})
    SC.write_jsonl(a.out, rows, append=a.append)
    used = len({r["skeleton"] for r in rows})
    print("pairs=%d (minimal=%d context=%d) distinct question skeletons=%d%s -> %s" %
          (len(rows), sum(r["pair_type"] == "minimal_pair" for r in rows), sum(r["pair_type"] == "context_pair" for r in rows),
           used, " (fell back to built-in templates for %d)" % fallback if fallback else "", a.out))
    return 0


def cmd_server_cmd(a):
    ctx_total = a.ctx_per_slot * a.slots
    parts = ["llama-server", "-m", '"%s"' % a.model_path, "-c", str(ctx_total), "-np", str(a.slots), "-ngl", str(a.ngl),
             "--flash-attn", "on" if a.kv_quant else "auto", "--host", a.host, "--port", str(a.port), "--jinja"]
    if a.kv_quant:
        parts += ["-ctk", a.kv_quant, "-ctv", a.kv_quant]
    print(" ".join(parts))
    print("\n# %d slots x %d tokens = -c %d (the context is split evenly between the slots)." % (a.slots, a.ctx_per_slot, ctx_total))
    print("# Then:  python build_synthetic.py generate ... --backend llamacpp --endpoint http://%s:%d   (workers = slots by default)" % (a.host, a.port))
    print("# Older builds may want bare -fa instead of '--flash-attn on|auto'; run  llama-server --help  to check your build.")
    return 0


# ----------------------------------------------------------------- cli
def add_llm_args(p):
    p.add_argument("--backend", choices=["template", "anthropic", "openai", "ollama", "llamacpp", "import"], default="template")
    p.add_argument("--model", default="", help="model name (required for openai / ollama; optional label for llamacpp)")
    p.add_argument("--endpoint", default="", help="anthropic: full messages URL | openai: base URL incl. /v1 | ollama: "
                   "http://localhost:11434 (/api, /v1 accepted) | llamacpp: http://127.0.0.1:8080 (/v1 accepted)")
    p.add_argument("--api-key-env", default="")
    p.add_argument("--system-file", help="text file replacing the built-in system prompt (generate)")
    p.add_argument("--temperature", type=float, default=0.9)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--top-k", type=int, default=40, help="ollama / llamacpp")
    p.add_argument("--min-p", type=float, default=0.05, help="ollama / llamacpp")
    p.add_argument("--repeat-penalty", type=float, default=1.1, help="ollama / llamacpp: discourages loops and stock phrases")
    p.add_argument("--max-tokens", type=int, default=600)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=600, help="seconds per request")
    p.add_argument("--wait-server", type=int, default=120, help="llamacpp: seconds to wait for /health (model loading)")
    p.add_argument("--no-cache-prompt", action="store_true", help="llamacpp: do not re-use the KV cache of the common prompt prefix")
    p.add_argument("--num-ctx", type=int, default=8192, help="ollama: context window tokens per request")
    p.add_argument("--keep-alive", default="30m", help="ollama: keep the model loaded between requests")
    p.add_argument("--no-think", action="store_true", help="ollama: send think=false (reasoning models such as qwen3)")
    p.add_argument("--seed", type=int, default=7)


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
    p.add_argument("--ambiguous", action="store_true",
                   help="also plan insufficient-context examples (term used on several screens; expected decision: ask which screen)")
    p.add_argument("--seed", type=int, default=7)
    p.set_defaults(fn=cmd_plan)

    g = sub.add_parser("generate", help="requests.jsonl -> generated_raw.jsonl (uses the backend)")
    g.add_argument("--requests", required=True)
    g.add_argument("--out", required=True)
    add_llm_args(g)
    g.add_argument("--import-file", help="JSONL {id, text} for --backend import")
    g.add_argument("--workers", type=int, default=0, help="parallel requests; 0 = auto (llamacpp: number of server slots, else 1)")
    g.add_argument("--sleep", type=float, default=0.0, help="seconds to wait after each call")
    g.add_argument("--resume", action="store_true", help="append and skip ids already in --out")
    g.add_argument("--no-diversify", dest="diversify", action="store_false", help="do not append the variation block to prompts")
    g.add_argument("--temp-jitter", type=float, default=0.1, help="random +/- temperature per request")
    g.add_argument("--max-opening-repeat", type=int, default=3,
                   help="re-sample a text whose first words were already generated this many times (0 = off)")
    g.add_argument("--diversity-retries", type=int, default=2)
    g.set_defaults(fn=cmd_generate)

    c = sub.add_parser("contrast", help="contrastive pairs; with an LLM backend the question wording is generated as skeletons")
    c.add_argument("--knowledge", required=True)
    c.add_argument("--out", required=True)
    add_llm_args(c)
    c.add_argument("--append", action="store_true")
    c.add_argument("--max-pairs", type=int, default=1000)
    c.add_argument("--templates-per-pair", type=int, default=2, help="how many pairs (different wording) per field pair")
    c.add_argument("--skeletons", type=int, default=80, help="skeletons wanted per pair type (LLM backends)")
    c.add_argument("--skeleton-batch", type=int, default=12, help="questions requested per LLM call")
    c.add_argument("--max-skeleton-calls", type=int, default=40)
    c.add_argument("--bank-file", help="JSON file to load / save the skeleton bank (reuse across runs)")
    c.add_argument("--start-id", type=int, default=1)
    c.set_defaults(fn=cmd_contrast)

    s = sub.add_parser("server-cmd", help="print a llama-server launch command for your GPU")
    s.add_argument("--model-path", required=True, help="path of the .gguf file")
    s.add_argument("--slots", type=int, default=4, help="parallel slots (-np) = parallel generation workers")
    s.add_argument("--ctx-per-slot", type=int, default=4096)
    s.add_argument("--ngl", default="99", help="layers on the GPU (99 = all)")
    s.add_argument("--kv-quant", default="", choices=["", "q8_0", "q4_0"], help="quantised KV cache (needs flash attention)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_server_cmd)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
