# Task Spec: Business-Terminology Learning Curriculum

## Goal
Give the LLM 
(1) solid general English (English dataset)
(2) the vocabulary of this business (jewelry production worksheets) (production worksheet, bill of material, model making sheet, etc...)
(3) the ability to connect a term in an email to the same term in code and in the database (to be figure)

## Recommended base
Start from a pretrained open base model. Training from scratch cannot learn
English from a small private corpus. Use this corpus for continued pretraining
(Stage 2-3) and instruction tuning (Stage 4-5).

## Stages
| Stage | Capability | Data | Format |
|---|---|---|---|
| 1 | General English | Public corpora (not in this folder) | plain text |
| 2 | Business English | Your real emails, anonymized (see 03_email_examples.md for the target style) | plain text |
| 3 | Source-code vocabulary | PHP and SQL from the application, with comments added | code + prose |
| 4 | Term linking (email <-> code <-> DB) | 01_glossary.md, 04_code_examples.md | Q/A pairs |
| 5 | IT-support reasoning | 02_term_test.jsonl and real tickets | trajectories |

## Standard-term rules (apply to every file)
1. Each business concept has ONE canonical name. Aliases are listed in the glossary and always mapped back to it.
2. Every term has: definition, aliases, where it appears (email wording / code identifier / DB column), and a confidence tag.
3. Confidence tags: `CODE` (proven by code you provided), `INFERRED` (guessed from names, needs owner confirmation), `UNKNOWN`.
4. Never train on INFERRED definitions until a business owner confirms them.
5. No credentials, real customer names, or real PO numbers. Use placeholders such as `{{USERNAME}}`, `CUST-001`.
6. Keep numbers and state values consistent. Example: `status = 1` means the worksheet is confirmed and displayed red (proven in prod_main.php). Do not write the opposite anywhere.

## Term-understanding test (for IT support)
- Use 02_term_test.jsonl. Each item has a `type`: `definition`, `alias`, `email_to_column`, `state_to_ui`, `code_reading`.
- Score: exact match for state values and column names; rubric match for definitions.
- Report coverage = terms answered correctly / total terms in 01_glossary.md.
- Hold out 20% of items and never train on them.

## Expansion procedure (to reach many terms fast)
1. Extract every column name from the schema (130+ in psht_mas). Add each to the glossary with a tag.
2. Extract identifiers from PHP (function names, config keys, table names).
3. Extract nouns that repeat in real emails. Cluster aliases.
4. Ask a business owner to confirm all INFERRED entries in one session.
5. For each confirmed term, write 3 varied sentences: one email, one code comment, one support ticket.
