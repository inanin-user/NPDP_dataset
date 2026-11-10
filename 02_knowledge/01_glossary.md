# Business Glossary (generated from static analysis)

_Generated 2026-11-09 from the CSVs in `/tmp/t/allout`. Structure comes from code; a definition appears only when a business owner confirmed it (`definitions_confirmed.csv`)._

Ambiguity: **low** = the term points to one page / column / button, **medium** = 2-3, **high** = 4+. Confidence: CODE = found in source, INFERRED = only weak evidence, CONFIRMED = definition confirmed by an owner.

## A. Pages / functions (page titles, headings, menu entries)

| Term | Aliases | Page title in | Menu link to | Ambiguity | Conf. | Definition |
|---|---|---|---|---|---|---|
| Metal lock data maintenance | — | misc/mld_main.php | — | low | CODE | — |
| Metal Lock Price Data Entry Form | — | misc/mld_mas.php | — | low | CODE | — |
| Shared | — | — | pro/shared.php | low | CODE | — |
| Back to menu | — | — | misc/menu.php | low | CODE | — |
| Detail | — | — | misc/mld_mas.php | low | CODE | — |

## B. Field labels (what the user sees next to a value)

| Term | Aliases | Column -> table | Seen in | Value links to | Notes | Ambiguity | Definition |
|---|---|---|---|---|---|---|---|
| Gold | gold lock, gold price lock | mld_gold -> mld_entry | misc/mld_main.php:8, misc/mld_mas.php:12 | misc/mld_mas.php (action=modify) | — | low | Gold lock price for the day used when pricing worksheets (CONFIRMED BY OWNER IN THIS TEST) |
| Status | — | mld_status -> mld_entry | misc/mld_mas.php:13 | — | — | medium | — |
| Status | — | wsm_stage -> psht_mas | pro/prod_mas.php:9, pro/prod_mas.php:13 | — | permission flag pro_pws.wsm_stage; read-only | medium | — |
| Lock Date | — | mld_date -> mld_entry | misc/mld_mas.php:15 | misc/calendar.php | — | low | — |
| Received Time | — | wsm_receivedbytime1 -> psht_mas | pro/prod_mas.php:9, pro/prod_mas.php:16 | — | permission flag pro_pws.wsm_receivedbytime1 | low | — |
| Silver | — | mld_silver -> mld_entry | misc/mld_main.php:8, misc/mld_mas.php:14 | — | — | low | — |
| Date | — | mld_date -> mld_entry | misc/mld_main.php:8 | — | — | low | — |

## C. Buttons

| Button label | Name | On page | Handled at | Tables | Columns written | Ambiguity |
|---|---|---|---|---|---|---|
| Save | btnSave | misc/mld_mas.php, pro/prod_mas.php | misc/mld_get.php:2 | mld_entry:UPDATE | mld_entry.mld_gold, mld_entry.mld_silver | low |
| Search | btnSearch | misc/mld_main.php | not found | — | — | low |

## D. Links that open another page

| Label / column header | From -> to | Ambiguity |
|---|---|---|
| Gold | misc/mld_main.php -> misc/mld_mas.php (action=modify) | low |
| Lock Date | misc/mld_mas.php -> misc/calendar.php | low |

## E. System messages users quote

_(none found)_

## F. Tables and the pages that use them

| Table | Read by | Written by | Columns written |
|---|---|---|---|
| mld_entry | misc/mld_main.php, misc/mld_mas.php | misc/mld_get.php(UPDATE) | mld_gold, mld_silver |

