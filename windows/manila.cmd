@echo off
rem Manila's command line, runnable from anywhere.
rem
rem `python -m manila` finds the package only from the folder Manila was copied
rem into: nothing is installed into site-packages, deliberately, because nothing
rem about Manila is installed at all. That is fine when you are standing in the
rem folder and a puzzle when you are not -- the error is "No module named
rem manila", which sounds like a broken install rather than a wrong directory.
rem
rem So this puts its own folder on PYTHONPATH and gets out of the way. Called by
rem its full path it works from any directory:
rem
rem     & "$env:LOCALAPPDATA\Programs\Manila\windows\manila.cmd" --mcp-install
rem
setlocal
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"

rem The py launcher is the right way in when it is there, since it picks a real
rem Python. Asked for its version rather than merely looked for on disk: a file
rem called python.exe on PATH proves nothing on Windows, where the shell ships
rem a placeholder that only advertises the Store, and the same caution is what
rem install.ps1 applies when it goes looking for an interpreter.
py -V >nul 2>&1
if %ERRORLEVEL%==0 (
    py -m manila %*
) else (
    python -m manila %*
)
set "CODE=%ERRORLEVEL%"
endlocal & exit /b %CODE%
