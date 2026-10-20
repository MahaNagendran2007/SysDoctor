@echo off
REM Controlled demonstrations for Windows. Each window stops by itself or with Ctrl+C.
cd /d %~dp0\..
call .venv\Scripts\activate
echo 1 = CPU hog (180 s)   2 = Memory growth (180 s)   3 = Disk pressure (120 s)
set /p choice=Choose 1, 2 or 3: 
if "%choice%"=="1" python stress_tests\cpu_hog.py --seconds 180
if "%choice%"=="2" python stress_tests\memory_leak.py --rate 20 --seconds 180
if "%choice%"=="3" python stress_tests\disk_io.py --mb 256 --seconds 120
