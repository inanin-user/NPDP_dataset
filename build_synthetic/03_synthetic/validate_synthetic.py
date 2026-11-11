#!/usr/bin/env python3
"""validate_synthetic.py - accept / reject generated records against the knowledge, then export pre-training text.

  generated_raw.jsonl  ->  validated.jsonl (+ "rendered" pre-training document)
                           rejected.jsonl  (+ "reasons")
                           validation_report.json
                           [--export-pretrain pretrain.jsonl]   {"text": ...} per accepted record
                           [--export-structured structured.jsonl]  task / user_input / resolved_entities / relationships /
                                                evidence / ambiguity / expected_decision (all but user_input from the knowledge)

Glossary-conditioned records (email / ticket / code_comment) are rejected when:
  generation_error | bad_format | code_fence | meta_chatter | too_short / too_long
  missing_required_surface | missing_context | forbidden_term:<x>
  technical_leak:<x>      a column / table / php name appears in an email or ticket (users never write those)
  missing_technical_name  a code comment lacks the column / table / button / file it must name
  confusable_term:<key>   a different glossary term that is confusable with the target appears in the text
  extra_term:<key>        (only with --strict-extra) any glossary term other than the target and its context
  pii:<kind> | real_identifier_shape | forbidden_regex
  label_mismatch | target_mismatch | unknown_term | unknown_target   (labels re-derived from the knowledge differ)
  context_resolves_ambiguity   (ambiguous_term_resolution) a candidate screen title appears, so the text is no longer ambiguous
  unsupported_surface     ambiguous examples are emails / tickets only
  near_duplicate          same term/target/surface and >= --dedupe-threshold word-trigram Jaccard
Contrastive pairs are rejected when the two answers are equal, a question lacks its own distinguishing term or contains
the other's, the two questions are not the same template apart from that term, a label does not really map to the answer
in relationships.json, or a bare label is ambiguous (maps to several columns/tables) - or duplicates an accepted pair.

Usage:
  python validate_synthetic.py --knowledge ../02_knowledge --in generated_raw.jsonl --out-dir . --export-pretrain pretrain.jsonl
Python 3.8+, standard library only.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

import synth_common as SC
import terms_common as TC

META_RE = re.compile(r"\b(as an ai|language model|i cannot (?:assist|help)|i can't (?:assist|help)|here is|here's (?:the|a|an)|"
                     r"sure[,!.]|certainly[,!.]|of course[,!.])", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")
ID_SHAPE = re.compile(r"\b[A-Z]{1,4}-[A-Z]{2,5}\d{3,}\b")           # PS-XXXX placeholders are fine; PS-PQL773 is not
BOUNDS = {"email": (20, 220), "ticket": (20, 260), "code_comment": (4, 80)}


def has_phone(text):
    for m in PHONE_RE.finditer(text):
        s = m.group(0)
        if len(re.sub(r"\D", "", s)) >= 9 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.strip()):
            return True
    return False


def shingles(text, n=3):
    w = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(1, len(w) - n + 1))}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


# ----------------------------------------------------------------- glossary-conditioned records
def check_generation(rec, kn, tech_rx, strict_extra, forbid_rx):
    if rec.get("error") or not isinstance(rec.get("generated"), str) or not rec["generated"].strip():
        return ["generation_error"]
    reasons, text, surface = [], rec["generated"].strip(), rec.get("surface", "")
    if surface not in BOUNDS:
        return ["unknown_surface"]
    if "```" in text:
        reasons.append("code_fence")
    if META_RE.search(text):
        reasons.append("meta_chatter")
    if surface == "email" and not (re.match(r"Subject:\s*\S", text) and "\n" in text):
        reasons.append("bad_format")
    if surface == "ticket" and not all(h in text for h in ("Title:", "Description:")):
        reasons.append("bad_format")
    if surface == "code_comment" and not all(l.strip().startswith(("//", "#", "/*", "*")) for l in text.splitlines() if l.strip()):
        reasons.append("bad_format")
    n = len(text.split())
    lo, hi = BOUNDS[surface]
    if n < lo:
        reasons.append("too_short")
    if n > hi:
        reasons.append("too_long")

    al = kn.aliases
    if not SC.phrase_in_text(rec["required_surface"], text, al):
        reasons.append("missing_required_surface")
    if rec.get("context_phrase") and not SC.phrase_in_text(rec["context_phrase"], text, al):
        reasons.append("missing_context")
    for f in rec.get("forbidden_terms", []):
        if SC.phrase_in_text(f, text, al):
            reasons.append("forbidden_term:" + f)

    t = rec.get("target", {})
    amb = rec.get("task") == "ambiguous_term_resolution"
    if amb:
        if surface == "code_comment":
            reasons.append("unsupported_surface")
        for c in t.get("candidates", []):
            if SC.phrase_in_text(c.get("page_title", ""), text, al):
                reasons.append("context_resolves_ambiguity")
    if surface in ("email", "ticket"):
        m = tech_rx.search(text) if tech_rx else None
        if m:
            reasons.append("technical_leak:" + m.group(0))
    else:
        need = {"field": [t.get("column"), t.get("table")], "button": [t.get("button")]}.get(t.get("kind"), [os.path.basename(t.get("file", ""))])
        if any(x and not re.search(r"(?<![A-Za-z0-9_])" + re.escape(x) + r"(?![A-Za-z0-9_])", text, re.I) for x in need):
            reasons.append("missing_technical_name")

    if EMAIL_RE.search(text):
        reasons.append("pii:email")
    if has_phone(text):
        reasons.append("pii:phone")
    if re.search(r"https?://|www\.", text):
        reasons.append("pii:url")
    if ID_SHAPE.search(text):
        reasons.append("real_identifier_shape")
    for rx in forbid_rx:
        if rx.search(text):
            reasons.append("forbidden_regex")

    card = kn.cards.get(rec.get("term_key"))
    if not card:
        reasons.append("unknown_term")
    else:
        allowed = set(rec.get("allowed_keys", [])) | {rec["term_key"]}
        found = {d["key"] for d in kn.detector.detect(text)} - allowed
        for k in sorted(found & set(card.get("confusable_with", []))):
            reasons.append("confusable_term:" + k)
        if strict_extra:
            for k in sorted(found - set(card.get("confusable_with", []))):
                reasons.append("extra_term:" + k)
        if amb:
            target = SC.derive_ambiguous(card, kn)
            expected = lambda: SC.build_ambiguous_labels(card, target, rec["required_surface"], kn)
        else:
            target = next((x for x in SC.derive_targets(card, kn) if x["id"] == t.get("id")), None)
            expected = lambda: SC.build_labels(card, target, rec["required_surface"], kn)
        if target is None or target.get("id") != t.get("id"):
            reasons.append("unknown_target")
        else:
            if target != t:
                reasons.append("target_mismatch")
            if expected() != rec.get("labels"):
                reasons.append("label_mismatch")
    return reasons


# ----------------------------------------------------------------- contrastive pairs
def contains(tokens, phrase):
    n = len(phrase)
    return bool(phrase) and any(tokens[i:i + n] == phrase for i in range(len(tokens) - n + 1))


def check_pair(rec, kn, meanings):
    ex = rec.get("examples") or []
    if len(ex) != 2 or any(not isinstance(e.get("input"), str) or not isinstance(e.get("output"), dict) for e in ex):
        return ["bad_structure"]
    d = rec.get("distinguishing_terms") or []
    if len(d) != 2:
        return ["bad_structure"]
    reasons, al = [], kn.aliases
    o = [e["output"] for e in ex]
    if (o[0].get("field"), o[0].get("table")) == (o[1].get("field"), o[1].get("table")):
        reasons.append("pair_not_contrastive")
    tok = [TC.norm_tokens(e["input"], al) for e in ex]
    dt = [TC.norm_tokens(x, al) for x in d]
    for i in (0, 1):
        if not contains(tok[i], dt[i]):
            reasons.append("missing_distinguishing_term")
        if contains(tok[i], dt[1 - i]) and not contains(dt[i], dt[1 - i]):
            reasons.append("other_term_present")
    if [SC.remove_phrase(tok[i], dt[i]) for i in (0, 1)][0] != [SC.remove_phrase(tok[i], dt[i]) for i in (0, 1)][1]:
        reasons.append("skeleton_differs")

    pt = rec.get("pair_type")
    for i in (0, 1):
        want = (o[i].get("field"), o[i].get("table"))
        if pt == "minimal_pair":
            ok = [f for f in kn.rel["fields"] if (f["column"], f["table"]) == want and TC.norm_key(f["label"], al) == " ".join(dt[i])]
            if not ok:
                reasons.append("label_does_not_map_to_answer")
            if len(meanings.get(" ".join(dt[i]), ())) > 1:
                reasons.append("ambiguous_label_without_context")
        elif pt == "context_pair":
            form = TC.norm_key(rec.get("label_form", ""), al)
            ok = [f for f in kn.rel["fields"] if (f["column"], f["table"], f["file"]) == (want[0], want[1], o[i].get("page"))
                  and TC.norm_key(f["label"], al) == form and kn.title(f["file"]) == d[i]]
            if not ok:
                reasons.append("label_does_not_map_to_answer")
            if not contains(tok[i], TC.norm_tokens(rec.get("label_form", ""), al)):
                reasons.append("missing_label")
        else:
            reasons.append("unknown_pair_type")
            break
    return list(dict.fromkeys(reasons))


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knowledge", required=True)
    ap.add_argument("--in", dest="inp", required=True, help="generated_raw.jsonl")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--export-pretrain", help="write accepted records as {\"text\": ...} JSONL")
    ap.add_argument("--export-structured", help="write accepted records in the structured JSON schema")
    ap.add_argument("--dedupe-threshold", type=float, default=0.85)
    ap.add_argument("--strict-extra", action="store_true", help="reject any glossary term other than target and context")
    ap.add_argument("--forbid-regex", action="append", default=[], help="extra regex that must not appear (repeatable)")
    ap.add_argument("--min-per-term", type=int, default=3, help="coverage report: terms with fewer accepted records")
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)

    kn = SC.Knowledge(a.knowledge)
    tech_rx = kn.tech_regex()
    forbid_rx = [re.compile(x, re.I) for x in a.forbid_regex]
    meanings = defaultdict(set)
    for f in kn.rel["fields"]:
        if f["table"]:
            meanings[TC.norm_key(f["label"], kn.aliases)].add((f["column"], f["table"]))

    accepted, rejected, seen_shingles, seen_pairs = [], [], defaultdict(list), set()
    reasons_count, by_task, by_source, per_term, terms_seen = Counter(), Counter(), Counter(), Counter(), set()
    n_in = 0
    for rec in SC.read_jsonl(a.inp):
        n_in += 1
        task = rec.get("task")
        if task == "contrastive_term_linking":
            reasons = check_pair(rec, kn, meanings)
            sig = tuple(sorted(" ".join(TC.norm_tokens(e["input"], kn.aliases)) for e in rec.get("examples", []) if isinstance(e.get("input"), str)))
            if not reasons and sig in seen_pairs:
                reasons = ["near_duplicate"]
            elif not reasons:
                seen_pairs.add(sig)
        elif task in ("glossary_conditioned_generation", "ambiguous_term_resolution"):
            terms_seen.add(rec.get("term_key"))
            reasons = check_generation(rec, kn, tech_rx, a.strict_extra, forbid_rx)
            if not reasons:
                group = (rec["term_key"], rec["target"]["id"], rec["surface"])
                sh = shingles(rec["generated"])
                if any(jaccard(sh, s) >= a.dedupe_threshold for s in seen_shingles[group]):
                    reasons = ["near_duplicate"]
                else:
                    seen_shingles[group].append(sh)
        else:
            reasons = ["unknown_task"]
        if reasons:
            rejected.append({**rec, "reasons": reasons})
            for r in reasons:
                reasons_count[r.split(":")[0]] += 1
        else:
            rec = dict(rec)
            rec["rendered"] = SC.render_document(rec)
            accepted.append(rec)
            by_task[task] += 1
            by_source[rec.get("source", "")] += 1
            if task in ("glossary_conditioned_generation", "ambiguous_term_resolution"):
                per_term[rec["term_key"]] += 1

    SC.write_jsonl(os.path.join(a.out_dir, "validated.jsonl"), accepted)
    SC.write_jsonl(os.path.join(a.out_dir, "rejected.jsonl"), rejected)
    if a.export_pretrain:
        SC.write_jsonl(a.export_pretrain, [{"id": r["id"], "task": r["task"], "source": r.get("source", ""),
                                            "text": r["rendered"]} for r in accepted])
    if a.export_structured:
        SC.write_jsonl(a.export_structured, [SC.to_structured(r, kn) for r in accepted])
    report = {"input": n_in, "accepted": len(accepted), "rejected": len(rejected), "accepted_by_task": dict(by_task),
              "accepted_by_source": dict(by_source), "reject_reasons": dict(reasons_count.most_common()),
              "terms_seen": len(terms_seen), "terms_with_accepted": len(per_term),
              "terms_below_min": sorted(k for k in terms_seen if per_term[k] < a.min_per_term)[:200],
              "min_per_term": a.min_per_term}
    with open(os.path.join(a.out_dir, "validation_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: report[k] for k in ("input", "accepted", "rejected", "reject_reasons")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
