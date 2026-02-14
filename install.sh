#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  COpenClaw installer for Linux / macOS
#
#  Checks prerequisites, installs GitHub Copilot CLI, sets up a Python venv,
#  runs the interactive channel configurator, and optionally configures
#  autostart on boot (systemd on Linux, launchd on macOS).
# ──────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────

if [ -t 1 ]; then
    C_RESET='\033[0m'
    C_CYAN='\033[36m'
    C_GREEN='\033[32m'
    C_YELLOW='\033[33m'
    C_RED='\033[31m'
else
    C_RESET='' C_CYAN='' C_GREEN='' C_YELLOW='' C_RED=''
fi

step()  { echo -e "\n${C_CYAN}[$1] $2${C_RESET}"; }
ok()    { echo -e "  ${C_GREEN}[OK]${C_RESET} $1"; }
warn()  { echo -e "  ${C_YELLOW}[!!]${C_RESET} $1"; }
err()   { echo -e "  ${C_RED}[ERR]${C_RESET} $1"; }
info()  { echo "  $1"; }

# ── Resolve project root (with bootstrap clone) ──────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    PROJECT_DIR="$SCRIPT_DIR"
elif [ -f "$(dirname "$SCRIPT_DIR")/pyproject.toml" ]; then
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
elif [ -f "./pyproject.toml" ]; then
    PROJECT_DIR="$(pwd)"
else
    # Not inside a repo — clone to default location
    INSTALL_DIR="$HOME/.copenclaw-src"

    if ! command -v git &>/dev/null; then
        err "git is required but not found on PATH."
        echo "  Install git:"
        if [ "$(uname -s)" = "Darwin" ]; then
            echo "    brew install git"
        else
            echo "    sudo apt install git  (Debian/Ubuntu)"
            echo "    sudo dnf install git  (Fedora)"
        fi
        exit 1
    fi

    if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
        info "Found existing install at $INSTALL_DIR, updating..."
        cd "$INSTALL_DIR" && git pull || warn "git pull failed, continuing with existing code..."
    else
        info "Cloning COpenClaw to $INSTALL_DIR..."
        git clone https://github.com/glmcdona/copenclaw.git "$INSTALL_DIR"
        if [ $? -ne 0 ]; then
            err "git clone failed. Check your internet connection and try again."
            exit 1
        fi
    fi

    PROJECT_DIR="$INSTALL_DIR"
    ok "Repository ready at $PROJECT_DIR"
fi

cd "$PROJECT_DIR"

# ── Banner ────────────────────────────────────────────────────────────────

echo ""
echo -e "${C_CYAN}==================================================${C_RESET}"
echo -e "${C_CYAN}  COpenClaw 🦀 Installer  ($(uname -s))${C_RESET}"
echo -e "${C_CYAN}==================================================${C_RESET}"
echo ""

# ── Security Disclaimer ──────────────────────────────────────────────────

echo -e "${C_YELLOW}"
cat <<'DISCLAIMER'

                            WARNING  SECURITY WARNING  WARNING

  COpenClaw grants an AI agent FULL ACCESS to your computer.
  By proceeding, you acknowledge and accept the following risks:

  • REMOTE CONTROL: Anyone who can message your connected chat channels (Telegram, WhatsApp,
    Signal, Teams, Slack) can execute arbitrary commands on your machine.

  • ACCOUNT TAKEOVER = DEVICE TAKEOVER: If an attacker compromises any of your linked chat
    accounts, they gain full remote control of this computer through COpenClaw.

  • AI MISTAKES: The AI agent can and will make errors. It may delete files, wipe data, corrupt
    configurations, or execute destructive commands — even without malicious intent.

  • PROMPT INJECTION: When the agent browses the web, reads emails, or processes external
    content, specially crafted inputs can hijack the agent and take control of your system.

  • MALICIOUS TOOLS: The agent may autonomously download and install MCP servers or other tools
    from untrusted sources, which could contain malware or exfiltrate your data.

  • FINANCIAL RISK: If you have banking apps, crypto wallets, payment services, or trading
    platforms accessible from this machine, the agent (or an attacker via the agent) could make
    unauthorized transactions, transfers, or purchases on your behalf.

  RECOMMENDATION: Run COpenClaw inside a Docker container or virtual machine to limit the blast
  radius of any incident. Never run on a machine with access to sensitive financial accounts or
  irreplaceable data without appropriate isolation.

  YOU USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK.


DISCLAIMER
echo -e "${C_RESET}"

