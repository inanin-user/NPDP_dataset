# Static-analysis label generator for legacy PHP

## Run
    run_static_analysis.bat D:\NPS_backup_20261029\Test D:\Dataset_20261020\labels_v4 D:\NPS_backup_20261029\redesign_sql\redesign.sql

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
| session_bindings.csv | Every `allFormSession(table, 'INIT'|'', extra)` call: resolved table, how it was resolved, mode, extra fields, parent it was inherited from |
| session_scope.csv | Effective session table per file: `own` (page calls the binder) or `inherited` from the parent page that INITs it |
| display_calls.csv | `displayField` / `displayText` calls: visible UI term (label cell), column, permission flag `$_SESSION[scope][column]`, value expression, bound table |
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

## Session keys <-> ONE table (allFormSession)
`allFormSession($tablename, $init, $extraField)` creates one `$_SESSION[<column>]` per column of ONE table
(`'INIT'`: empty values; any other value: copy of `$_POST`). So inside a page every `$_SESSION['column']` belongs to the table passed to
that call, not to every table that happens to have a column of that name.

- **own**: the page calls the binder with a literal (`'psht_mas'`) or a `$var` assigned a literal earlier in the same file.
- **inherited**: the page has no resolvable call (no call, or `$tablename` is only set by the parent) and is linked / posted to /
  redirected to / included by a page whose call is `'INIT'`. Only INIT calls are inherited; the page's own call always wins;
  `--bind-depth N` (default 1) sets how many hops are followed.
- Several INIT parents with different tables: up to 3 tables are listed (`inherited_ambiguous`); more than 3 -> `inherited_too_many`
  and no table is claimed.
- The file that DEFINES the binder is never given an inherited table.
- `table_source` in `session_map.csv` / `display_calls.csv`: `bound`, `inherited`, `inherited_ambiguous`, `extra_field` (key listed in
  the 3rd argument), `not_in_bound_table` (e.g. `username`, `login.level`: not a column of the page's table), `dynamic_key`
  (`$_SESSION[$value]`), `schema_unique` / `schema_ambiguous` (file has no binding; hint only), `*_unverified` (no --schema given).
- Plain `$_SESSION` reads inside `displayField(...)`/`displayText(...)` are no longer repeated as `use` rows in session_map.csv;
  they are in display_calls.csv.
- Different helper name or argument order: `binder_config.json`, e.g. `{"allFormSession": {"table": 0, "init": 1, "extra": 2}}`
  (0-based positions; pass with `--binder-config`, or put the file next to the .bat).
- Limits: only literal / simply assigned table names are resolved (`$_GET['t']`, concatenations -> `unresolved_expression`);
  a table chosen at run time in the parent cannot be known statically.

## Links that wrap columns (link_actions.csv) - lowers the email -> page ambiguity
List pages often show a column value as a link to the edit page: `<td><a href="mld_mas.php?action=modify&id=<?= $row['mld_id'] ?>"><?= $row['mld_gold'] ?></a></td>`.
`extract_form_actions.py` now records every `<a href="x.php...">` that sits in a table cell or inside a form:

| column | meaning |
|---|---|
| context | `table_cell`, or `in_form` (link inside a <form> but not in a table); links elsewhere (menus) are skipped unless `--all-links` |
| header_label / row_label | the column header above the cell (`Gold`), and/or the label cell on the left in the same row |
| link_columns / key_columns | columns shown inside the link (`mld_gold`) / record key used in the URL (`mld_id`) |
| target_file, action_mode | resolved target page and the literal `action=` value (`modify`, `add`, `view`) |
| target_mode_found / _line / _tables / _columns_written / _calls | where the target handles `$_REQUEST['action'] == 'modify'` and what that block does |

`link_actions.jsonl` has the same rows plus a summary sentence. `field_labels.csv` gained `link_target` and `link_mode`
(a header label such as `Gold` on the list page knows which page it opens), and `term_bridge.jsonl` carries them too.
The record key in the URL is NOT counted as a column displayed in the cell.
Chain for an email saying "gold lock": file_titles.csv (page title) -> field_labels.csv (`Gold` -> mld_gold, link_target mld_mas.php, mode modify)
-> field_labels.csv on mld_mas.php (`Gold` -> mld_gold, table mld_entry) -> link_actions.csv (what the modify branch does).
Limits: `javascript:` links without a `.php` path and links whose text is built by a function are skipped or have empty `link_columns`;
only `action=<literal>` is understood as a mode; a mode handled with `switch/case` (not `if (... == 'x')`) shows `target_mode_found=no`.