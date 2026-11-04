@echo off
setlocal EnableExtensions
REM ------------------------------------------------------------------
REM Usage: run_static_analysis.bat [SRC_DIR] [OUT_DIR] [SCHEMA_FILE]
REM   SRC_DIR     PHP project root       (default D:\AppServ\htdocs\Test)
REM   OUT_DIR     where labels are saved (default .\output)
REM   SCHEMA_FILE file with CREATE TABLE (default .\schema\psht_mas.md if present)
REM Optional: filter_rules.json next to this file customises which files are skipped
REM           (create it with: python file_filter.py --write-default-config filter_rules.json)
REM Requires Python 3.8+ on PATH. Read-only: never modifies the PHP source.
REM ------------------------------------------------------------------
set "HERE=%~dp0"
set "SRC=%~1"
if "%SRC%"=="" set "SRC=D:\AppServ\htdocs\Test"
set "OUT=%~2"
if "%OUT%"=="" set "OUT=%HERE%output"
set "SCHEMA=%~3"
if "%SCHEMA%"=="" if exist "%HERE%schema\psht_mas.md" set "SCHEMA=%HERE%schema\psht_mas.md"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found on PATH.
  exit /b 1
)
if not exist "%SRC%" (
  echo [ERROR] Source folder not found: %SRC%
  exit /b 1
)
if not exist "%OUT%" mkdir "%OUT%"

set "SCHEMA_ARG="
if not "%SCHEMA%"=="" set SCHEMA_ARG=--schema "%SCHEMA%"
set "FILTER_ARG="
if exist "%HERE%filter_rules.json" set FILTER_ARG=--filter-config "%HERE%filter_rules.json"

echo [%date% %time%] Step 1/2 relationships ...
python "%HERE%extract_labels.py" --src "%SRC%" --out "%OUT%" %SCHEMA_ARG% %FILTER_ARG% > "%OUT%\run.log" 2>&1
if errorlevel 1 (
  echo [ERROR] extract_labels failed. See "%OUT%\run.log"
  type "%OUT%\run.log"
  exit /b 1
)
type "%OUT%\run.log"

echo [%date% %time%] Step 2/2 forms and buttons ...
python "%HERE%extract_form_actions.py" --src "%SRC%" --out "%OUT%" %FILTER_ARG% > "%OUT%\run_forms.log" 2>&1
if errorlevel 1 (
  echo [ERROR] extract_form_actions failed. See "%OUT%\run_forms.log"
  type "%OUT%\run_forms.log"
  exit /b 1
)
type "%OUT%\run_forms.log"
echo [OK] Labels written to "%OUT%" (skipped files are listed in excluded_files.csv)
endlocal
exit /b 0
