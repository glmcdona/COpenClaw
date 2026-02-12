#!/usr/bin/env python3
"""copenclaw interactive configurator.

Pure-stdlib script (no third-party deps) that handles:
  1. Workspace directory setup + multi-folder linking
  2. Local chat-app detection
  3. Interactive channel credential walkthrough
  4. .env file generation / merge

Can be run standalone:  python scripts/configure.py
Accepts --reconfigure to re-prompt for existing channels only.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Colours (ANSI, disabled if not a TTY) ─────────────────────────────────

_USE_COLOUR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

def green(t: str) -> str:
    return _c("32", t)

def red(t: str) -> str:
    return _c("31", t)

def yellow(t: str) -> str:
    return _c("33", t)

def cyan(t: str) -> str:
    return _c("36", t)

def bold(t: str) -> str:
    return _c("1", t)

def dim(t: str) -> str:
    return _c("2", t)

# ── Helpers ───────────────────────────────────────────────────────────────

def banner(title: str) -> None:
    line = "=" * 50
    print(f"\n{line}")
    print(f"  {title}")
    print(f"{line}\n")


def prompt(msg: str, default: str = "") -> str:
    """Prompt the user, showing [default] if provided."""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return val if val else default


def prompt_yn(msg: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"{msg} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not val:
        return default
    return val.startswith("y")


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


# ── .env reading / writing ────────────────────────────────────────────────

def read_env_file(path: Path) -> Dict[str, str]:
    """Parse a .env / .env.example file into a dict (preserving order isn't
    needed since we rebuild from the template)."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def write_env_file(template_path: Path, dest_path: Path, values: Dict[str, str]) -> None:
    """Rewrite dest_path using template_path as structure, inserting values."""
    lines: List[str] = []
    for raw in template_path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _, _ = stripped.partition("=")
            key = key.strip()
            val = values.get(key, "")
            lines.append(f"{key}={val}")
        else:
            lines.append(raw)
    dest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Workspace linking ─────────────────────────────────────────────────────

def _default_workspace() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("USERPROFILE", Path.home())) / ".copenclaw"
    return Path.home() / ".copenclaw"


def _create_link(target: Path, link: Path) -> bool:
    """Create a directory junction (Windows) or symlink (Unix)."""
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
            )
        else:
            link.symlink_to(target)
        return True
    except Exception as exc:
        print(f"  {red('✗')} Failed to create link: {exc}")
        return False


def configure_workspace(env_values: Dict[str, str]) -> Dict[str, str]:
    banner("Workspace Configuration")

    ws = _default_workspace()
    print(f"copenclaw workspace: {cyan(str(ws))}\n")
    ws.mkdir(parents=True, exist_ok=True)
    env_values["COPILOT_CLAW_WORKSPACE_DIR"] = str(ws)

    print("Link folders into the workspace so the bot can access them.")
    print(f"{dim('(repos, documents, or any folder you want the bot to reach)')}\n")

    count = 0
    while True:
        raw = prompt("Enter a folder path to link (or press Enter to finish)")
        if not raw:
            break
        target = Path(raw).expanduser().resolve()
        if not target.is_dir():
            print(f"  {red('✗')} Not a valid directory: {target}")
            continue

        name = target.name
        link = ws / name
        if link.exists():
            print(f"  {yellow('!')} A link named '{name}' already exists in the workspace.")
            name = prompt("  Enter a different name for this link", name)
            link = ws / name
            if link.exists():
                print(f"  {red('✗')} '{name}' still exists — skipping.")
                continue

        if _create_link(target, link):
            print(f"  {green('✔')} Linked: {cyan(str(link))} -> {target}")
            count += 1

    if count:
        print(f"\n{count} folder(s) linked to workspace.\n")
    else:
        print(f"\nNo folders linked.  You can link folders later by creating junctions/symlinks in {ws}\n")

    return env_values


# ── Chat-app detection ────────────────────────────────────────────────────

_DETECTORS: Dict[str, List[callable]] = {}

def _detect_windows(name: str, exe_names: List[str], paths: List[str]) -> bool:
    for exe in exe_names:
        if _which(exe):
            return True
    for p in paths:
        expanded = os.path.expandvars(p)
        if os.path.exists(expanded):
            return True
    return False


def _detect_unix(name: str, exe_names: List[str], app_paths: List[str]) -> bool:
    for exe in exe_names:
        if _which(exe):
            return True
    for p in app_paths:
        if os.path.exists(p):
            return True
    return False


class Channel:
    def __init__(
        self,
        key: str,
        label: str,
        win_exes: List[str],
        win_paths: List[str],
        unix_exes: List[str],
        mac_apps: List[str],
        env_vars: List[Tuple[str, str, str]],  # (var_name, description, hint)
    ):
        self.key = key
        self.label = label
        self.win_exes = win_exes
        self.win_paths = win_paths
        self.unix_exes = unix_exes
        self.mac_apps = mac_apps
        self.env_vars = env_vars
        self.detected = False

    def detect(self) -> bool:
        system = platform.system()
        if system == "Windows":
            self.detected = _detect_windows(self.label, self.win_exes, self.win_paths)
        elif system == "Darwin":
            self.detected = _detect_unix(self.label, self.unix_exes, self.mac_apps)
        else:
            self.detected = _detect_unix(self.label, self.unix_exes, [])
        return self.detected


