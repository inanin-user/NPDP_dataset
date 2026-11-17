@echo off
setlocal EnableExtensions
REM ------------------------------------------------------------------------------
REM Usage: run_synthetic.bat EXTRACTION_OUT_DIR [BACKEND] [MODEL] [LIMIT]
REM   EXTRACTION_OUT_DIR  output folder of run_static_analysis.bat; must contain glossary\term_cards.jsonl
REM   BACKEND             template (default) | ollama | anthropic | openai
REM   MODEL               required for ollama / openai, e.g. qwen2.5:7b-instruct
REM   LIMIT               optional: plan only N requests (test run). Deletes the previous generated_raw.jsonl.
REM Example (cmd):
REM   run_synthetic.bat D:\Dataset_20261020\labels_v5 ollama qwen2.5:7b-instruct 20
REM Re-running without LIMIT resumes an interrupted generation (ids are stable for the same seed).
REM ------------------------------------------------------------------------------
set "HERE=%~dp0"
set "EXT=%~1"
if "%EXT%"=="" (
  echo Usage: run_synthetic.bat EXTRACTION_OUT_DIR [BACKEND] [MODEL] [LIMIT]
  exit /b 1
)
set "BACKEND=%~2"
if "%BACKEND%"=="" set "BACKEND=template"
set "K=%HERE%02_knowledge\out"
set "S=%HERE%03_synthetic"

if not exist "%EXT%\glossary\term_cards.jsonl" (
  echo [ERROR] %EXT%\glossary\term_cards.jsonl not found.
  echo         Run build_glossary.py first:  python build_glossary.py --in "%EXT%" --out "%EXT%\glossary"
  exit /b 1
)
if /I "%BACKEND%"=="ollama" if "%~3"=="" (
  echo [ERROR] the ollama backend needs a model name, e.g. qwen2.5:7b-instruct
  exit /b 1
)
if /I "%BACKEND%"=="openai" if "%~3"=="" (
  echo [ERROR] the openai backend needs a model name
  exit /b 1
)

set "MODEL_ARG="
if not "%~3"=="" set MODEL_ARG=--model "%~3"
set "LIMIT_ARG="
if not "%~4"=="" (
  set LIMIT_ARG=--limit %~4
  if exist "%S%\generated_raw.jsonl" del "%S%\generated_raw.jsonl"
)

echo [1/5] knowledge ...
python "%HERE%02_knowledge\build_knowledge.py" --csv-dir "%EXT%" --glossary-dir "%EXT%\glossary" --out "%K%" || exit /b 1
echo [2/5] plan ...
python "%S%\build_synthetic.py" plan --knowledge "%K%" --out "%S%\requests.jsonl" --ambiguous %LIMIT_ARG% || exit /b 1
echo [3/5] generate (%BACKEND%) ...
python "%S%\build_synthetic.py" generate --requests "%S%\requests.jsonl" --out "%S%\generated_raw.jsonl" --backend %BACKEND% %MODEL_ARG% --resume || exit /b 1
echo [4/5] contrastive pairs ...
python "%S%\build_synthetic.py" contrast --knowledge "%K%" --out "%S%\generated_raw.jsonl" --append || exit /b 1
echo [5/5] validate ...
python "%S%\validate_synthetic.py" --knowledge "%K%" --in "%S%\generated_raw.jsonl" --out-dir "%S%" --export-pretrain "%S%\pretrain.jsonl" --export-structured "%S%\structured.jsonl" || exit /b 1
echo [OK] results in %S%  (validation_report.json, validated.jsonl, rejected.jsonl, pretrain.jsonl, structured.jsonl)
endlocal
