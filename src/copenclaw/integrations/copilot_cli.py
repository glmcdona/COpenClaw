from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from copenclaw.core.logging_config import (
    append_to_file,
    get_activity_log_path,
    get_orchestrator_log_path,
)
from copenclaw.core.mcp_registry import get_user_servers_for_merge

logger = logging.getLogger("copenclaw.copilot_cli")

DEFAULT_TIMEOUT = 7200  # seconds (2 hours)


class CopilotCliError(RuntimeError):
    pass


def write_mcp_config(
    target_dir: str,
    mcp_server_url: str,
    mcp_token: Optional[str] = None,
    filename: str = "copilot-mcp-config.json",
    task_id: Optional[str] = None,
    role: Optional[str] = None,
) -> str:
    """Write an MCP config JSON file into *target_dir* and return the absolute path.

    This is a module-level helper so both CopilotCli and worker/supervisor
    code can write a config into any directory they control.

    If *task_id* and *role* are provided, they are appended as query
    parameters to the URL so the server can identify which task a
    tool call belongs to and log it to the per-task event stream.
    """
    os.makedirs(target_dir, exist_ok=True)

    # Build URL with optional task routing query params
    url = mcp_server_url
    if task_id:
        sep = "&" if "?" in url else "?"
        url += f"{sep}task_id={task_id}"
        if role:
            url += f"&role={role}"

    config: dict = {
        "mcpServers": {
            "copenclaw": {
                "type": "http",
                "url": url,
                "tools": ["*"],
            }
        }
    }
    if mcp_token:
        config["mcpServers"]["copenclaw"]["headers"] = {
            "x-mcp-token": mcp_token,
        }

    # Merge user-installed MCP servers from ~/.copilot/mcp-config.json
    # so that workers/supervisors have access to the same tools as the brain
    try:
        user_servers = get_user_servers_for_merge()
        if user_servers:
            config["mcpServers"].update(user_servers)
            logger.debug("Merged %d user MCP servers into task config", len(user_servers))
    except Exception:  # noqa: BLE001
        logger.debug("Could not merge user MCP servers (non-fatal)", exc_info=True)

    config_path = os.path.join(target_dir, filename)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    abs_path = os.path.abspath(config_path)
    logger.debug("Wrote MCP config: %s (task_id=%s, role=%s)", abs_path, task_id, role)
    return abs_path


