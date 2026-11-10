@echo off
setlocal EnableExtensions
REM Usage: run_synthetic.bat EXTRACTION_OUT_DIR [BACKEND] [MODEL]
REM   EXTRACTION_OUT_DIR  output folder of ..\static_analysis\run_static_analysis.bat (contains glossary\)
REM   BACKEND             template (default) | anthropic | openai
set "HERE=%~dp0"
set "EXT=%~1"
if "%EXT%"=="" (echo Usage: run_synthetic.bat EXTRACTION_OUT_DIR [BACKEND] [MODEL] & exit /b 1)
set "BACKEND=%~2"
if "%BACKEND%"=="" set "BACKEND=template"
set "MODEL_ARG="
if not "%~3"=="" set MODEL_ARG=--model "%~3"
set "K=%HERE%02_knowledge\out"
set "S=%HERE%03_synthetic"

python "%HERE%02_knowledge\build_knowledge.py" --csv-dir "%EXT%" --glossary-dir "%EXT%\glossary" --out "%K%" || exit /b 1
python "%S%\build_synthetic.py" plan --knowledge "%K%" --out "%S%\requests.jsonl" || exit /b 1
python "%S%\build_synthetic.py" generate --requests "%S%\requests.jsonl" --out "%S%\generated_raw.jsonl" --backend %BACKEND% %MODEL_ARG% --resume || exit /b 1
python "%S%\build_synthetic.py" contrast --knowledge "%K%" --out "%S%\generated_raw.jsonl" --append || exit /b 1
python "%S%\validate_synthetic.py" --knowledge "%K%" --in "%S%\generated_raw.jsonl" --out-dir "%S%" --export-pretrain "%S%\pretrain.jsonl" || exit /b 1
echo [OK] see %S%\validation_report.json and pretrain.jsonl
endlocal
