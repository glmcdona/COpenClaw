"""Execution policy — controls which shell commands are allowed.

Three modes:
  1. allow_all=True  → everything allowed except denied commands
  2. allowed_commands non-empty → only those base commands allowed
  3. Both empty → nothing allowed (safe default)

Matching is by **base command** (the first whitespace-delimited token),
not the full command string. For example, allowing "git" permits
"git status", "git log --oneline", etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import subprocess
import sys
from typing import Iterable, Set

logger = logging.getLogger("copenclaw.policy")

# Dangerous patterns matched as substrings in the full command
DEFAULT_DENIED_PATTERNS = {
    "rm -rf /",
    ":(){:|:&};:",  # fork bomb
}

# Dangerous base commands — matched only against the extracted base command,
# NOT as substrings (avoids false positives like "dd" in paths/task IDs)
DEFAULT_DENIED_BASE_COMMANDS = {
    "format",
    "dd",
    "timeout",  # Interactive/blocking: waits for keypress or countdown
    "sleep",    # Blocking: used by AI to "sleep" instead of returning
    "pause",    # Interactive: waits for keypress (Windows)
    "choice",   # Interactive: waits for keypress (Windows)
    "read",     # Interactive: waits for stdin input (Unix)
}

# Base command prefixes that are always blocked (e.g. mkfs, mkfs.ext4, mkfs.xfs)
DEFAULT_DENIED_BASE_PREFIXES = {
    "mkfs",
}

@dataclass
class ExecutionPolicy:
    allowed_commands: Set[str] = field(default_factory=set)
    denied_commands: Set[str] = field(default_factory=set)
    allow_all: bool = False

    def _extract_base_command(self, command: str) -> str:
        """Extract the base command (first token) from a command string."""
        command = command.strip()
        if not command:
            return ""
        # Handle common shell patterns
        # Strip leading env vars like VAR=val cmd
        parts = command.split()
        for part in parts:
            if "=" in part and not part.startswith("-"):
                continue  # skip VAR=value prefixes
            return part.lower()
        return parts[0].lower() if parts else ""

    def is_allowed(self, command: str) -> bool:
        """Check if a command is allowed by this policy.

        Matching is by base command (first token), not the full string.
        """
        if not command or not command.strip():
            logger.debug("Policy: empty command → denied")
            return False

        # Always block dangerous substring patterns
        cmd_lower = command.strip().lower()
        for pattern in DEFAULT_DENIED_PATTERNS:
            if pattern in cmd_lower:
                logger.warning("Policy: command matches DEFAULT_DENIED_PATTERNS '%s' → denied", pattern)
                return False

        # Extract base command for all further checks
        base = self._extract_base_command(command)

        # Block dangerous base commands (exact match, not substring)
        if base in DEFAULT_DENIED_BASE_COMMANDS:
            logger.warning("Policy: base command '%s' in DEFAULT_DENIED_BASE_COMMANDS → denied", base)
            return False
        # Block dangerous command prefixes (e.g. mkfs.ext4, mkfs.xfs, etc.)
        for prefix in DEFAULT_DENIED_BASE_PREFIXES:
            if base.startswith(prefix):
                logger.warning("Policy: base command '%s' matches DEFAULT_DENIED prefix '%s' → denied", base, prefix)
                return False
        if base in self.denied_commands:
            logger.info("Policy: base command '%s' in denied_commands → denied", base)
            return False

        if self.allow_all:
            logger.debug("Policy: allow_all=True, base='%s' → allowed", base)
            return True

        # Check if base command is in the allowed set
        allowed = base in self.allowed_commands
        if allowed:
            logger.debug("Policy: base='%s' in allowed_commands → allowed", base)
        else:
            logger.info(
                "Policy: base='%s' NOT in allowed_commands=%s, allow_all=%s → denied",
                base, self.allowed_commands, self.allow_all,
            )
        return allowed

    def add_allowed(self, commands: Iterable[str]) -> None:
        self.allowed_commands.update(c.lower().strip() for c in commands if c.strip())

    def add_denied(self, commands: Iterable[str]) -> None:
        self.denied_commands.update(c.lower().strip() for c in commands if c.strip())

def load_execution_policy() -> ExecutionPolicy:
    allow_all_raw = os.getenv("copenclaw_ALLOW_ALL_COMMANDS", "false")
    allow_all = allow_all_raw.lower() in {"1", "true", "yes"}
    allowed = os.getenv("copenclaw_ALLOWED_COMMANDS", "")
    allowed_set = {c.strip().lower() for c in allowed.split(",") if c.strip()}
    denied = os.getenv("copenclaw_DENIED_COMMANDS", "")
    denied_set = {c.strip().lower() for c in denied.split(",") if c.strip()}

    logger.info(
        "Loaded execution policy: allow_all=%s (raw='%s'), allowed=%s, denied=%s",
        allow_all, allow_all_raw, allowed_set or "(empty)", denied_set or "(empty)",
    )

    return ExecutionPolicy(allowed_commands=allowed_set, denied_commands=denied_set, allow_all=allow_all)

def run_command(command: str, policy: ExecutionPolicy, timeout: int | None = None, cwd: str | None = None) -> str:
    """Execute a shell command subject to the execution policy.

    On Windows, forces cmd.exe as the shell to avoid PowerShell escaping issues
    (e.g. @ being interpreted as a here-string terminator).
    """
    if not policy.is_allowed(command):
        raise PermissionError("command not allowed by policy")

    if timeout is None:
        timeout = int(os.getenv("copenclaw_EXEC_TIMEOUT", "300"))

    logger.info("Executing command (timeout=%ss): %s", timeout, command[:200])

    # On Windows, explicitly use cmd.exe to avoid PowerShell escaping issues
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "shell": True,
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if cwd:
        kwargs["cwd"] = cwd
    if sys.platform == "win32":
        kwargs["executable"] = os.environ.get("COMSPEC", "cmd.exe")

    try:
        result = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out after %ss: %s", timeout, command[:200])
        raise RuntimeError(
            f"Command timed out after {timeout}s and was killed. "
            f"Avoid long-running or interactive commands, or increase copenclaw_EXEC_TIMEOUT."
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        logger.warning("Command failed (exit %d): %s", result.returncode, stderr[:300])
        raise RuntimeError(stderr or f"command failed with exit code {result.returncode}")

    output = result.stdout.strip()
    logger.debug("Command output (%d chars): %s", len(output), output[:200])
    return output
