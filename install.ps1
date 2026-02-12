#Requires -Version 5.1
<#
.SYNOPSIS
    copenclaw installer for Windows.
.DESCRIPTION
    Checks prerequisites, installs GitHub Copilot CLI, sets up a Python venv,
    runs the interactive channel configurator, and optionally configures
    autostart on boot.
#>

param(
    [switch]$SkipAutostart
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # speed up Invoke-WebRequest if used

# ── Colours ───────────────────────────────────────────────────────────────

function Write-Step  { param([string]$n,[string]$t) Write-Host "`n[$n] $t" -ForegroundColor Cyan }
function Write-Ok    { param([string]$t) Write-Host "  [OK] $t" -ForegroundColor Green }
function Write-Warn  { param([string]$t) Write-Host "  [!!] $t" -ForegroundColor Yellow }
function Write-Err   { param([string]$t) Write-Host "  [ERR] $t" -ForegroundColor Red }
function Write-Info  { param([string]$t) Write-Host "  $t" }

# ── Resolve project root ─────────────────────────────────────────────────

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
# If installed at repo root
if (Test-Path (Join-Path $ScriptDir "pyproject.toml")) {
    $ProjectDir = $ScriptDir
} else {
    # Script is in scripts/
    $ProjectDir = Split-Path -Parent $ScriptDir
}

if (-not (Test-Path (Join-Path $ProjectDir "pyproject.toml"))) {
    Write-Err "Cannot find pyproject.toml.  Run this script from the copenclaw repo root."
    exit 1
}

Push-Location $ProjectDir

try {

# ── Banner ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "  copenclaw  Installer  (Windows)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# ── Security Disclaimer ──────────────────────────────────────────────────

$disclaimer = @"

                                 WARNING  SECURITY WARNING  WARNING

  copenclaw grants an AI agent FULL ACCESS to your computer.
  By proceeding, you acknowledge and accept the following risks:

  * REMOTE CONTROL: Anyone who can message your connected chat channels (Telegram, WhatsApp,
    Signal, Teams, Slack) can execute arbitrary commands on your machine.

  * ACCOUNT TAKEOVER = DEVICE TAKEOVER: If an attacker compromises any of your linked chat
    accounts, they gain full remote control of this computer through copenclaw.

  * AI MISTAKES: The AI agent can and will make errors. It may delete files, wipe data, corrupt
    configurations, or execute destructive commands -- even without malicious intent.

  * PROMPT INJECTION: When the agent browses the web, reads emails, or processes external
    content, specially crafted inputs can hijack the agent and take control of your system.

  * MALICIOUS TOOLS: The agent may autonomously download and install MCP servers or other tools
    from untrusted sources, which could contain malware or exfiltrate your data.

  * FINANCIAL RISK: If you have banking apps, crypto wallets, payment services, or trading
    platforms accessible from this machine, the agent (or an attacker via the agent) could make
    unauthorized transactions, transfers, or purchases on your behalf.

  RECOMMENDATION: Run copenclaw inside a Docker container or virtual machine to limit the blast
  radius of any incident. Never run on a machine with access to sensitive financial accounts or
  irreplaceable data without appropriate isolation.

  YOU USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK.


"@

Write-Host $disclaimer -ForegroundColor Yellow
$agree = Read-Host 'Type "I AGREE" to accept these risks and continue, or press Enter to exit'
if ($agree -ne "I AGREE") {
    Write-Host ""
    Write-Host "You must type exactly 'I AGREE' to proceed. Exiting." -ForegroundColor Red
    exit 0
}

Write-Ok "Risks acknowledged."
Write-Host ""

# ── Detect existing install ───────────────────────────────────────────────

$HasVenv = Test-Path ".venv"
$HasEnv  = Test-Path ".env"

if ($HasVenv -or $HasEnv) {
    Write-Host "An existing installation was detected." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  [1] Fresh install   (wipe venv & .env, start over)"
    Write-Host "  [2] Repair          (rebuild venv & reinstall deps, keep .env)"
    Write-Host "  [3] Reconfigure     (re-run channel/workspace setup only)"
    Write-Host "  [4] Exit"
    Write-Host ""

    $choice = Read-Host "Choose an option (1-4)"
    switch ($choice) {
        "1" {
            Write-Info "Removing existing venv and .env..."
            if ($HasVenv) { Remove-Item -Recurse -Force ".venv" }
            if ($HasEnv)  { Remove-Item -Force ".env" }
            $HasVenv = $false
            $HasEnv  = $false
        }
        "2" {
            Write-Info "Repairing: will rebuild venv..."
            if ($HasVenv) { Remove-Item -Recurse -Force ".venv" }
            $HasVenv = $false
        }
        "3" {
            Write-Info "Jumping to configuration..."
            # Activate existing venv
            if ($HasVenv) {
                . .\.venv\Scripts\Activate.ps1
            }
            python scripts\configure.py --reconfigure
            Write-Ok "Reconfiguration complete."
            exit 0
        }
        default {
            Write-Host "Exiting."
            exit 0
        }
    }
}

# ── Step 1: Prerequisites ────────────────────────────────────────────────

Write-Step "1/6" "Checking prerequisites..."

# Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Err "Python is not installed or not on PATH."
    Write-Host "  Install Python >= 3.10 from https://www.python.org/downloads/"
    exit 1
}

$pyVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
$pyMajor = [int]($pyVersion.Split('.')[0])
$pyMinor = [int]($pyVersion.Split('.')[1])
if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 10)) {
    Write-Err "Python $pyVersion found but >= 3.10 is required."
    exit 1
}
Write-Ok "Python $pyVersion"

