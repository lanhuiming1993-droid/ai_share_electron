$ErrorActionPreference = "Stop"
$compose = Join-Path $PSScriptRoot "compose.yaml"
docker compose -f $compose up -d
Write-Host "WeRSS started. Return to AlphaDesk and click Login WeChat Official Account. Open http://127.0.0.1:8001/ only for advanced diagnostics or subscription maintenance."
