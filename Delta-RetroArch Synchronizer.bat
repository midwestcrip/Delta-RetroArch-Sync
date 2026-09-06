@echo off
REM Double-click to open the launcher.
REM pythonw keeps the console window from appearing behind it.
cd /d "%~dp0"
start "" pythonw "launch_gui.pyw"
