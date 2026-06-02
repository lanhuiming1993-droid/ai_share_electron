param(
  [Parameter(Mandatory = $true)]
  [string]$Archive
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$archivePath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Archive))
if (-not (Test-Path -LiteralPath $archivePath)) {
  throw "Backup archive not found: $archivePath"
}

function Assert-WorkspacePath([string]$Path) {
  $resolved = [System.IO.Path]::GetFullPath($Path)
  $prefix = $root.TrimEnd("\") + "\"
  if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to modify a path outside the AlphaDesk workspace: $resolved"
  }
  return $resolved
}

Push-Location $root
try {
  if (Test-Path -LiteralPath ".env") {
    docker compose --env-file .env down
  }
  $restoreRoot = Assert-WorkspacePath (Join-Path $root ".restore-tmp")
  $dataPath = Assert-WorkspacePath (Join-Path $root "data")
  if (Test-Path -LiteralPath $restoreRoot) {
    Remove-Item -LiteralPath $restoreRoot -Recurse -Force
  }
  New-Item -ItemType Directory -Path $restoreRoot | Out-Null
  Expand-Archive -LiteralPath $archivePath -DestinationPath $restoreRoot -Force
  Copy-Item -LiteralPath (Join-Path $restoreRoot ".env") -Destination (Join-Path $root ".env") -Force
  if (Test-Path -LiteralPath $dataPath) {
    Remove-Item -LiteralPath $dataPath -Recurse -Force
  }
  Copy-Item -LiteralPath (Join-Path $restoreRoot "data") -Destination (Join-Path $root "data") -Recurse
  Remove-Item -LiteralPath $restoreRoot -Recurse -Force
} finally {
  Pop-Location
}

Write-Host "Backup restored. Run scripts\start.ps1 to launch AlphaDesk."
