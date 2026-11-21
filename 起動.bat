@echo off
rem Launch the AI writing tool (close this window or press Ctrl+C to stop)
cd /d "%~dp0"
python -m streamlit run app.py --server.address localhost
if errorlevel 1 pause
