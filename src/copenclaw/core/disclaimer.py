"""Security disclaimer and risk-acceptance gate.

Before copenclaw starts, the user must explicitly acknowledge the risks.
Acceptance is recorded in a marker file inside the data directory so the
prompt is only shown once.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Disclaimer text (plain, no box-drawing — callers format as needed) ────

DISCLAIMER_RISKS = [
    (
        "REMOTE CONTROL",
        "Anyone who can message your connected chat channels (Telegram, WhatsApp, Signal, Teams, Slack) "
        "can execute arbitrary commands on your machine.",
    ),
    (
        "ACCOUNT TAKEOVER = DEVICE TAKEOVER",
        "If an attacker compromises any of your linked chat accounts, they gain full remote control "
        "of this computer through copenclaw.",
    ),
    (
        "AI MISTAKES",
        "The AI agent can and will make errors. It may delete files, wipe data, corrupt configurations, "
        "or execute destructive commands — even without malicious intent.",
    ),
    (
        "PROMPT INJECTION",
        "When the agent browses the web, reads emails, or processes external content, specially crafted "
        "inputs can hijack the agent and take control of your system.",
    ),
    (
        "MALICIOUS TOOLS",
        "The agent may autonomously download and install MCP servers or other tools from untrusted "
        "sources, which could contain malware or exfiltrate your data.",
    ),
    (
        "FINANCIAL RISK",
        "If you have banking apps, crypto wallets, payment services, or trading platforms accessible "
        "from this machine, the agent (or an attacker via the agent) could make unauthorized "
        "transactions, transfers, or purchases on your behalf.",
    ),
]

DISCLAIMER_RECOMMENDATION = (
    "Run copenclaw inside a Docker container or virtual machine to limit the blast radius of any "
    "incident. Never run on a machine with access to sensitive financial accounts or irreplaceable "
    "data without appropriate isolation."
)

DISCLAIMER_FOOTER = "YOU USE THIS SOFTWARE ENTIRELY AT YOUR OWN RISK."


def format_disclaimer_plain() -> str:
    """Return the full disclaimer as a plain-text block for terminal display."""
    width = 96
    border = "=" * width

    lines = [
        "",
        border,
        "  ⚠️  SECURITY WARNING  ⚠️".center(width),
        border,
        "",
        "  copenclaw grants an AI agent FULL ACCESS to your computer.",
        "  By proceeding, you acknowledge and accept the following risks:",
        "",
    ]

    for title, desc in DISCLAIMER_RISKS:
        lines.append(f"  • {title}: {desc}")
        lines.append("")

    lines.append(f"  RECOMMENDATION: {DISCLAIMER_RECOMMENDATION}")
    lines.append("")
    lines.append(f"  {DISCLAIMER_FOOTER}")
    lines.append("")
    lines.append(border)
    lines.append("")

    return "\n".join(lines)


# ── Acceptance persistence ────────────────────────────────────────────────

_MARKER_FILENAME = ".copenclaw_accepted"


def _marker_path() -> Path:
    """Return the path to the acceptance marker file."""
    # Prefer the configured data dir, fall back to .data in cwd
    data_dir = os.environ.get("copenclaw_DATA_DIR", ".data")
    return Path(data_dir) / _MARKER_FILENAME


def has_accepted() -> bool:
    """Check whether the user has previously accepted the disclaimer."""
    return _marker_path().exists()


def record_acceptance() -> None:
    """Record that the user accepted the disclaimer."""
    marker = _marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"accepted_at={datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )


def check_or_prompt(*, allow_flag: bool = False) -> None:
    """Gate: if not yet accepted, show disclaimer and require 'I AGREE'.

    Parameters
    ----------
    allow_flag : bool
        If True, the caller has passed --accept-risks; record acceptance
        and continue without prompting.
    """
    if has_accepted():
        return

    if allow_flag:
        record_acceptance()
        return

    # Show disclaimer
    print(format_disclaimer_plain())

    # Check if we're interactive
    if not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        print("ERROR: Security disclaimer has not been accepted.")
        print("Run the installer first, or start with: copenclaw serve --accept-risks")
        sys.exit(1)

    try:
        response = input('Type "I AGREE" to accept these risks and continue, or press Enter to exit: ').strip()
    except (EOFError, KeyboardInterrupt):
        print("\nExiting.")
        sys.exit(1)

    if response.upper() != "I AGREE":
        print("\nYou must type exactly 'I AGREE' to proceed. Exiting.")
        sys.exit(1)

    record_acceptance()
    print("\n  ✔ Risks acknowledged. Acceptance recorded.\n")