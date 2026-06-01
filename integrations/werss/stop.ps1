$ErrorActionPreference = "Stop"
$compose = Join-Path $PSScriptRoot "compose.yaml"
docker compose -f $compose down
Write-Host "WeRSS stopped."