CHANNELS: List[Channel] = [
    Channel(
        key="telegram",
        label="Telegram",
        win_exes=["telegram"],
        win_paths=[
            r"%APPDATA%\Telegram Desktop",
        ],
        unix_exes=["telegram-desktop"],
        mac_apps=["/Applications/Telegram.app", "/Applications/Telegram Desktop.app"],
        env_vars=[
            ("TELEGRAM_BOT_TOKEN", "Bot token from @BotFather", "e.g. 123456:ABC-DEF..."),
            ("TELEGRAM_WEBHOOK_SECRET", "Webhook secret (optional, for added security)", "any random string"),
            ("TELEGRAM_ALLOW_FROM", "Allowed chat IDs (comma-separated, leave blank for all)", "e.g. 12345,67890"),
            ("TELEGRAM_OWNER_CHAT_ID", "Your personal chat ID (for owner notifications)", "e.g. 12345"),
        ],
    ),
    Channel(
        key="whatsapp",
        label="WhatsApp",
        win_exes=[],
        win_paths=[
            r"%LOCALAPPDATA%\Packages\*WhatsApp*",
            r"%LOCALAPPDATA%\WhatsApp",
        ],
        unix_exes=["whatsapp"],
        mac_apps=["/Applications/WhatsApp.app"],
        env_vars=[
            ("WHATSAPP_PHONE_NUMBER_ID", "Phone Number ID from Meta developer dashboard", "e.g. 123456789012345"),
            ("WHATSAPP_ACCESS_TOKEN", "Permanent access token from Meta dashboard", ""),
            ("WHATSAPP_VERIFY_TOKEN", "Webhook verify token (you choose this)", "any random string"),
            ("WHATSAPP_ALLOW_FROM", "Allowed phone numbers, E.164 without + (comma-separated)", "e.g. 1234567890,0987654321"),
        ],
    ),
    Channel(
        key="signal",
        label="Signal",
        win_exes=["signal-desktop"],
        win_paths=[
            r"%LOCALAPPDATA%\Programs\signal-desktop",
        ],
        unix_exes=["signal-desktop"],
        mac_apps=["/Applications/Signal.app"],
        env_vars=[
            ("SIGNAL_API_URL", "URL of your signal-cli-rest-api instance", "e.g. http://localhost:8080"),
            ("SIGNAL_PHONE_NUMBER", "Your Signal phone number (E.164 format)", "e.g. +1234567890"),
            ("SIGNAL_ALLOW_FROM", "Allowed sender numbers (comma-separated, E.164)", "e.g. +1234567890,+0987654321"),
        ],
    ),
    Channel(
        key="teams",
        label="Microsoft Teams",
        win_exes=["ms-teams"],
        win_paths=[
            r"%LOCALAPPDATA%\Microsoft\Teams",
            r"%LOCALAPPDATA%\Packages\*Teams*",
            r"%PROGRAMFILES%\WindowsApps\*Teams*",
        ],
        unix_exes=["teams"],
        mac_apps=["/Applications/Microsoft Teams.app", "/Applications/Microsoft Teams (work or school).app"],
        env_vars=[
            ("MSTEAMS_APP_ID", "Azure Bot App ID", ""),
            ("MSTEAMS_APP_PASSWORD", "Azure Bot App Password", ""),
            ("MSTEAMS_TENANT_ID", "Azure AD Tenant ID", ""),
            ("MSTEAMS_ALLOW_FROM", "Allowed user IDs (comma-separated, leave blank for all)", ""),
        ],
    ),
    Channel(
        key="slack",
        label="Slack",
        win_exes=["slack"],
        win_paths=[
            r"%LOCALAPPDATA%\slack",
            r"%LOCALAPPDATA%\Programs\slack",
        ],
        unix_exes=["slack"],
        mac_apps=["/Applications/Slack.app"],
        env_vars=[
            ("SLACK_BOT_TOKEN", "Bot User OAuth Token (xoxb-...)", "starts with xoxb-"),
            ("SLACK_SIGNING_SECRET", "Signing Secret from Basic Information", ""),
            ("SLACK_ALLOW_FROM", "Allowed Slack user IDs (comma-separated)", "e.g. U01ABC123,U02DEF456"),
        ],
    ),
]


def _glob_exists(pattern: str) -> bool:
    """Check if an expandvars path with * glob matches anything."""
    import glob
    expanded = os.path.expandvars(pattern)
    return bool(glob.glob(expanded))


def _detect_windows_path(path: str) -> bool:
    if "*" in path:
        return _glob_exists(path)
    return os.path.exists(os.path.expandvars(path))


