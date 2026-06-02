$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $root ".env"
$templatePath = Join-Path $root ".env.example"
$werssDataPath = Join-Path $root "data\werss"

function New-RandomSecret([int]$size = 32) {
  $bytes = New-Object byte[] $size
  $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $generator.GetBytes($bytes)
  } finally {
    $generator.Dispose()
  }
  return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

if (-not (Test-Path -LiteralPath $envPath)) {
  if ((Test-Path -LiteralPath $werssDataPath) -and @(Get-ChildItem -LiteralPath $werssDataPath -Force).Count -gt 0) {
    throw "Existing data\werss content was found but .env is missing. Create .env with the credentials used by the existing WeRSS database, or move data\werss aside before initializing a fresh deployment."
  }
  $content = Get-Content -LiteralPath $templatePath -Raw
  $content = $content.Replace("change-me-password-generated-by-init", (New-RandomSecret))
  $content = $content.Replace("change-me-secret-key-generated-by-init", (New-RandomSecret))
  [System.IO.File]::WriteAllText($envPath, $content, (New-Object System.Text.UTF8Encoding($false)))
  Write-Host "Created .env with generated WeRSS credentials."
} else {
  Write-Host "Using existing .env."
}

foreach ($relativePath in @("data\alphadesk", "data\werss", "backups")) {
  $path = Join-Path $root $relativePath
  New-Item -ItemType Directory -Path $path -Force | Out-Null
}

Push-Location $root
try {
  docker compose --env-file .env config --quiet
} finally {
  Pop-Location
}

Write-Host "AlphaDesk deployment directory is initialized."
