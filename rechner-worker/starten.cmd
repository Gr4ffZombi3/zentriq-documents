@echo off
REM Zentriq-Rechner starten (Fenster offen lassen bzw. per Aufgabenplanung beim Anmelden starten)
cd /d "%~dp0"
py -3 zentriq_rechner.py %*
if errorlevel 1 pause