class CopilotCli:
    """Adapter that invokes Copilot CLI for each prompt.

    System instructions live in .github/copilot-instructions.md in the
    workspace directory — Copilot CLI reads that file automatically.
    Each ``run_prompt`` call passes only the user's message via ``-p``,
    keeping prompt and instructions cleanly separated.

    Output is streamed line-by-line to the logger and to per-task log
    files so you can watch in real-time.
    """

    def __init__(
        self,
        executable: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        mcp_server_url: Optional[str] = None,
        mcp_token: Optional[str] = None,
        add_dirs: Optional[list[str]] = None,
        mcp_config_path: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        subcommand: Optional[str] = None,
    ) -> None:
        self.executable = executable or os.getenv("COPILOT_CLI_PATH", "copilot")
        self.workspace_dir = workspace_dir or os.getenv("copenclaw_WORKSPACE_DIR")
        self.timeout = timeout
        self.mcp_server_url = mcp_server_url
        self.mcp_token = mcp_token
        self.add_dirs: list[str] = add_dirs or []

        self._session_id: Optional[str] = None
        self._resume_session_id: Optional[str] = resume_session_id
        self._mcp_config_path: Optional[str] = mcp_config_path
        self._subcommand: Optional[str] = subcommand or os.getenv("COPILOT_CLI_SUBCOMMAND")
        self._initialized = False

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def resume_session_id(self) -> Optional[str]:
        return self._resume_session_id

    @resume_session_id.setter
    def resume_session_id(self, value: Optional[str]) -> None:
        self._resume_session_id = value

    # ── internal helpers ──────────────────────────────────────

    def _resolve_executable(self) -> str:
        path = shutil.which(self.executable)
        if not path:
            raise CopilotCliError("copilot CLI not found on PATH")
        return path

    def _ensure_mcp_config(self) -> str:
        """Write MCP config into the workspace directory (or .data/) and return abs path.

        The config is written into the workspace directory itself so that
        the ``@path`` reference always resolves correctly regardless of cwd.
        """
        if self._mcp_config_path and os.path.exists(self._mcp_config_path):
            return self._mcp_config_path
        if self._mcp_config_path and not self.mcp_server_url:
            return self._mcp_config_path

        url = self.mcp_server_url or "http://127.0.0.1:18790/mcp"
        target_dir = self.workspace_dir or os.getenv("copenclaw_DATA_DIR", ".data")
        self._mcp_config_path = write_mcp_config(
            target_dir=target_dir,
            mcp_server_url=url,
            mcp_token=self.mcp_token,
        )
        logger.info("MCP config ready: %s", self._mcp_config_path)
        return self._mcp_config_path

    def _base_cmd(self, resume_id: Optional[str] = None) -> list[str]:
        """Build the base command with non-interactive flags.

        If *resume_id* is provided, ``--resume <id>`` is added so Copilot CLI
        restores the conversation from a previous session instead of starting
        fresh.  When no *resume_id* is given but ``self._resume_session_id``
        is set, that value is used automatically.
        """
        exe = self._resolve_executable()
        cmd = [exe]
        if self._subcommand:
            cmd.append(self._subcommand)

        # Resume a previous session if we have a session ID
        effective_resume = resume_id or self._resume_session_id
        if effective_resume:
            cmd.extend(["--resume", effective_resume])

        # MCP config — written into the workspace directory so @path resolves
        if self.mcp_server_url or self._mcp_config_path:
            mcp_path = self._ensure_mcp_config()
            cmd.extend(["--additional-mcp-config", f"@{mcp_path}"])

        # Grant access to additional directories so Copilot can use
        # its built-in file tools (read/write/edit) instead of exec_run
        for d in self.add_dirs:
            abs_d = os.path.abspath(d)
            if os.path.isdir(abs_d):
                cmd.extend(["--add-dir", abs_d])

        # Non-interactive autonomous flags
        # --yolo enables all permissions (tools, paths, URLs) at once
        cmd.extend([
            "--yolo",
            "--no-ask-user",
            "-s",  # silent (clean output only)
        ])

        return cmd

    @staticmethod
    def _should_retry_with_chat(output: str) -> bool:
        """Detect CLI errors that indicate a missing 'chat' subcommand."""
        lowered = output.lower()
        return (
            "too many arguments" in lowered
            or "expected 0 arguments" in lowered
            or "unexpected extra argument" in lowered
            or "no such option" in lowered
        )

    def _orchestrator_log_path(self) -> str:
        return get_orchestrator_log_path()

    def _activity_log_path(self) -> str:
        return get_activity_log_path()

    def _log_line(self, line: str, prefix: str = "ORCHESTRATOR") -> None:
        """Log a single line to both the Python logger and disk log files."""
        clean = line.rstrip()
        logger.info("%s | %s", prefix, clean)
        # Write to centralized orchestrator log
        try:
            with open(self._orchestrator_log_path(), "a", encoding="utf-8") as f:
                f.write(line)
                if not line.endswith("\n"):
                    f.write("\n")
        except Exception:  # noqa: BLE001
            pass
        # Also append to unified activity log
        append_to_file(self._activity_log_path(), f"[{prefix}] {clean}")

    def _make_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _discover_latest_session_id(self) -> Optional[str]:
        """Try to discover the most-recently-modified session from Copilot CLI's data dir.

        Copilot CLI stores sessions under ``~/.copilot/session-state/``.
        Each session is a directory whose name is the session ID.  We pick
        the one with the most recent modification time.
        """
        config_dir = os.path.expanduser("~/.copilot")
        # Copilot CLI uses "session-state" for session storage
        sessions_dir = os.path.join(config_dir, "session-state")
        if not os.path.isdir(sessions_dir):
            # Fallback: older versions may use "sessions"
            sessions_dir = os.path.join(config_dir, "sessions")
        if not os.path.isdir(sessions_dir):
            logger.debug("No Copilot sessions dir found at %s", sessions_dir)
            return None
        try:
            entries = [
                e for e in os.scandir(sessions_dir)
                if e.is_dir()
            ]
            if not entries:
                return None
            latest = max(entries, key=lambda e: e.stat().st_mtime)
            session_id = latest.name
            logger.info("Discovered latest Copilot CLI session: %s", session_id)
            return session_id
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to discover session ID: %s", exc)
            return None

    # ── public API ────────────────────────────────────────────

    def create_session(self, context: str = "", allow_retry: bool = True) -> str:
        """Bootstrap a brain session (validates CLI works). Returns the response.

        Sends a greeting with optional context (e.g. README.md contents);
        system instructions come from .github/copilot-instructions.md in
        the workspace directory.
        """
        logger.info("Creating Copilot CLI brain session...")
        cmd = self._base_cmd()

        if context:
            boot_prompt = (
                "Hello! You are coming online. Here is the current workspace README.md "
                "so you understand what projects and tasks have been done:\n\n"
                f"{context}\n\n"
                "Please confirm you are online and ready."
            )
        else:
            boot_prompt = "Hello! Please confirm you are online and ready."

        cmd.extend(["-p", boot_prompt])

        try:
            result = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._make_env(),
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise CopilotCliError(
                f"copilot CLI timed out during session creation after {self.timeout}s"
            ) from exc

        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            error_text = stderr or output
            if allow_retry and not self._subcommand and self._should_retry_with_chat(error_text):
                logger.warning("copilot CLI rejected args; retrying with 'chat' subcommand")
                self._subcommand = "chat"
                return self.create_session(context=context, allow_retry=False)
            logger.error("Session creation failed: %s", error_text)
            raise CopilotCliError(error_text or "copilot CLI session creation failed")

        self._initialized = True
        logger.info("Brain session created. Response: %s", output[:200])
        return output

    def run_prompt(
        self,
        prompt: str,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        log_prefix: str = "ORCHESTRATOR",
        resume_id: Optional[str] = None,
        allow_retry: bool = True,
    ) -> str:
        """Send a user prompt to Copilot CLI with streaming output.

        If *resume_id* is given (or ``self._resume_session_id`` is set),
        the session is resumed via ``--resume`` so Copilot CLI maintains
        its own conversation context natively — no need to prepend history.

        System instructions come from .github/copilot-instructions.md in
        the workspace dir.  Only the user's actual message is passed
        via ``-p``.  Output is streamed line-by-line.
        """
        cmd = self._base_cmd(resume_id=resume_id)

        # Pass ONLY the user message — system instructions are in the file
        cmd.extend(["-p", prompt])

        if model:
            cmd.extend(["--model", model])

        effective_cwd = cwd or self.workspace_dir

        # Log the inbound prompt BEFORE starting the subprocess
        logger.info("%s ← %s", log_prefix, prompt[:300])
        try:
            with open(self._orchestrator_log_path(), "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"USER: {prompt}\n")
                f.write(f"{'='*60}\n")
        except Exception:  # noqa: BLE001
            pass

        # Use Popen for streaming output
        try:
            process = subprocess.Popen(
                cmd,
                cwd=effective_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # merge stderr into stdout
                text=True,
                env=self._make_env(),
                encoding="utf-8",
                errors="replace",
                # On Windows, CREATE_NEW_PROCESS_GROUP avoids "Terminate batch job" prompt
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
        except FileNotFoundError as exc:
            raise CopilotCliError(f"copilot CLI not found: {exc}") from exc

        # Stream output line-by-line
        output_lines: list[str] = []
        start_time = time.monotonic()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                elapsed = time.monotonic() - start_time
                if elapsed > self.timeout:
                    process.kill()
                    raise CopilotCliError(
                        f"copilot CLI timed out after {self.timeout}s"
                    )
                output_lines.append(line)
                self._log_line(line, prefix=log_prefix)

            process.wait(timeout=10)
        except CopilotCliError:
            raise
        except Exception as exc:
            process.kill()
            raise CopilotCliError(f"copilot CLI error: {exc}") from exc

        output = "".join(output_lines).strip()

        if process.returncode != 0:
            if allow_retry and not self._subcommand and self._should_retry_with_chat(output):
                logger.warning("copilot CLI rejected args; retrying with 'chat' subcommand")
                self._subcommand = "chat"
                return self.run_prompt(
                    prompt,
                    model=model,
                    cwd=cwd,
                    log_prefix=log_prefix,
                    resume_id=resume_id,
                    allow_retry=False,
                )
            if not output:
                raise CopilotCliError(
                    f"copilot CLI failed with exit code {process.returncode}"
                )

        # Log completion
        logger.info("%s → complete (%d chars)", log_prefix, len(output))

        self._initialized = True
        return output

    def version(self) -> str:
        """Return copilot CLI version string."""
        exe = self._resolve_executable()
        try:
            result = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip()
        except Exception as exc:
            raise CopilotCliError(f"failed to get version: {exc}") from exc
