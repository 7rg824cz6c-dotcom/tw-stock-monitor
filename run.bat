@echo off
cd /d C:\dev\stock
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
.venv\Scripts\python.exe tw_stock_monitor_v2.py >> run.log 2>&1
