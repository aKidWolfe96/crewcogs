@echo off
rem Runs chatterbox-docker.ps1 without needing to change PowerShell's script policy.
rem Usage: chatterbox-docker.cmd up   (also: down, restart, logs, status, update, help)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0chatterbox-docker.ps1" %*
exit /b %ERRORLEVEL%
