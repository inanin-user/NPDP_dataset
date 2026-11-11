# Synthetic data pipeline (glossary-conditioned + contrastive)

```
static_analysis/           run_static_analysis.bat  -> CSVs + glossary/ (term_cards.jsonl, term_index.json, 01_glossary.md)
synthetic_pipeline/
  02_knowledge/build_knowledge.py     CSVs + term cards -> glossary.json, glossary.md, relationships.json
  03_synthetic/synth_common.py        shared: knowledge loader, term detector, targets, labels, renderer
  03_synthetic/build_synthetic.py     plan | generate | contrast   -> requests.jsonl, generated_raw.jsonl
  03_synthetic/validate_synthetic.py  generated_raw.jsonl -> validated.jsonl, rejected.jsonl, validation_report.json, pretrain.jsonl
  run_synthetic.bat                   all steps (template backend by default)
```
`terms_common.py` and `build_glossary.py` (in static_analysis/) must be importable: the scripts look in their own folder,
`..`, `../01_glossary` and `../../static_analysis`. If you reorganise the folders, copy those two files next to the scripts.

## Principle: labels are never written by the LLM
`derive_targets()` / `build_labels()` compute every label (term -> column -> table -> page) from glossary.json +
relationships.json. The LLM writes only the natural-language surface. The validator re-derives the labels independently and
rejects any record whose text or labels disagree with the knowledge.

## Glossary-conditioned generation  (`plan` -> `generate`)
One request per (term TARGET x surface x variation). A target is ONE concrete meaning: field `mld_gold` of `mld_entry` on page X,
a button with a found handler, a page, a message. A field target exists only if its table is certain. For ambiguous terms
(`Status` -> two columns) the screen title is mandatory in the text, and targets without a title are skipped.
Each request carries: the exact surface form that must appear, a context phrase, forbidden confusable terms (from the term
cards), variation controls (persona, tone, language level, length, situation), a prompt, and the labels.
Surfaces: `email`, `ticket`, `code_comment` (= the "3 varied sentences per term" of 03_email_examples).
Emails/tickets use business wording only (no column/table/file names); code comments must name the column/table/button/file.

Backends of `generate`: `template` (offline, deterministic, low diversity - for tests and as a baseline), `anthropic`,
`openai` (any OpenAI-compatible server: vLLM, Ollama, ...), `import` (responses produced elsewhere: JSONL `{id, text}`).
Keys: ANTHROPIC_API_KEY / OPENAI_API_KEY (or `--api-key-env`). `--workers N`, `--resume`, retries with backoff on 429/5xx.
Anthropic default model is `claude-sonnet-5-5`; pass `--model` to change it.

## Contrastive generation  (`contrast`, no LLM)
- `minimal_pair`: sibling fields of one table whose labels differ by one word (Gold / Silver). Same question template, only the
  distinguishing term changes. Labels that are ambiguous system-wide (`Status`) are never used in a bare question.
- `context_pair`: same label, different page -> different column/table; the questions differ only in the screen title.
Format = your proposal plus `pair_type`, `distinguishing_terms`, `evidence`:
```
{"task":"contrastive_term_linking","pair_id":"contrast_000001","pair_type":"minimal_pair",
 "examples":[{"input":"Which field stores the gold value?","output":{"field":"mld_gold","table":"mld_entry"}},
             {"input":"Which field stores the silver value?","output":{"field":"mld_silver","table":"mld_entry"}}], ...}
```

## Validation (`validate_synthetic.py`) - see its docstring for every reject reason
Format, meta-chatter, length, required phrase, context phrase, forbidden terms, technical-name leakage into emails/tickets,
PII / real-looking identifiers, confusable-term contamination, label re-derivation, near-duplicates (word-trigram Jaccard),
pair checks (contrastive answers, same skeleton, label really maps to the answer, no ambiguous bare label).
`validation_report.json` lists reject reasons and terms with fewer than `--min-per-term` accepted records.

## Pre-training text
`--export-pretrain pretrain.jsonl` writes `{"text": ...}`: email/ticket/comment followed by the deterministic block
`Term mapping / Related fields / Related tables / Related php files / Entropy`; pairs as Question/Answer lines.

## Things to decide / watch
- Hold out terms (not just records) for evaluation: remove their cards from the training plan, keep them in the test set.
- Template-backend records have `source: "template"`: down-weight or exclude them from the final mix.
- Every document repeats the same label block, so a model can learn the block format quickly; keep the real-email share
  high and vary the LLM prompts (`--per-surface`, `--seed`) rather than regenerating the same targets.
- Nothing here reads database records: facts are structure only (pages, labels, columns, tables).

## Ollama (local small models, e.g. qwen2.5:7b-instruct)
Ollama exposes two APIs on port 11434:

| base URL | what it is | use it with |
|---|---|---|
| `http://localhost:11434` | server root (status page, no API) | `--backend ollama` (default endpoint) |
| `http://localhost:11434/api` | native API prefix (`/api/chat`, `/api/generate`) | `--backend ollama` (suffix is accepted and stripped) |
| `http://localhost:11434/v1` | OpenAI-compatible layer (`/v1/chat/completions`) | `--backend openai --endpoint http://localhost:11434/v1 --model ...` |

Prefer `--backend ollama`: the native API lets the script set the context window per request (`--num-ctx`, default 8192), keep the
model loaded (`--keep-alive`) and send `think=false` (`--no-think`); the OpenAI-compatible layer cannot set the context window,
and Ollama may silently fall back to a small default (check your version) - long prompts would be truncated without an error.

    ollama pull qwen2.5:7b-instruct
    python build_synthetic.py generate --requests requests.jsonl --out generated_raw.jsonl --backend ollama ^
           --model qwen2.5:7b-instruct --num-ctx 8192 --temperature 0.8 --workers 2

`--workers` only helps if the server runs requests in parallel (OLLAMA_NUM_PARALLEL); otherwise keep 1.
A system prompt is sent with every request (built-in `SYSTEM_PROMPT`, or `--system-file`).

## Why the model writes ONLY the user text
A generic "return JSON with resolved_entities, relationships, evidence, ..." instruction makes the LLM write the labels, and a 7B model
will invent plausible-looking ones. Here the LLM writes `user_input` only; `resolved_entities / relationships / evidence / ambiguity /
expected_decision` are derived from the knowledge and attached by `validate_synthetic.py --export-structured structured.jsonl`.

## Ambiguous examples (insufficient context)
`plan --ambiguous` adds `ambiguous_term_resolution` requests for terms that mean different things on different screens
(`Status`: wsm_stage on one page, mld_status on another). The text must NOT name any candidate screen; the label is
`expected_decision: ask_clarification` + candidates + a clarifying question ("Which screen do you mean: 'A' or 'B'?").
The validator rejects texts that contain a candidate screen title (`context_resolves_ambiguity`).
