@echo off
setlocal enabledelayedexpansion

:: ──────────────────────────────────────────────────────────────────────────
::  COpenClaw installer for Windows (batch script)
::
::  Can be run standalone — if not inside a COpenClaw repo, it will clone
::  the repo to %USERPROFILE%\.copenclaw-src and install from there.
::
::  Usage:
::    install.bat              Normal install
::    install.bat --no-auto    Skip autostart setup
:: ──────────────────────────────────────────────────────────────────────────

set "SKIP_AUTOSTART=0"
if "%~1"=="--no-auto" set "SKIP_AUTOSTART=1"

:: ── Banner ───────────────────────────────────────────────────────────────

echo.
echo ==================================================
echo   COpenClaw  Installer  (Windows)
echo ==================================================
echo.

:: ── Security Disclaimer ─────────────────────────────────────────────────

echo.
echo                         WARNING  SECURITY WARNING  WARNING
echo.
echo   COpenClaw grants an AI agent FULL ACCESS to your computer.
echo   By proceeding, you acknowledge and accept the following risks:
echo.
echo   * REMOTE CONTROL: Anyone who can message your connected chat channels
echo     (Telegram, WhatsApp, Signal, Teams, Slack) can execute arbitrary
echo     commands on your machine.
echo.
echo   * ACCOUNT TAKEOVER = DEVICE TAKEOVER: If an attacker compromises any
echo     of your linked chat accounts, they gain full remote control of this
echo     computer through COpenClaw.
echo.
echo   * AI MISTAKES: The AI agent can and will make errors. It may delete
echo     files, wipe data, corrupt configurations, or execute destructive
echo     commands -- even without malicious intent.
echo.
echo   * PROMPT INJECTION: When the agent browses the web, reads emails, or
echo     processes external content, specially crafted inputs can hijack the
echo     agent and take control of your system.
echo.
echo   * MALICIOUS TOOLS: The agent may autonomously download and install MCP
echo     servers or other tools from untrusted sources, which could contain
echo     malware or exfiltrate your data.
echo.
echo   * FINANCIAL RISK: If you have banking apps, crypto wallets, payment
echo     services, or trading platforms accessible from this machine, the
echo     agent (or an attacker via the agent) could make unauthorized
echo     transactions, transfers, or purchases on your behalf.
echo.
echo   RECOMMENDATION: Run COpenClaw inside a Docker container or virtual
echo   machine to limit the blast radius of any incident.
echo.
echo   YOU USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK.
echo.

set /p "AGREE=Type I AGREE to accept these risks and continue, or press Enter to exit: "
if not "!AGREE!"=="I AGREE" (
    echo.
    echo [ERR] You must type exactly 'I AGREE' to proceed. Exiting.
    goto :end
)
echo   [OK] Risks acknowledged.
echo.

:: ── Bootstrap: clone repo if not inside one ─────────────────────────────

set "PROJECT_DIR=%~dp0"
:: Remove trailing backslash
if "!PROJECT_DIR:~-1!"=="\" set "PROJECT_DIR=!PROJECT_DIR:~0,-1!"

if exist "!PROJECT_DIR!\pyproject.toml" (
    echo   Found COpenClaw repo at !PROJECT_DIR!
    goto :have_repo
)

:: Not in a repo — clone to default location
set "INSTALL_DIR=%USERPROFILE%\.copenclaw-src"

:: Check for git
where git >nul 2>&1
if errorlevel 1 (
    echo [ERR] git is required but not found on PATH.
    echo   Install git from: https://git-scm.com/download/win
    goto :end
)

if exist "!INSTALL_DIR!\pyproject.toml" (
    echo   Found existing install at !INSTALL_DIR!, updating...
    pushd "!INSTALL_DIR!"
    git pull
    if errorlevel 1 (
        echo   [!!] git pull failed, continuing with existing code...
    )
    popd
) else (
    echo   Cloning COpenClaw to !INSTALL_DIR!...
    git clone https://github.com/glmcdona/copenclaw.git "!INSTALL_DIR!"
    if errorlevel 1 (
        echo [ERR] git clone failed. Check your internet connection and try again.
        goto :end
    )
)

set "PROJECT_DIR=!INSTALL_DIR!"
echo   [OK] Repository ready at !PROJECT_DIR!

