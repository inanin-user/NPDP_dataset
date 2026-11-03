# Business Glossary (Standard Terms)

Confidence: CODE = proven by supplied code; INFERRED = guessed from names, must be confirmed.

## A. Systems and modules
| Canonical term | Aliases | Definition | Where it appears | Conf. |
|---|---|---|---|---|
| NPS | - | One of two web applications; users confirm/send production worksheets here (expansion of the acronym: UNKNOWN) | curl_tool.md, tickets | CODE (name only) |
| NPDP | - | Second web application, served from /production/ (expansion: UNKNOWN) | curl_tool.md | CODE (name only) |
| Production Worksheet | PWS, WS, W/S#, worksheet | A work order record for producing one item; one row in `psht_mas` | `psht_mas`, prod_main.php | CODE |
| Worksheet number | WS no, wsno | Human-readable ID of a worksheet, e.g. `PS-PQL773` | `wsm_wsno` | CODE |
| Worksheet ID | record id | Internal auto-increment key | `wsm_id` | CODE |

## B. Workflow state (most important for support)
| Term | Field | Value | Meaning | UI effect | Written by | Conf. |
|---|---|---|---|---|---|---|
| Confirmed / in production | `status` | 1 | Worksheet confirmed and moved to production | Link shown in **red** | prod_get.php (btnCheck2) | CODE |
| Not confirmed | `status` | 0 | Default, not yet confirmed | Link in default color | table default | CODE |
| Stage WIP | `wsm_stage` | 'WIP' | Work in progress | - | prod_get.php | CODE |
| Step 4 | `wsm_step` | 4 | Step set on confirmation | - | prod_get.php | CODE |
| Checker 2 | `wsm_checkby2` | username | User who pressed "Confirmed" | shown in form | prod_get.php | CODE |
| Check date 2 | `wsm_checkdate2` | datetime | When it was confirmed | shown in form | prod_get.php | CODE |
| Stage date | `wsm_stagedate` | date | Date stage was last set | - | prod_get.php | CODE |

## C. Deferred approval (after cut-off)
| Term | Definition | Where | Conf. |
|---|---|---|---|
| Approval cut-off time | Daily time (config key `ws_approve_time_cut_off`). Confirming before it updates the worksheet immediately; after it, the request is queued | `Systemfunction::getConfig` | CODE |
| Send list | Queue table of worksheets waiting for next-day auto approval | `worksheet_send_list` | CODE |
| Send-list status 0 | Queued, not yet processed | `wsl_status = '0'` | CODE |
| Auto approve and send | A job at about 01:00 next day should apply the same UPDATE as an immediate confirm | message text in prod_get.php | CODE (job source file: UNKNOWN, must be added) |
| Check by (queued) | User who queued the confirmation | `wsl_check_by` | CODE |

## D. Order and customer terms
| Term | Aliases | Definition | Field | Conf. |
|---|---|---|---|---|
| Style number | style no | Product style identifier | `wsm_styleno` | INFERRED |
| Style revision | - | Revision of the style | `wsm_stylerevno` | INFERRED |
| Design number | - | Design identifier | `wsm_dsgnno` | INFERRED |
| Vendor code | vendor | Supplier/maker code | `wsm_vendcode` | INFERRED |
| Customer code | cust | Customer identifier | `wsm_custcode` | INFERRED |
| Customer PO | PO | Customer purchase order number | `wsm_custpo` | INFERRED |
| Customer due date | - | Date customer needs the goods | `wsm_custduedate` | INFERRED |
| Factory requested ship date | FTY req ship | Ship date requested of the factory | `wsm_ftyreqshipdate` | INFERRED |
| Order quantity | - | Pieces ordered | `wsm_orderqty` | INFERRED |
| Total shipped | - | Pieces shipped so far | `wsm_totalship` | INFERRED |
| Balance | - | Remaining quantity | `wsm_balance` | INFERRED |
| Country of origin | COO | Origin declaration | `wsm_coo` | INFERRED |
| Retailer | - | End retailer | `wsm_retailer` | INFERRED |
| Ship location | - | Destination | `wsm_shiplocation` | INFERRED |
| Hold / reject | - | Worksheet put on hold or rejected | `wsm_holdreject` | INFERRED |
| Revision number | rev | Revision counter of the worksheet | `wsm_revno`, `wsm_wsrevno` | INFERRED |
| Active flag | - | 1 = active record | `wsm_active` | INFERRED |

## E. Materials terms (jewelry)
| Term | Definition | Field | Conf. |
|---|---|---|---|
| Metal code / color | Metal type and plating color of the piece (up to 3 codes, 4 colors) | `wsm_metalcode*`, `wsm_metalcolor*` | INFERRED |
| Stamp | Marking applied to the piece | `wsm_stamp`, `wsm_charstamp`, `wsm_metalstamp` | INFERRED |
| Gold / silver / copper lock | Locked metal price and its date | `wsm_goldlock`, `wsm_goldlockdate`, etc. | INFERRED |
| Finished weight (gold/silver/platinum) | Final metal weight; "req" = required | `wsm_fingoldwt`, `wsm_reqfingoldwt`, ... | INFERRED |
| Total diamond / color stone weight | Stone weights | `wsm_totaldiamondwt`, `wsm_totalcolorstonewt` | INFERRED |
| On-hand stones | Stones already in stock | `wsm_onhandstones*` | INFERRED |
| FTY / CUST metal totals | Metal supplied by factory vs. by customer | `wsm_ftyttmetal*`, `wsm_custtmetal*` | INFERRED |

## F. IT and support vocabulary
| Term | Meaning |
|---|---|
| Ticket | A support request describing a problem |
| Root cause | The underlying reason, not the symptom |
| Symptom | What the user sees (e.g. record not red) |
| Reproduce | Trigger the problem on purpose to confirm it |
| Backup before update | Save affected rows before any UPDATE |
| Rollback / revert | Restore backed-up values |
| Verification | Checking via UI response and DB export that the fix worked |
| Handoff | Written record of steps taken, for the next engineer |
| Cron / scheduled job | Process that runs at a set time (e.g. 01:00) |
| Idempotent | Safe to run twice with the same result |

## G. Business email vocabulary
PO, ETA, ship date, due date, lead time, revision, hold, release, confirm, approve, please advise, kindly note, as per, attached, follow up, urgent, delay, shortage, rework, QC.
