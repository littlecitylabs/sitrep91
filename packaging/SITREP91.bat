@echo off
rem SITREP91 launcher: starts the GUI with the bundled Python, no console.
rem Every path is built from this file's own folder (%~dp0), never the
rem working folder, so a shortcut with any "Start in" still works.
rem build_release.py copies this file to the top of the release folder.
rem Any arguments are passed on to gui.py (e.g. --demo), except --diagnose,
rem which runs the scan-only health check (diagnose.py) in this window.

if exist "%~dp0python\pythonw.exe" goto run

rem No ( ) block here: a folder name containing ")" - "Program Files (x86)" -
rem would end it early.
echo SITREP91 cannot start: the python folder is missing from
echo   "%~dp0"
echo.
echo Extract the whole SITREP91 zip again, then run this file from the
echo extracted folder.
echo.
pause
exit /b 1

:run
if /i "%~1"=="--diagnose" goto diagnose
start "" "%~dp0python\pythonw.exe" "%~dp0app\gui.py" %*
exit /b 0

:diagnose
rem Scan only, never connects. It prints its answer, so it runs under
rem python.exe in this console (the app itself uses pythonw, no console).
"%~dp0python\python.exe" "%~dp0app\diagnose.py"
echo.
pause
exit /b 0