:have_repo
pushd "!PROJECT_DIR!"

:: ── Detect existing install ─────────────────────────────────────────────

set "HAS_VENV=0"
set "HAS_ENV=0"
if exist ".venv" set "HAS_VENV=1"
if exist ".env" set "HAS_ENV=1"

if "!HAS_VENV!"=="1" goto :existing
if "!HAS_ENV!"=="1" goto :existing
goto :fresh_install

:existing
echo   An existing installation was detected.
echo.
echo   [1] Fresh install   (wipe venv ^& .env, start over)
echo   [2] Repair          (rebuild venv ^& reinstall deps, keep .env)
echo   [3] Reconfigure     (re-run channel/workspace setup only)
echo   [4] Exit
echo.
set /p "CHOICE=Choose an option (1-4): "

if "!CHOICE!"=="1" (
    echo   Removing existing venv and .env...
    if exist ".venv" rmdir /s /q ".venv"
    if exist ".env" del /f ".env"
    set "HAS_VENV=0"
    set "HAS_ENV=0"
    goto :fresh_install
)
if "!CHOICE!"=="2" (
    echo   Repairing: will rebuild venv...
    if exist ".venv" rmdir /s /q ".venv"
    set "HAS_VENV=0"
    goto :fresh_install
)
if "!CHOICE!"=="3" (
    echo   Jumping to configuration...
    if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
    python scripts\configure.py --reconfigure
    echo   [OK] Reconfiguration complete.
    goto :done
)
echo   Exiting.
goto :done

:: ── Step 1: Prerequisites ───────────────────────────────────────────────

:fresh_install
echo.
echo [1/6] Checking prerequisites...

:: Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERR] Python is not installed or not on PATH.
    echo   Install Python ^>= 3.10 from https://www.python.org/downloads/
    goto :done
)

for /f "tokens=*" %%v in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2^>nul') do set "PY_VERSION=%%v"
for /f "tokens=1,2 delims=." %%a in ("!PY_VERSION!") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

if !PY_MAJOR! LSS 3 (
    echo [ERR] Python !PY_VERSION! found but ^>= 3.10 is required.
    goto :done
)
if !PY_MAJOR! EQU 3 if !PY_MINOR! LSS 10 (
    echo [ERR] Python !PY_VERSION! found but ^>= 3.10 is required.
    goto :done
)
echo   [OK] Python !PY_VERSION!

:: pip
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERR] pip is not available. Re-install Python with pip enabled.
    goto :done
)
echo   [OK] pip available

:: Git
where git >nul 2>&1
if errorlevel 1 (
    echo   [!!] git not found (optional for updates)
) else (
    echo   [OK] git available
)

:: ── Step 2: GitHub Copilot CLI ──────────────────────────────────────────

echo.
echo [2/6] Checking GitHub Copilot CLI...

set "COPILOT_FOUND=0"
where copilot >nul 2>&1
if not errorlevel 1 (
    set "COPILOT_FOUND=1"
    echo   [OK] GitHub Copilot CLI found
    goto :copilot_auth
)

:: Check gh copilot
where gh >nul 2>&1
if not errorlevel 1 (
    gh copilot --version >nul 2>&1
    if not errorlevel 1 (
        set "COPILOT_FOUND=1"
        echo   [OK] GitHub Copilot CLI available (via gh copilot)
        goto :copilot_auth
    )
)

echo   [!!] GitHub Copilot CLI not found.
echo.
set /p "INSTALL_COPILOT=  Install GitHub Copilot CLI via winget? (Y/n): "
if "!INSTALL_COPILOT!"=="" set "INSTALL_COPILOT=Y"

echo !INSTALL_COPILOT! | findstr /i "^Y" >nul
if not errorlevel 1 (
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [ERR] winget not found. Install Copilot CLI manually:
        echo     winget install GitHub.Copilot
        echo     -- or --
        echo     https://docs.github.com/en/copilot
        goto :copilot_auth
    )
    echo   Running: winget install GitHub.Copilot
    winget install GitHub.Copilot --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo [ERR] winget install failed. Please install Copilot CLI manually.
    ) else (
        echo   [OK] GitHub Copilot CLI installed
        set "COPILOT_FOUND=1"
    )
    :: Refresh PATH
    for /f "tokens=*" %%p in ('echo %PATH%') do set "PATH=%%p"
) else (
    echo   [!!] Skipping Copilot CLI install. COpenClaw requires it to function.
    echo   Install later: winget install GitHub.Copilot
)

