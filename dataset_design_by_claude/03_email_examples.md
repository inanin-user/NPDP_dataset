# Business Email Examples (synthetic, anonymized)

Each example has the email, then the **term mapping** the model should learn.
Replace with real anonymized emails over time.

---
## Example 1: Support request
**Subject:** PWS not turning red after confirmation

Hi IT Support,

Our team confirmed several production worksheets in NPS on 15 Oct after 18:00. As per the process, they should have been approved automatically at 01:00 the next day, but the records are still not red. Examples: PS-PQL773, PS-PQL778. Kindly check and advise. This is urgent as the factory is waiting.

Thanks,
{{USER_NAME}}

**Term mapping**
| Email wording | Standard term | System meaning |
|---|---|---|
| PWS / production worksheets | Production Worksheet | rows in `psht_mas` |
| confirmed | Confirmed action | button `btnCheck2` |
| after 18:00 | past approval cut-off | `ws_approve_time_cut_off` |
| approved automatically at 01:00 | auto approve and send | job over `worksheet_send_list` |
| red | confirmed display state | `status = 1` |
| PS-PQL773 | Worksheet number | `wsm_wsno` |

---
## Example 2: Order follow-up
**Subject:** CUST-001 PO follow up for style ST-100

Dear {{VENDOR_CONTACT}},

Please confirm the ship date for PO {{PO_NO}}, style ST-100, quantity 500 pcs. The customer due date is 30 Nov. Kindly note the balance of 120 pcs is still outstanding.

Best regards,
{{SENDER}}

**Term mapping**
| Email wording | Standard term | Column |
|---|---|---|
| PO | Customer PO | `wsm_custpo` |
| style | Style number | `wsm_styleno` |
| quantity | Order quantity | `wsm_orderqty` |
| customer due date | Customer due date | `wsm_custduedate` |
| balance | Balance | `wsm_balance` |
| ship date | Factory requested ship date | `wsm_ftyreqshipdate` (INFERRED) |

---
## Example 3: Metal price
**Subject:** Gold price lock for worksheet PS-XXXX

Hi team, please lock the gold price at today's rate for this worksheet and update the lock date. Silver is not required.

**Term mapping**: gold price lock -> `wsm_goldlock`; lock date -> `wsm_goldlockdate`; silver not required -> `wsm_silverlock` stays 0 (INFERRED).

---
## Example 4: IT reply and handoff
**Subject:** RE: PWS not turning red after confirmation

Hi {{USER_NAME}},

We found that the confirmation was queued in the send list (status 0) and the 01:00 job did not process it. We have processed the affected worksheets manually and verified that they now show red. Root cause is under investigation.

**Term mapping**: send list -> `worksheet_send_list`; status 0 -> queued; verified -> UI response and DB export checked.
