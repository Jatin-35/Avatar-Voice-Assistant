# run_backend.ps1 — start the BotrixAI voice backend
# Usage:  right-click > Run with PowerShell,  OR  from a terminal:
#   powershell -ExecutionPolicy Bypass -File .\run_backend.ps1

$ErrorActionPreference = "Stop"

# Always run from the backend dir (imports are relative to it)
Set-Location "$PSScriptRoot\backend"

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  BotrixAI Voice Backend" -ForegroundColor Cyan
Write-Host "  WebSocket : ws://localhost:8000/ws/voice"        -ForegroundColor Cyan
Write-Host "  Health    : http://localhost:8000/health"        -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# UTF-8 so Hindi/emoji log lines don't crash on Windows consoles
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

python main.py