# pip
$pipOk = & python -m pip --version 2>$null
if (-not $pipOk) {
    Write-Err "pip is not available.  Re-install Python with pip enabled."
    exit 1
}
Write-Ok "pip available"

# Git (informational)
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) { Write-Ok "git available" } else { Write-Warn "git not found (optional)" }

# ── Step 2: GitHub Copilot CLI ────────────────────────────────────────────

Write-Step "2/6" "Checking GitHub Copilot CLI..."

$copilotCmd = Get-Command copilot -ErrorAction SilentlyContinue
if (-not $copilotCmd) {
    $ghCopilot = $false
    # Also check gh copilot
    $ghCmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($ghCmd) {
        $ghCopilotTest = & gh copilot --version 2>$null
        if ($LASTEXITCODE -eq 0) { $ghCopilot = $true }
    }

    if (-not $ghCopilot) {
        Write-Warn "GitHub Copilot CLI not found."
        Write-Host ""
        $install = Read-Host "  Install GitHub Copilot CLI via winget? (Y/n)"
        if ($install -eq "" -or $install -match "^[Yy]") {
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if (-not $winget) {
                Write-Err "winget not found.  Install Copilot CLI manually:"
                Write-Host "    winget install GitHub.Copilot"
                Write-Host "    -- or --"
                Write-Host "    https://docs.github.com/en/copilot/managing-copilot/managing-github-copilot-in-your-organization/managing-the-copilot-subscription-for-your-organization"
                exit 1
            }
            Write-Info "Running: winget install GitHub.Copilot"
            & winget install GitHub.Copilot --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Err "winget install failed.  Please install Copilot CLI manually."
                exit 1
            }
            # Refresh PATH
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
            Write-Ok "GitHub Copilot CLI installed"
        } else {
            Write-Warn "Skipping Copilot CLI install.  copenclaw requires it to function."
            Write-Host "  Install later: winget install GitHub.Copilot"
        }
    } else {
        Write-Ok "GitHub Copilot CLI available (via gh copilot)"
    }
} else {
    Write-Ok "GitHub Copilot CLI found"
}

# ── Copilot CLI auth check ────────────────────────────────────────────────

Write-Step "2b/6" "Verifying GitHub authentication..."

$ghToken = $env:GH_TOKEN
if (-not $ghToken) { $ghToken = $env:GITHUB_TOKEN }

if ($ghToken) {
    Write-Ok "GitHub token detected (GH_TOKEN / GITHUB_TOKEN)"
} else {
    # Try running copilot to see if already authenticated
    $copilotAvail = Get-Command copilot -ErrorAction SilentlyContinue
    if ($copilotAvail) {
        Write-Warn "No GH_TOKEN / GITHUB_TOKEN environment variable set."
        Write-Host ""
        Write-Host "  You need to authenticate with GitHub for Copilot CLI to work."
        Write-Host "  Options:"
        Write-Host "    [1] Launch copilot now for interactive login (/login, then /model)"
        Write-Host "    [2] Set a Personal Access Token (PAT) as GH_TOKEN"
        Write-Host "    [3] Skip for now"
        Write-Host ""
        $authChoice = Read-Host "  Choose (1-3)"
        switch ($authChoice) {
            "1" {
                Write-Host ""
                Write-Host "  Launching copilot CLI..." -ForegroundColor Cyan
                Write-Host "  Run /login to authenticate, optionally /model to pick your model."
                Write-Host "  Type /exit or close the window when done to continue installation."
                Write-Host ""
                try {
                    & copilot
                } catch {
                    Write-Warn "copilot exited: $_"
                }
                Write-Ok "Copilot CLI setup step complete."
            }
            "2" {
                Write-Host ""
                $pat = Read-Host "  Enter your GitHub Personal Access Token"
                if ($pat) {
                    Write-Host ""
                    Write-Host "  Where should the token be saved?"
                    Write-Host "    [1] User environment variable (persists across sessions)"
                    Write-Host "    [2] Current session only"
                    Write-Host ""
                    $patChoice = Read-Host "  Choose (1-2)"
                    if ($patChoice -eq "1") {
                        [System.Environment]::SetEnvironmentVariable("GH_TOKEN", $pat, "User")
                        $env:GH_TOKEN = $pat
                        Write-Ok "GH_TOKEN saved to user environment."
                    } else {
                        $env:GH_TOKEN = $pat
                        Write-Ok "GH_TOKEN set for current session."
                    }
                }
            }
            default {
                Write-Warn "Skipping auth.  Copilot CLI won't work until you authenticate."
                Write-Host "  Run 'copilot' and use /login, or set GH_TOKEN."
            }
        }
    } else {
        Write-Warn "Copilot CLI not available — skipping auth check."
    }
}