:: ── Step 2b: Auth check ─────────────────────────────────────────────────

:copilot_auth
echo.
echo [2b/6] Verifying GitHub authentication...

if defined GH_TOKEN (
    echo   [OK] GitHub token detected (GH_TOKEN)
    goto :setup_venv
)
if defined GITHUB_TOKEN (
    echo   [OK] GitHub token detected (GITHUB_TOKEN)
    goto :setup_venv
)

if "!COPILOT_FOUND!"=="0" (
    echo   [!!] Copilot CLI not available -- skipping auth check.
    goto :setup_venv
)

echo   [!!] No GH_TOKEN / GITHUB_TOKEN environment variable set.
echo.
echo   You need to authenticate with GitHub for Copilot CLI to work.
echo   Options:
echo     [1] Launch copilot now for interactive login (/login, then /model)
echo     [2] Set a Personal Access Token (PAT) as GH_TOKEN
echo     [3] Skip for now
echo.
set /p "AUTH_CHOICE=  Choose (1-3): "

if "!AUTH_CHOICE!"=="1" (
    echo.
    echo   Launching copilot CLI...
    echo   Run /login to authenticate, optionally /model to pick your model.
    echo   Type /exit or close the window when done to continue installation.
    echo.
    copilot
    echo   [OK] Copilot CLI setup step complete.
    goto :setup_venv
)
if "!AUTH_CHOICE!"=="2" (
    echo.
    set /p "PAT=  Enter your GitHub Personal Access Token: "
    if defined PAT (
        echo.
        echo   Where should the token be saved?
        echo     [1] User environment variable (persists across sessions)
        echo     [2] Current session only
        echo.
        set /p "PAT_CHOICE=  Choose (1-2): "
        if "!PAT_CHOICE!"=="1" (
            setx GH_TOKEN "!PAT!" >nul 2>&1
            set "GH_TOKEN=!PAT!"
            echo   [OK] GH_TOKEN saved to user environment.
        ) else (
            set "GH_TOKEN=!PAT!"
            echo   [OK] GH_TOKEN set for current session.
        )
    )
    goto :setup_venv
)
echo   [!!] Skipping auth. Copilot CLI won't work until you authenticate.
echo   Run 'copilot' and use /login, or set GH_TOKEN.

:: ── Step 3: Virtual environment & install ───────────────────────────────

:setup_venv
echo.
echo [3/6] Setting up virtual environment...

if not exist ".venv" (
    echo   Creating .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERR] Failed to create virtual environment.
        goto :done
    )
)

echo   Activating .venv...
call ".venv\Scripts\activate.bat"

echo   Installing COpenClaw and dependencies...
pip install -e . --quiet >nul 2>&1
if errorlevel 1 (
    echo   [!!] Quiet install failed, retrying with output...
    pip install -e .
    if errorlevel 1 (
        echo [ERR] pip install failed.
        goto :done
    )
)
echo   [OK] COpenClaw installed in .venv

:: ── Step 4: Interactive configuration ───────────────────────────────────

echo.
echo [4/6] Running interactive configuration...
echo.

python scripts\configure.py
if errorlevel 1 (
    echo [ERR] Configuration script failed.
    goto :done
)

:: Record disclaimer acceptance
python -c "from copenclaw.core.disclaimer import record_acceptance; record_acceptance()" >nul 2>&1

:: Optional: auto-provision Microsoft Teams bot if admin credentials are available
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if not "%%a"=="" if not "%%a:~0,1"=="#" set "%%a=%%b"
    )
)

set "AUTO_TEAMS_SETUP=true"
if /i "%MSTEAMS_AUTO_SETUP%"=="false" set "AUTO_TEAMS_SETUP=false"
if /i "%MSTEAMS_AUTO_SETUP%"=="0" set "AUTO_TEAMS_SETUP=false"
if /i "%MSTEAMS_AUTO_SETUP%"=="no" set "AUTO_TEAMS_SETUP=false"

