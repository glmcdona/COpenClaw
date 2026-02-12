param(
  [string]$BindHost = "127.0.0.1",
  [int]$Port = 18790
)

$ErrorActionPreference = "Stop"

# Resolve the copenclaw project directory (where pyproject.toml lives)
# The script lives at copenclaw/scripts/start-windows.ps1
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

# Change to the project directory so pip install and venv work correctly
Push-Location $ProjectDir
try {
  if (-not $env:copenclaw_WORKSPACE_DIR -or [string]::IsNullOrWhiteSpace($env:copenclaw_WORKSPACE_DIR)) {
    $defaultWorkspace = Join-Path $env:USERPROFILE ".githubclaw"
    $env:copenclaw_WORKSPACE_DIR = $defaultWorkspace
  }

  if (-not (Test-Path $env:copenclaw_WORKSPACE_DIR)) {
    New-Item -ItemType Directory -Path $env:copenclaw_WORKSPACE_DIR | Out-Null
  }

  if (-Not (Test-Path .venv)) {
    Write-Host "Creating venv..."
    python -m venv .venv
  }

  Write-Host "Activating venv..."
  . .\.venv\Scripts\Activate.ps1

  Write-Host "Installing deps..."
  pip install -e .

  Write-Host "Starting copenclaw..."
  copenclaw serve --host $BindHost --port $Port
} finally {
  Pop-Location
}