read -rp 'Type "I AGREE" to accept these risks and continue, or press Enter to exit: ' agree
if [ "$agree" != "I AGREE" ]; then
    echo ""
    err "You must type exactly 'I AGREE' to proceed. Exiting."
    exit 0
fi

ok "Risks acknowledged."
echo ""

# ── Detect existing install ───────────────────────────────────────────────

HAS_VENV=false
HAS_ENV=false
[ -d ".venv" ] && HAS_VENV=true
[ -f ".env" ]  && HAS_ENV=true

if $HAS_VENV || $HAS_ENV; then
    echo -e "${C_YELLOW}An existing installation was detected.${C_RESET}"
    echo ""
    echo "  [1] Fresh install   (wipe venv & .env, start over)"
    echo "  [2] Repair          (rebuild venv & reinstall deps, keep .env)"
    echo "  [3] Reconfigure     (re-run channel/workspace setup only)"
    echo "  [4] Exit"
    echo ""
    read -rp "Choose an option (1-4): " choice
    case "$choice" in
        1)
            info "Removing existing venv and .env..."
            $HAS_VENV && rm -rf .venv
            $HAS_ENV  && rm -f .env
            HAS_VENV=false
            HAS_ENV=false
            ;;
        2)
            info "Repairing: will rebuild venv..."
            $HAS_VENV && rm -rf .venv
            HAS_VENV=false
            ;;
        3)
            info "Jumping to configuration..."
            if $HAS_VENV; then
                # shellcheck disable=SC1091
                source .venv/bin/activate
            fi
            python3 scripts/configure.py --reconfigure
            ok "Reconfiguration complete."
            exit 0
            ;;
        *)
            echo "Exiting."
            exit 0
            ;;
    esac
fi

# ── Step 1: Prerequisites ────────────────────────────────────────────────

step "1/6" "Checking prerequisites..."

# Python
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON_CMD="$cmd"
        break
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    err "Python is not installed or not on PATH."
    echo "  Install Python >= 3.10:"
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "    brew install python@3.12"
    else
        echo "    sudo apt install python3  (Debian/Ubuntu)"
        echo "    sudo dnf install python3  (Fedora)"
    fi
    exit 1
fi

