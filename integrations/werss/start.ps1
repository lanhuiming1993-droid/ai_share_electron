$ErrorActionPreference = "Stop"
$compose = Join-Path $PSScriptRoot "compose.yaml"
docker compose -f $compose up -d
Write-Host "WeRSS started. Open http://127.0.0.1:8001/ to log in, scan the WeChat QR code, and manage subscriptions."
