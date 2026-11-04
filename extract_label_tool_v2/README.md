# Static-analysis label generator for legacy PHP

## Run
    run_static_analysis.bat D:\NPS_backup_20261029\Test D:\Dataset_20261020\labels_v3 D:\NPS_backup_20261029\redesign_sql\redesign.sql

Runs two steps: `extract_labels.py` (relationships) then `extract_form_actions.py` (forms/buttons).
Both use `file_filter.py` to skip backup/draft/dated copies.

Needs Python 3.8+ only (no pip packages). Read-only on your source.
Schedule nightly (Windows Task Scheduler), adjust paths:

    schtasks /create /tn "PHP static labels" /tr "C:\tools\static_analysis\run_static_analysis.bat" /sc daily /st 02:00

## Outputs
| File | Answers |
|---|---|
| file_to_file.csv | Which file includes / links / redirects / posts to which file (edge_type) |
| function_calls.csv | Which file calls a function defined in which other file |
| table_to_file.csv | Which file reads/writes which table, in which function, which columns written. One-line queries are aggregated per (table, operation) with `lines` like `100;101`; every query built over several statements gets its OWN row (`statement_kind`, `exec_line`) |
| column_to_file.csv | Which file mentions/writes a schema column (e.g. who writes psht_mas.status) |
| session_map.csv | $_SESSION key <-> local variable: alias, derived, write, use (co-occurring vars) |
| excluded_files.csv | Files skipped by the filter, with the reason (audit trail) |
| form_actions.csv | Every form and button: which button posts to which file, method, query params, hidden fields, onclick targets |
| button_handlers.csv | For each named button: where the target file tests it (`isset($_POST['btnX'])`) and what that block does (tables, columns written, calls, redirects, session keys); `not_found` rows = no handler located |
| ui_actions.jsonl | One JSON record per button with handlers and a template-generated summary sentence (for Q/A generation) |
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

## File filter (file_filter.py)
Skipped by default (case-insensitive, on the file name):
- stem ends with a date: `prod_get-20220317.php`, `prod_get_2022-03-17.php`, `prod_get_17032022.php` (optionally + time)
- contains `__`, contains whitespace (`prod_mas - copy (2).php`), contains `bak`

    python file_filter.py --self-test
    python file_filter.py --test prod_get.php "prod_draft_mas - __ (2).php"
    python file_filter.py --src D:\AppServ\htdocs\Test --csv excluded.csv
    python file_filter.py --write-default-config filter_rules.json   # then edit: substrings, extra_regex, allow ...

Any script can reuse it: `from file_filter import FileFilter; FileFilter(config_path=...).is_excluded(name)`.
Scripts accept `--filter-config rules.json` and `--no-filter`.

## Form/button limits
- Handler is found by the button NAME (`$_POST['name']`, `$_REQUEST`, `$_GET`, or `$name` variable). Buttons without a name (JS-only) are listed but have no handler; their `onclick` text is recorded.
- JS functions called by `onsubmit`/`onclick` (e.g. `formCheck`) are recorded by name only, not analysed.
- Forms built from several concatenated strings, or an action taken from a variable, show `dynamic`/`no_php_target`.
- `?action=modify` style modes (`$_REQUEST['action'] == 'modify'`) are recorded as query params but their server branches are not yet traced.
- If a handler block calls functions, tables are reported under `tables_via_calls` (name-based match).

## Queries built over several statements (query_builder.py)
Pattern: `$query = "INSERT INTO t "; $fields = "("; ... $fields = $fields . "$column,"; ... $query = $query . $fields . " VALUES " . $values; mysql_query($query);`

- The query variable is followed from its start statement to the first execution (`mysql_query`, `mysqli_query`, `->query` ...). `lines` = start line, `exec_line` = execution line.
- Columns are collected from the "column variables" (before `VALUES` for INSERT, before `WHERE` for UPDATE, any appended `col = ...` fragment) in source order.
- `$column` appends are resolved by the enclosing `if ($column == "name")` or `in_array($column, array(...))`.
- If the column can only be known at run time, the marker `<dynamic:POST[$column]>` (from `array_key_exists($column, $_POST)` / `isset($_POST[$column])`) or `<dynamic:$column>` is written. To resolve it, intersect the schema columns with the input names of the form that posts to this file.
- Column variables built BEFORE the query statement (e.g. `$f2 = "a,b"; mysql_query("INSERT INTO t (" . $f2 . ") VALUES ...")`) are found by a bounded look-back (8000 chars, up to the last reset of the variable).
- `statement_kind`: `single_line` (aggregated), `built`, `built_unresolved` (no column found), `*_no_exec` (query start found but no execution found; columns are then collected up to the end of the function or the next query start).
- Comment text (including trailing `// ...` comments) is blanked before analysis, so commented-out code never produces rows.

Limits: no real data flow; queries stored in arrays, passed to other functions before execution, or built with `sprintf`/`implode` are reported as `built_unresolved`; a variable reused for two queries is split at the next `$var = "INSERT|UPDATE|..."` assignment.
