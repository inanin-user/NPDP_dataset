# Code Examples with Standard Terms

Goal: the model links code identifiers to business terms.

## 1. UI color rule (prod_main.php)
```php
// Confirmed worksheet (status = 1) is shown in red
if ($wsm_row['status'] == 1)
    echo "<a href='prod_mas.php?action=modify&id=" . $wsm_row['wsm_id'] . "' style='color:red;'>";
else
    echo "<a href='prod_mas.php?action=modify&id=" . $wsm_row['wsm_id'] . "'>";
```
**Terms**: `status` = confirmation state; `wsm_id` = Worksheet ID; red = Confirmed / in production.
**Q/A**: *Which value makes the record red?* -> `status = 1`.

## 2. Immediate confirmation (prod_get.php, before cut-off)
```php
mysql_query("UPDATE `psht_mas` SET
    `wsm_checkby2` = '$_SESSION[username]',
    `wsm_checkdate2` = '$time',
    `status` = '1',
    `wsm_stage` = 'WIP',
    `wsm_stagedate` = '" . date("Y-m-d") . "',
    `wsm_step` = '4'
  WHERE `wsm_id` = $_REQUEST[id]");
```
**Terms**: checker 2, check date 2, Confirmed, Stage WIP, Step 4.
**Note for training**: this is legacy PHP (mysql_* is removed in PHP 7). The model should recognize it but recommend prepared statements for new code.

## 3. Deferred confirmation (after cut-off)
```php
$checked = $systemfunction->checkWSSendList($_REQUEST['id']);
if (!$checked) {
    $insert['wsl_ws_id']    = $_REQUEST['id'];
    $insert['wsl_wsno']     = $_POST['wsm_wsno'];
    $insert['wsl_date']     = $time;
    $insert['wsl_check_by'] = $_SESSION['username'];
    $insert['wsl_status']   = '0';   // queued
    $systemfunction->insertWSSendList($insert);
}
```
**Terms**: send list, queued (`wsl_status = 0`), cut-off.
**Gap**: the job that reads `wsl_status = 0` and performs the immediate-confirm UPDATE is not provided. Add its file path and source.

## 4. Diagnostic SQL (read-only first)
```sql
-- Worksheets queued but not confirmed
SELECT m.wsm_id, m.wsm_wsno, m.status, m.wsm_step, m.wsm_checkby2,
       s.wsl_status, s.wsl_date, s.wsl_check_by
FROM psht_mas m
JOIN worksheet_send_list s ON s.wsl_ws_id = m.wsm_id
WHERE m.wsm_wsno IN ('PS-PQL773','PS-PQL778','PS-PQL779','PS-PQL780')
  AND s.wsl_status = 0;
```

## 5. Fix pattern (only after backup and human approval)
```sql
-- Expected end state for a queued confirmation that the job missed
UPDATE psht_mas
SET status = 1, wsm_stage = 'WIP', wsm_stagedate = CURDATE(), wsm_step = 4,
    wsm_checkby2 = '{{CHECK_BY_FROM_SEND_LIST}}',
    wsm_checkdate2 = '{{WSL_DATE_FROM_SEND_LIST}}'
WHERE wsm_id IN ({{VERIFIED_IDS}});
```
**Terms**: the fix reproduces what btnCheck2 writes. The related `worksheet_send_list.wsl_status` update must follow the job's real behavior (UNKNOWN until job source is added).
