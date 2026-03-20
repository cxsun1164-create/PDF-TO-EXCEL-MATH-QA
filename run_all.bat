@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/3] PDF -> images...
python pdf_to_images_v2.py

echo.
echo [2/3] OCR (questions/answers) -> json...
python -u ocr_processor_v2.py --rpm 60 --workers 4 --cache output_cache

echo.
echo [3/3] Align -> Excel...
python -u data_aligner.py output_cache -o aligned_result.xlsx

echo.
echo Done. Output: aligned_result.xlsx
pause

