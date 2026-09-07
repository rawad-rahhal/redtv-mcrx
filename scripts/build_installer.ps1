
# build_installer.ps1 — starter packaging script for Windows (no MSI yet).
# Creates a distributable folder with venv, configs, and run scripts.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 -OutDir dist\MCRX

param(
  [string]$OutDir = "dist\MCRX"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$OutPath = Join-Path $RepoRoot $OutDir

Write-Host "Building distribution at $OutPath"

if (Test-Path $OutPath) { Remove-Item -Recurse -Force $OutPath }
New-Item -ItemType Directory -Force -Path $OutPath | Out-Null

# Copy repo essentials
Copy-Item -Recurse -Force (Join-Path $RepoRoot "config") (Join-Path $OutPath "config")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "scripts") (Join-Path $OutPath "scripts")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "api_gateway") (Join-Path $OutPath "api_gateway")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "playout_core") (Join-Path $OutPath "playout_core")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "program_renderer") (Join-Path $OutPath "program_renderer")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "live_ingest") (Join-Path $OutPath "live_ingest")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "shared_contracts") (Join-Path $OutPath "shared_contracts")
Copy-Item -Recurse -Force (Join-Path $RepoRoot "operator_ui") (Join-Path $OutPath "operator_ui")
Copy-Item -Force (Join-Path $RepoRoot "pyproject.toml") (Join-Path $OutPath "pyproject.toml")
Copy-Item -Force (Join-Path $RepoRoot "README.md") (Join-Path $OutPath "README.md")

# Create venv
$Venv = Join-Path $OutPath ".venv"
python -m venv $Venv
& (Join-Path $Venv "Scripts\pip.exe") install -U pip
& (Join-Path $Venv "Scripts\pip.exe") install -e $OutPath".[dev,desktop,media]" 2>$null
if ($LASTEXITCODE -ne 0) {
  # fall back to core deps if extras fail
  & (Join-Path $Venv "Scripts\pip.exe") install -e $OutPath.
}

Write-Host "Done. Run using scripts\run_gateway.bat etc from $OutPath"
