param(
  [switch]$KeepStopped
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$backupDir = Join-Path $root "backups"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$archive = Join-Path $backupDir "alphadesk-backup-$timestamp.zip"

New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

Push-Location $root
$restartServices = $false
try {
  docker compose --env-file .env stop
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to stop AlphaDesk services before backup."
  }
  $restartServices = -not $KeepStopped
  & tar.exe -a -c -f $archive --exclude="data/logs" ".env" "data"
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create backup archive."
  }
  Write-Host "Backup created: $archive"
} finally {
  if ($restartServices) {
    docker compose --env-file .env start
  }
  Pop-Location
}
