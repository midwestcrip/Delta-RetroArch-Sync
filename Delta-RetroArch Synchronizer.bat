@echo off
REM Double-click this to open the launcher.
REM Uses pythonw so no console window appears behind the app.
setlocal
cd /d "%~dp0"
start "" pythonw -c "import sys; sys.path.insert(0, 'src'); from delta_retroarch_synchronizer import launcher; launcher.main()"