# ── Step 3: Virtual environment & install ─────────────────────────────────

Write-Step "3/6" "Setting up virtual environment..."

if (-not (Test-Path ".venv")) {
    Write-Info "Creating .venv..."
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to create virtual environment."
        exit 1
    }
}

Write-Info "Activating .venv..."
. .\.venv\Scripts\Activate.ps1

Write-Info "Installing copenclaw and dependencies..."
& pip install -e . 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Err "pip install failed.  Check output above."
    & pip install -e .
    exit 1
}
Write-Ok "copenclaw installed in .venv"

# ── Step 4: Interactive configuration ─────────────────────────────────────

Write-Step "4/6" "Running interactive configuration..."
Write-Host ""

& python scripts\configure.py
if ($LASTEXITCODE -ne 0) {
    Write-Err "Configuration script failed."
    exit 1
}

# Record disclaimer acceptance so the app doesn't re-prompt
& python -c "from copenclaw.core.disclaimer import record_acceptance; record_acceptance()"

# ── Step 5: Autostart ─────────────────────────────────────────────────────

Write-Step "5/6" "Autostart configuration..."

if ($SkipAutostart) {
    Write-Info "Skipped (--SkipAutostart flag)."
} else {
    Write-Host ""
    $wantAutostart = Read-Host "  Set copenclaw to start automatically on login? (y/N)"
    if ($wantAutostart -match "^[Yy]") {
        $venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
        $taskName = "copenclaw"

        # Remove existing task if present
        $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Info "Removed existing scheduled task."
        }

        $action  = New-ScheduledTaskAction `
            -Execute $venvPython `
            -Argument "-m copenclaw.cli serve" `
            -WorkingDirectory $ProjectDir

        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1)

        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Description "copenclaw - Remote Copilot CLI gateway" `
            -RunLevel Limited | Out-Null

        Write-Ok "Scheduled task '$taskName' created (runs at logon)."
        Write-Info "Manage with:  Get-ScheduledTask -TaskName $taskName"
        Write-Info "Remove with:  Unregister-ScheduledTask -TaskName $taskName"
    } else {
        Write-Info "Skipped autostart.  Start manually with: copenclaw serve"
    }
}

# ── Step 6: Verification ─────────────────────────────────────────────────

Write-Step "6/6" "Verifying installation..."

# Quick health check: start server, probe it, shut it down
$healthPassed = $false
try {
    $job = Start-Job -ScriptBlock {
        param($dir)
        Set-Location $dir
        . .\.venv\Scripts\Activate.ps1
        & python -m copenclaw.cli serve --host 127.0.0.1 --port 18790
    } -ArgumentList $ProjectDir

    Start-Sleep -Seconds 4

    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:18790/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            $healthPassed = $true
        }
    } catch {
        # Server might not be ready yet or health endpoint different
    }

    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
} catch {
    Write-Warn "Could not run health check: $_"
}

if ($healthPassed) {
    Write-Ok "Health check passed — copenclaw is working!"
} else {
    Write-Warn "Health check inconclusive.  This is normal if Copilot CLI is not yet authenticated."
    Write-Info "Start manually to verify: copenclaw serve"
}

# ── Summary ───────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Installation complete!" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Start copenclaw:     copenclaw serve"
Write-Host "  Reconfigure:         python scripts\configure.py"
Write-Host "  Reconfigure channels only:  python scripts\configure.py --reconfigure"
Write-Host ""

} finally {
    Pop-Location
}