$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot "backup.ps1") -KeepStopped

Push-Location $root
try {
  docker compose --env-file .env pull
  docker compose --env-file .env up -d
} finally {
  Pop-Location
}

Write-Host "AlphaDesk images updated. Run scripts\health.ps1 to verify readiness."
