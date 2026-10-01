@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop_Demo.ps1"
if errorlevel 1 pause