if "!AUTO_TEAMS_SETUP!"=="true" (
    if defined MSTEAMS_ADMIN_TENANT_ID if defined MSTEAMS_ADMIN_CLIENT_ID if defined MSTEAMS_ADMIN_CLIENT_SECRET if defined MSTEAMS_AZURE_SUBSCRIPTION_ID if defined MSTEAMS_AZURE_RESOURCE_GROUP if defined MSTEAMS_BOT_ENDPOINT (
        if not defined MSTEAMS_APP_ID if not defined MSTEAMS_APP_PASSWORD if not defined MSTEAMS_TENANT_ID (
            echo   Auto-provisioning Microsoft Teams bot...
            ".venv\Scripts\python.exe" -m copenclaw.cli teams-setup --messaging-endpoint "!MSTEAMS_BOT_ENDPOINT!" --write-env ".env"
            if errorlevel 1 (
                echo   [!!] Teams auto-provisioning failed. Run 'copenclaw teams-setup' manually.
            ) else (
                echo   [OK] Teams auto-provisioning complete.
            )
        )
    )
)

:: ── Step 5: Autostart ───────────────────────────────────────────────────

echo.
echo [5/6] Autostart configuration...

if "!SKIP_AUTOSTART!"=="1" (
    echo   Skipped (--no-auto flag).
    goto :verify
)

echo.
set /p "WANT_AUTO=  Set COpenClaw to start automatically on login? (Y/n): "
if "!WANT_AUTO!"=="" set "WANT_AUTO=Y"

echo !WANT_AUTO! | findstr /i "^Y" >nul
if errorlevel 1 (
    echo   Skipped autostart. Start manually with: COpenClaw serve
    goto :verify
)

set "VENV_PYTHON=!PROJECT_DIR!\.venv\Scripts\python.exe"
set "TASK_NAME=copenclaw"

:: Remove existing task if present
schtasks /query /tn "!TASK_NAME!" >nul 2>&1
if not errorlevel 1 (
    schtasks /delete /tn "!TASK_NAME!" /f >nul 2>&1
    echo   Removed existing scheduled task.
)

schtasks /create /tn "!TASK_NAME!" /tr "\"!VENV_PYTHON!\" -m copenclaw.cli serve" /sc onlogon /rl limited /f >nul 2>&1
if errorlevel 1 (
    echo   [!!] Failed to create scheduled task. You may need to run as administrator.
    echo   Create manually: schtasks /create /tn copenclaw /tr "\"!VENV_PYTHON!\" -m copenclaw.cli serve" /sc onlogon
) else (
    echo   [OK] Scheduled task '!TASK_NAME!' created (runs at logon).
    echo   Manage with:  schtasks /query /tn !TASK_NAME!
    echo   Remove with:  schtasks /delete /tn !TASK_NAME! /f
)

:: ── Step 6: Verification ────────────────────────────────────────────────

:verify
echo.
echo [6/6] Verifying installation...

set "HEALTH_PASSED=0"

:: Start server in background
start /b "" "!VENV_PYTHON!" -m copenclaw.cli serve --host 127.0.0.1 --port 18790 >nul 2>&1

:: Wait for startup
timeout /t 4 /nobreak >nul 2>&1

:: Check health
where curl >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%h in ('curl -s -o nul -w "%%{http_code}" http://127.0.0.1:18790/health 2^>nul') do (
        if "%%h"=="200" set "HEALTH_PASSED=1"
    )
)

:: Kill the background server
for /f "tokens=2" %%p in ('tasklist /fi "imagename eq python.exe" /fo list 2^>nul ^| findstr "PID"') do (
    taskkill /pid %%p /f >nul 2>&1
)

if "!HEALTH_PASSED!"=="1" (
    echo   [OK] Health check passed -- COpenClaw is working!
) else (
    echo   [!!] Health check inconclusive. This is normal if Copilot CLI is not yet authenticated.
    echo   Start manually to verify: COpenClaw serve
)

:: ── Summary ─────────────────────────────────────────────────────────────

:summary
echo.
echo ==================================================
echo   Installation complete!
echo ==================================================
echo.
echo   Install location:  !PROJECT_DIR!
echo   Start COpenClaw:   copenclaw serve
echo   Reconfigure:       python scripts\configure.py
echo   Reconfigure channels only:  python scripts\configure.py --reconfigure
echo.

:done
popd 2>nul
endlocal
pause
