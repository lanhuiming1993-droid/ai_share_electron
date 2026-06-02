$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot "init.ps1")

Push-Location $root
try {
  if ((Test-Path -LiteralPath "docker\frontend.Dockerfile") -and (Test-Path -LiteralPath "docker\backend.Dockerfile")) {
    docker compose --env-file .env up -d --build
  } else {
    docker compose --env-file .env pull
    docker compose --env-file .env up -d --no-build
  }
  $portLine = Get-Content -LiteralPath ".env" | Where-Object { $_ -match "^ALPHADESK_PORT=" } | Select-Object -First 1
  $port = if ($portLine) { ($portLine -split "=", 2)[1].Trim() } else { "8080" }
  $url = "http://127.0.0.1:$port"
  $readyUrl = "$url/health/ready"
  $ready = $false
  for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -Uri $readyUrl -TimeoutSec 3
      if ($response.StatusCode -eq 200) {
        $ready = $true
        break
      }
    } catch {
      Start-Sleep -Seconds 2
    }
  }
  if (-not $ready) {
    throw "AlphaDesk did not become ready in time. Run: docker compose logs --tail=200"
  }
  Write-Host "AlphaDesk is ready: $url"
} finally {
  Pop-Location
}