PY_VERSION=$($PYTHON_CMD -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "Python $PY_VERSION found but >= 3.10 is required."
    exit 1
fi
ok "Python $PY_VERSION ($PYTHON_CMD)"

# pip
if ! $PYTHON_CMD -m pip --version &>/dev/null; then
    err "pip is not available.  Install it:"
    echo "    $PYTHON_CMD -m ensurepip --upgrade"
    exit 1
fi
ok "pip available"

# Git (informational)
if command -v git &>/dev/null; then
    ok "git available"
else
    warn "git not found (optional)"
fi

# ── Step 2: GitHub Copilot CLI ────────────────────────────────────────────

step "2/6" "Checking GitHub Copilot CLI..."

COPILOT_FOUND=false
if command -v copilot &>/dev/null; then
    COPILOT_FOUND=true
    ok "GitHub Copilot CLI found"
else
    # Check gh copilot
    if command -v gh &>/dev/null && gh copilot --version &>/dev/null 2>&1; then
        COPILOT_FOUND=true
        ok "GitHub Copilot CLI available (via gh copilot)"
    fi
fi

if ! $COPILOT_FOUND; then
    warn "GitHub Copilot CLI not found."
    echo ""

    if [ "$(uname -s)" = "Darwin" ]; then
        # macOS — use brew
        if command -v brew &>/dev/null; then
            read -rp "  Install GitHub Copilot CLI via Homebrew? (Y/n): " ans
            if [ -z "$ans" ] || [[ "$ans" =~ ^[Yy] ]]; then
                info "Running: brew install copilot-cli"
                brew install copilot-cli
                ok "GitHub Copilot CLI installed"
                COPILOT_FOUND=true
            else
                warn "Skipping Copilot CLI install.  COpenClaw requires it to function."
                echo "  Install later: brew install copilot-cli"
            fi
        else
            warn "Homebrew not found.  Install Copilot CLI manually:"
            echo "    brew install copilot-cli"
            echo "    -- or see: https://docs.github.com/en/copilot"
        fi
    else
        # Linux — check for brew (Linuxbrew)
        if command -v brew &>/dev/null; then
            read -rp "  Install GitHub Copilot CLI via Homebrew? (Y/n): " ans
            if [ -z "$ans" ] || [[ "$ans" =~ ^[Yy] ]]; then
                info "Running: brew install copilot-cli"
                brew install copilot-cli
                ok "GitHub Copilot CLI installed"
                COPILOT_FOUND=true
            else
                warn "Skipping Copilot CLI install.  COpenClaw requires it to function."
                echo "  Install later: brew install copilot-cli"
            fi
        else
            warn "Install GitHub Copilot CLI manually:"
            echo "    Install Homebrew first: https://brew.sh"
            echo "    Then: brew install copilot-cli"
            echo "    -- or see: https://docs.github.com/en/copilot"
        fi
    fi
fi

# ── Copilot CLI auth check ────────────────────────────────────────────────

step "2b/6" "Verifying GitHub authentication..."

GH_TOK="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
if [ -n "$GH_TOK" ]; then
    ok "GitHub token detected (GH_TOKEN / GITHUB_TOKEN)"
elif command -v copilot &>/dev/null; then
    warn "No GH_TOKEN / GITHUB_TOKEN environment variable set."
    echo ""
    echo "  You need to authenticate with GitHub for Copilot CLI to work."
    echo "  Options:"
    echo "    [1] Launch copilot now for interactive login (/login, then /model)"
    echo "    [2] Set a Personal Access Token (PAT) as GH_TOKEN"
    echo "    [3] Skip for now"
    echo ""
    read -rp "  Choose (1-3): " auth_choice
    case "$auth_choice" in
        1)
            echo ""
            echo -e "  ${C_CYAN}Launching copilot CLI...${C_RESET}"
            echo "  Run /login to authenticate, optionally /model to pick your model."
            echo "  Type /exit or press Ctrl-C when done to continue installation."
            echo ""
            copilot || true
            ok "Copilot CLI setup step complete."
            ;;
        2)
            echo ""
            read -rp "  Enter your GitHub Personal Access Token: " pat
            if [ -n "$pat" ]; then
                echo ""
                echo "  Where should the token be saved?"
                echo "    [1] Shell profile (~/.bashrc or ~/.zshrc)"
                echo "    [2] Current session only"
                echo ""
                read -rp "  Choose (1-2): " pat_choice
                if [ "$pat_choice" = "1" ]; then
                    SHELL_RC="$HOME/.bashrc"
                    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
                        SHELL_RC="$HOME/.zshrc"
                    fi
                    echo "export GH_TOKEN=\"$pat\"" >> "$SHELL_RC"
                    export GH_TOKEN="$pat"
                    ok "GH_TOKEN appended to $SHELL_RC and set for current session."
                else
                    export GH_TOKEN="$pat"
                    ok "GH_TOKEN set for current session."
                fi
            fi
            ;;
        *)
            warn "Skipping auth.  Copilot CLI won't work until you authenticate."
            echo "  Run 'copilot' and use /login, or set GH_TOKEN."
            ;;
    esac
else
    warn "Copilot CLI not available — skipping auth check."
fi

# ── Step 3: Virtual environment & install ─────────────────────────────────

step "3/6" "Setting up virtual environment..."

if [ ! -d ".venv" ]; then
    info "Creating .venv..."
    $PYTHON_CMD -m venv .venv
fi

info "Activating .venv..."
# shellcheck disable=SC1091
source .venv/bin/activate

info "Installing COpenClaw and dependencies..."
pip install -e . --quiet
ok "COpenClaw installed in .venv"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

# ── Step 4: Interactive configuration ─────────────────────────────────────

step "4/6" "Running interactive configuration..."
echo ""

python3 scripts/configure.py

# Record disclaimer acceptance so the app doesn't re-prompt
python3 -c "from copenclaw.core.disclaimer import record_acceptance; record_acceptance()"

# Optional: auto-provision Microsoft Teams bot if admin credentials are available
if [ -f ".env" ]; then
    # shellcheck disable=SC1091
    set -a
    source .env
    set +a
fi

AUTO_TEAMS_SETUP="${MSTEAMS_AUTO_SETUP:-}"
case "$AUTO_TEAMS_SETUP" in
    0|false|FALSE|False|no|NO|No)
        AUTO_TEAMS_SETUP="false"
        ;;
    *)
        AUTO_TEAMS_SETUP="true"
        ;;
esac

