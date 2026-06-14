# run_frontend.ps1 — serve the Faber frontend (static files)
# Usage:  powershell -ExecutionPolicy Bypass -File .\run_frontend.ps1
# Then open  http://localhost:5500  in your browser.

$ErrorActionPreference = "Stop"

Set-Location "$PSScriptRoot\frontend"

Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Faber Frontend" -ForegroundColor Green
Write-Host "  Open: http://localhost:5500"  -ForegroundColor Green
Write-Host "  (backend must be running on :8000)" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green

python -m http.server 5500
