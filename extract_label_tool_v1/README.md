# Static-analysis label generator for legacy PHP

## Run
    run_static_analysis.bat D:\AppServ\htdocs\Test D:\labels D:\schema\psht_mas.md

Needs Python 3.8+ only (no pip packages). Read-only on your source.
Schedule nightly (Windows Task Scheduler), adjust paths:

    schtasks /create /tn "PHP static labels" /tr "C:\tools\static_analysis\run_static_analysis.bat" /sc daily /st 02:00

## Outputs
| File | Answers |
|---|---|
| file_to_file.csv | Which file includes / links / redirects / posts to which file (edge_type) |
| function_calls.csv | Which file calls a function defined in which other file |
| table_to_file.csv | Which file reads/writes which table, in which function, which columns written |
| column_to_file.csv | Which file mentions/writes a schema column (e.g. who writes psht_mas.status) |
| session_map.csv | $_SESSION key <-> local variable: alias, derived, write, use (co-occurring vars) |
| labels.jsonl | One JSON record per file, combining all of the above (+ included_by, linked_from, tables_via_calls) |

`sample_output/` shows the result on a tiny test project built from your snippets.

## Session map kinds
- alias:   `$username = $_SESSION['username'];`
- derived: `$level3a = $_SESSION['login']['level'] >= 3;`
- write:   `$_SESSION['k'] = $var;`
- use:     any other read; local_vars = variables on the same line (co-occurrence, NOT proven data flow)

## Known limits (heuristic, regex-based)
- No real data flow: reassigned variables, `$$var`, arrays of keys, `extract($_SESSION)` are missed.
- Dynamic SQL (table name in a variable) is reported as the variable text; SQL built across many statements may lose columns.
- Method calls are matched by name only (no class resolution); `ambiguous=yes` means several files define the name.
- Heredoc/nowdoc strings and unusual quoting can confuse function-body detection.
- Tables not in --schema are still reported (in_schema=no); stopword/context filters reduce prose false positives but do not eliminate them.
- Spot-check 20 random rows before using labels for training. Wrong labels poison the dataset.

## Upgrade path
For exact results replace the regex layer with an AST parser (nikic/PHP-Parser) or a Phan/Psalm data-flow analysis, keeping the same CSV/JSONL output format.