if [ "$AUTO_TEAMS_SETUP" = "true" ]; then
    if [ -n "${MSTEAMS_ADMIN_TENANT_ID:-}" ] \
        && [ -n "${MSTEAMS_ADMIN_CLIENT_ID:-}" ] \
        && [ -n "${MSTEAMS_ADMIN_CLIENT_SECRET:-}" ] \
        && [ -n "${MSTEAMS_AZURE_SUBSCRIPTION_ID:-}" ] \
        && [ -n "${MSTEAMS_AZURE_RESOURCE_GROUP:-}" ] \
        && [ -n "${MSTEAMS_BOT_ENDPOINT:-}" ]; then
        if [ -z "${MSTEAMS_APP_ID:-}" ] || [ -z "${MSTEAMS_APP_PASSWORD:-}" ] || [ -z "${MSTEAMS_TENANT_ID:-}" ]; then
            info "Auto-provisioning Microsoft Teams bot..."
            if $VENV_PYTHON -m copenclaw.cli teams-setup \
                --messaging-endpoint "$MSTEAMS_BOT_ENDPOINT" \
                --write-env ".env"; then
                ok "Teams auto-provisioning complete."
            else
                warn "Teams auto-provisioning failed. Run 'copenclaw teams-setup' manually."
            fi
        fi
    fi
fi

# ── Step 5: Autostart ─────────────────────────────────────────────────────

step "5/6" "Autostart configuration..."

SKIP_AUTOSTART="${SKIP_AUTOSTART:-false}"
if [ "$SKIP_AUTOSTART" = "true" ]; then
    info "Skipped (SKIP_AUTOSTART=true)."
elif [ ! -t 0 ]; then
    info "Skipped autostart (non-interactive install)."
else
    echo ""
    read -rp "  Set COpenClaw to start automatically on login? (y/N): " want_autostart
    if [[ "$want_autostart" =~ ^[Yy] ]]; then
        if [ "$(uname -s)" = "Darwin" ]; then
            # ── macOS: launchd ────────────────────────────────────────
            PLIST_DIR="$HOME/Library/LaunchAgents"
            PLIST_FILE="$PLIST_DIR/com.copenclaw.plist"
            mkdir -p "$PLIST_DIR"

            cat > "$PLIST_FILE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.copenclaw</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PYTHON</string>
        <string>-m</string>
        <string>copenclaw.cli</string>
        <string>serve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/.data/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/.data/launchd-stderr.log</string>
</dict>
</plist>
PLIST

            launchctl unload "$PLIST_FILE" 2>/dev/null || true
            launchctl load "$PLIST_FILE"
            ok "LaunchAgent created: $PLIST_FILE"
            info "Manage with:  launchctl list | grep copenclaw"
            info "Remove with:  launchctl unload $PLIST_FILE && rm $PLIST_FILE"

        else
            # ── Linux: systemd user service ───────────────────────────
            SYSTEMD_DIR="$HOME/.config/systemd/user"
            SERVICE_FILE="$SYSTEMD_DIR/copenclaw.service"
            mkdir -p "$SYSTEMD_DIR"

            cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=COpenClaw - Remote Copilot CLI gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON -m copenclaw.cli serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

            systemctl --user daemon-reload
            systemctl --user enable copenclaw.service
            ok "Systemd user service created: $SERVICE_FILE"
            info "Start now:    systemctl --user start copenclaw"
            info "Status:       systemctl --user status copenclaw"
            info "Logs:         journalctl --user -u copenclaw -f"
            info "Disable:      systemctl --user disable copenclaw"

            # Enable lingering so services run without an active login session
            if command -v loginctl &>/dev/null; then
                loginctl enable-linger "$(whoami)" 2>/dev/null || true
            fi
        fi
    else
        info "Skipped autostart.  Start manually with: COpenClaw serve"
    fi
fi

# ── Step 6: Verification ─────────────────────────────────────────────────

step "6/6" "Verifying installation..."

HEALTH_PASSED=false

# Start server in background, probe health, then kill
$VENV_PYTHON -m copenclaw.cli serve --host 127.0.0.1 --port 18790 &
SERVER_PID=$!
sleep 4

if command -v curl &>/dev/null; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18790/health 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        HEALTH_PASSED=true
    fi
fi

kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true

if $HEALTH_PASSED; then
    ok "Health check passed — COpenClaw is working!"
else
    warn "Health check inconclusive.  This is normal if Copilot CLI is not yet authenticated."
    info "Start manually to verify: COpenClaw serve"
fi

# ── Summary ───────────────────────────────────────────────────────────────

echo ""
echo -e "${C_GREEN}==================================================${C_RESET}"
echo -e "${C_GREEN}  Installation complete!${C_RESET}"
echo -e "${C_GREEN}==================================================${C_RESET}"
echo ""
echo "  Start COpenClaw:              copenclaw serve"
echo "  Reconfigure:                  python3 scripts/configure.py"
echo "  Reconfigure channels only:    python3 scripts/configure.py --reconfigure"
echo ""
