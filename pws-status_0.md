## PHP function Module (System environment)
- Production -> Production Worksheet
- All following functions are from this module
- use `read` tool to 

## Data source in phpMyAdmin (System environment)
- records in table 'psht_mas' in database redesign
- table structure refer to psht_mas.md

## Tools in this module (System environment)
- systemfunction refer to systemfunction.md

### Function location (User Prompt)
- the condition for different color of ws record ``` path: D:\AppServ\htdocs\Test\pro\prod_main.php ```
- the initial trigger to turn ws record to red color or a POST request where id=123 to prod_get.php ``` path: D:\AppServ\htdocs\Test\pro\prod_mas.php ```
- the final trigger to turn ws record to red color or mysql query to UPDATE `psht_mas` 
							`status` to 1,
							`wsm_stage` to 'WIP', 
							`wsm_stagedate` to '" . date("Y-m-d") . "',
							`wsm_step` to 4
						 WHERE `wsm_id` = $_REQUEST[id]
                         ``` path: D:\AppServ\htdocs\Test\pro\prod_get.php ```



#### Related function (User Prompt)
```php
    // if pws record turn red from D:\AppServ\htdocs\Test\pro\prod_main.php
    if ($wsm_row['status'] == 1)
        echo "<a href='prod_mas.php?action=modify&id=" . $wsm_row['wsm_id'] . "' style='color:red;'>";
    else
        echo "<a href='prod_mas.php?action=modify&id=" . $wsm_row['wsm_id'] . "'>";

    // data source for checkby from D:\AppServ\htdocs\Test\pro\prod_mas.php
    <tr>
        <td>Checked by <?= $cocode ?>: &nbsp; </td>
        <td><?php
        
            if ($wsm_checkby2 == NULL){
                $sql = "SELECT * From `worksheet_send_list` where `wsl_ws_id` = {$_REQUEST['id']} AND `wsl_status` = 0";
                $data = mysql_query($sql);
                if ($row = mysql_fetch_array($data)) {
                    $wsm_checkby2 = $row['wsl_check_by'];
                    $wsm_checkdate2 = $row['wsl_date'];
                }
            }
        
            if (($_SESSION['login']['level'] >= $wsm_step || $level3a) && $wsm_checkby2 == NULL && $_SESSION['login']['level'] >= 2.9) {
            ?>
                <input type="submit" name="btnCheck2" value="Confirmed" style="width:100px;" <?= $_REQUEST['action'] == "add" ? "disabled" : "" ?>>
            <?php
            } else {
                echo $wsm_checkby2 . "&nbsp;&nbsp;&nbsp;&nbsp;" . $wsm_checkdate2;
            }
            ?>
        </td>
    </tr>

    // function btnCheck2 from D:\AppServ\htdocs\Test\pro\prod_get.php
    if (isset($_POST['btnCheck2'])) {
        include_once '../common/systemfunction.php';
        $systemfunction = new Systemfunction();
        $approval_cut_off_time = $systemfunction->getConfig('ws_approve_time_cut_off');
        $time = date("Y/m/d H:i:s");
        if (date("H:i:s") < $approval_cut_off_time) {
            mysql_query("UPDATE `psht_mas` SET 
                                `wsm_checkby2` = '$_SESSION[username]',
                                `wsm_checkdate2` = '$time',
                                `status` = '1',
                                `wsm_stage` = 'WIP', 
                                `wsm_stagedate` = '" . date("Y-m-d") . "',
                                `wsm_step` = '4'
                            WHERE `wsm_id` = $_REQUEST[id] ");
            echo '<script language = "JavaScript">window.location="prod_mas.php?action=modify&id=' . $_REQUEST['id'] . '";</script>';
        } else {
            $checked = $systemfunction->checkWSSendList($_REQUEST['id']);
            if ($checked) {
                echo '<script language = "JavaScript">alert("This W/S# already checked, system will approve and send it at next day 01:00 AM");</script>';
            } else {
                $insert['wsl_ws_id'] = $_REQUEST['id'];
                $insert['wsl_wsno'] = $_POST['wsm_wsno'];
                $insert['wsl_date'] = $time;
                $insert['wsl_check_by'] = $_SESSION['username'];
                $insert['wsl_status'] = '0';
                $systemfunction->insertWSSendList($insert);
                echo '<script language = "JavaScript">alert("This W/S# will approve and send at next day 01:00 AM");</script>';
            }
        }
        echo '<script language = "JavaScript">window.location="prod_mas.php?action=modify&id=' . $_REQUEST['id'] . '";</script>';
    }

```

### troubleshoot example (User Prompt)
- trouble  
user "confirm/sent" pws(production worksheet) in nps on 10/15 after 1800pm, by logic, it should auto send ws in system by 0100am next day and pws record should turn Red, but it did not turn red color.    
some case for below 
```
PS-PQL773   
PS-PQL778   
PS-PQL779   
PS-PQL780
```
- analyse    
find out which php function is showing the list of pws record in this module, and where the source code set ``` style='color:red ``` for pws record, and furthermore, find out which field and its corresponding value set color for pws record, for example, to set a red color, the value of its field should be 0, to set a blue color, the value of its field should be 1, and check if there're more fields need to update to pair the updated value of red color field, or pair the updated value of blue color field, and find out where the source code update this field into which table.

- do troubleshoot
use the knowledge of mysql, construct queries that able to fix the record with collected information, including table name, record, fields of the record, value of each field, for example, to construct queries to fix this record, the value of field 'status' should be 0 to set this record to red color, and the related fields should amend with it, so the value of field 'wsm_checkby2' should be '', and wsm_checkdate2 should be '', and wsm_step should be 1, so the expected query to do this troubleshoot: 
``` sql
UPDATE `psht_mas` SET 
    `wsm_checkby2` = '',
    `wsm_checkdate2` = '',
    `status` = '0',
    `wsm_stage` = 'WIP', 
    `wsm_stagedate` = '" . date("Y-m-d") . "',
    `wsm_step` = '1'
    WHERE `wsm_id` = $_REQUEST[id]  
```

- verify the fix
1. for front-end: read the curl_tool.md, login the application and access the function, get the response from the tool result, and check the response with the collected information.
2. for database: read the mysql-export.sh, and adapt the values for this application from database.md, and use the mysql-export.sh to export records from table, check if the fields to be amended in exported records were amend successfully.
3. if verification not passed, revert the polluted fields in records to original values. If verification passed, write down a handoff for the steps you've done for future troubleshooting.