def detect_channels() -> None:
    """Run detection for all channels."""
    banner("Chat App Detection")
    print("Scanning for installed chat applications...\n")

    for ch in CHANNELS:
        # Override detection to handle glob paths on Windows
        system = platform.system()
        if system == "Windows":
            detected = False
            for exe in ch.win_exes:
                if _which(exe):
                    detected = True
                    break
            if not detected:
                for p in ch.win_paths:
                    if _detect_windows_path(p):
                        detected = True
                        break
            ch.detected = detected
        else:
            ch.detect()

        status = f"{green('✔')} detected" if ch.detected else f"{red('✗')} not found"
        print(f"  {status}  {ch.label}")

    print()


def select_channels() -> List[Channel]:
    """Present the channel menu and return selected channels."""
    banner("Channel Selection")
    print("Select which chat channels to configure.\n")

    for i, ch in enumerate(CHANNELS, 1):
        tag = f"  {green('✔')} detected" if ch.detected else ""
        print(f"  [{i}] {ch.label}{tag}")

    print()
    raw = prompt("Enter channel numbers (comma-separated), or 'none' to skip", "none")

    if raw.lower() == "none":
        return []

    selected: List[Channel] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(CHANNELS):
                selected.append(CHANNELS[idx])
            else:
                print(f"  {yellow('!')} Ignoring invalid number: {part}")
        else:
            print(f"  {yellow('!')} Ignoring non-numeric input: {part}")

    if selected:
        names = ", ".join(ch.label for ch in selected)
        print(f"\n  Selected: {cyan(names)}\n")
    else:
        print(f"\n  No channels selected.\n")

    return selected


def configure_channels(selected: List[Channel], env_values: Dict[str, str]) -> Dict[str, str]:
    """Walk user through credential prompts for each selected channel."""
    for ch in selected:
        banner(f"{ch.label} Configuration")

        # Show setup instructions
        if ch.key == "telegram":
            print("  Create a bot via @BotFather on Telegram: https://t.me/BotFather")
            print("  Set webhook URL: https://<your-host>/telegram/webhook\n")
        elif ch.key == "whatsapp":
            print("  Create a Meta App: https://developers.facebook.com/apps/")
            print("  Add the WhatsApp product and note your Phone Number ID")
            print("  Set webhook URL: https://<your-host>/whatsapp/webhook\n")
        elif ch.key == "signal":
            print("  You need a running signal-cli-rest-api instance.")
            print("  Docs: https://github.com/bbernhard/signal-cli-rest-api")
            print("  Docker: docker run -d -p 8080:8080 bbernhard/signal-cli-rest-api\n")
        elif ch.key == "teams":
            print("  Register a Bot in Azure Portal.")
            print("  Set messaging endpoint: https://<your-host>/teams/api/messages\n")
        elif ch.key == "slack":
            print("  Create a Slack App: https://api.slack.com/apps")
            print("  Required scopes: chat:write, files:write, channels:history, im:history")
            print("  Set event URL: https://<your-host>/slack/events\n")

        for var_name, description, hint in ch.env_vars:
            current = env_values.get(var_name, "")
            if current:
                display_current = f" {dim(f'(current: {current})')}"
            else:
                display_current = ""

            hint_str = f" {dim(hint)}" if hint else ""
            label = f"  {description}{hint_str}{display_current}"
            print(label)

            if current:
                val = prompt(f"  {var_name}", current)
            else:
                val = prompt(f"  {var_name}")

            env_values[var_name] = val
            print()

    return env_values


# ── Main flow ─────────────────────────────────────────────────────────────

def find_project_root() -> Path:
    """Walk up from this script to find pyproject.toml."""
    here = Path(__file__).resolve().parent
    for candidate in [here.parent, here, Path.cwd()]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def main() -> None:
    reconfigure_only = "--reconfigure" in sys.argv

    project_root = find_project_root()
    env_example = project_root / ".env.example"
    env_file = project_root / ".env"

    if not env_example.exists():
        print(f"{red('Error')}: Cannot find .env.example at {env_example}")
        sys.exit(1)

    # Load existing values if any
    env_values = read_env_file(env_example)  # defaults / structure
    if env_file.exists():
        existing = read_env_file(env_file)
        env_values.update(existing)

    if not reconfigure_only:
        # Phase 1: Workspace
        env_values = configure_workspace(env_values)

    # Phase 2: Detection
    detect_channels()

    # Phase 3: Selection
    selected = select_channels()

    # Phase 4: Configure selected channels
    if selected:
        env_values = configure_channels(selected, env_values)

    # Phase 5: Write .env
    banner("Saving Configuration")
    write_env_file(env_example, env_file, env_values)
    print(f"  {green('✔')} Configuration saved to {cyan(str(env_file))}\n")

    # Summary
    configured = []
    for ch in CHANNELS:
        has_value = False
        for var_name, _, _ in ch.env_vars:
            if env_values.get(var_name):
                has_value = True
                break
        if has_value:
            configured.append(ch.label)

    if configured:
        print(f"  Configured channels: {', '.join(configured)}")
    else:
        print(f"  No channels configured. Run {cyan('python scripts/configure.py')} later to set them up.")

    print()


if __name__ == "__main__":
    main()