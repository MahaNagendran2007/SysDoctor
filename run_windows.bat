@echo off
REM SysDoctor launcher for Windows 10/11
cd /d %~dp0
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate
python -m pip install -q -r requirements.txt
python run_sysdoctor.py
