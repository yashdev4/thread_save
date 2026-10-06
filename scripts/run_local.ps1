# Run ThreadVault locally (file storage, no auth, NO GitHub sync) for testing.
# Usage:  powershell -File scripts\run_local.ps1 [-Port 8000]
param([int]$Port = 8000)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:THREADVAULT_STORAGE_BACKEND = "file"
$env:THREAD_SAVE_VAULT_ROOT      = Join-Path $root "vault_local"   # gitignored (vault_*/)
$env:THREADVAULT_AUTH_PAUSED     = "true"
$env:THREADVAULT_ENFORCE_AUTH    = "false"
$env:THREADVAULT_ALLOWED_HOSTS   = "*"

# Safety: never export local test data to the real GitHub archive repo.
$env:THREADVAULT_GH_REPO = ""
Remove-Item Env:GITHUB_TOKEN, Env:THREADVAULT_GH_TOKEN -ErrorAction SilentlyContinue

Write-Host "ThreadVault local: http://127.0.0.1:$Port/mcp   vault: $env:THREAD_SAVE_VAULT_ROOT"
python -m uvicorn thread_save.web.app:create_app --factory --host 127.0.0.1 --port $Port --reload --reload-dir src
