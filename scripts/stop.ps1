$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  docker compose --env-file .env down
} finally {
  Pop-Location
}

Write-Host "AlphaDesk stopped."
