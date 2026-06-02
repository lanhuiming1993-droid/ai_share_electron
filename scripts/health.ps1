$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$portLine = Get-Content -LiteralPath (Join-Path $root ".env") | Where-Object { $_ -match "^ALPHADESK_PORT=" } | Select-Object -First 1
$port = if ($portLine) { ($portLine -split "=", 2)[1].Trim() } else { "8080" }
$url = "http://127.0.0.1:$port"

$ready = Invoke-WebRequest -UseBasicParsing -Uri "$url/health/ready" -TimeoutSec 5
if ($ready.StatusCode -ne 200) {
  throw "AlphaDesk readiness check failed."
}

Write-Host "AlphaDesk is ready: $url"
