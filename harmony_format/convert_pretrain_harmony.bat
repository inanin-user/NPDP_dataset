
@echo off
setlocal

cd /d "%~dp0"

set "INPUT=pretrain.jsonl"
set "OUTPUT=pretrain_harmony.jsonl"

echo ==========================================
echo OpenMythos Harmony Dataset Conversion
echo ==========================================

python pretrain_to_harmony.py ^
    --input "%INPUT%" ^
    --output "%OUTPUT%" ^
    --analysis-prob 0.0 ^
    --save-messages

if errorlevel 1 (
    echo.
    echo ERROR: Conversion failed.
    pause
    exit /b 1
)

echo.
echo Conversion successful.
echo Output: %OUTPUT%

pause
