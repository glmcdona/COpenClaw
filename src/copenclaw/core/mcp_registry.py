"""Manage user-installed MCP servers in Copilot CLI's config.

This module provides CRUD operations on the ``~/.copilot/mcp-config.json``
file — the same file that Copilot CLI uses natively via ``/mcp add``.

Servers added here are automatically available to brain sessions (Copilot
CLI reads its own config) and are merged into worker/supervisor sessions
by ``write_mcp_config()`` in ``copilot_cli.py``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("copenclaw.mcp_registry")

# Default location — same as Copilot CLI's own config
_DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".copilot")
_CONFIG_FILENAME = "mcp-config.json"


def _config_path() -> str:
    """Return the path to the Copilot CLI MCP config file."""
    config_dir = os.getenv("COPILOT_CONFIG_DIR", _DEFAULT_CONFIG_DIR)
    return os.path.join(config_dir, _CONFIG_FILENAME)


def _read_config() -> dict[str, Any]:
    """Read the MCP config file, returning an empty structure if missing."""
    path = _config_path()
    if not os.path.exists(path):
        return {"mcpServers": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "mcpServers" not in data:
            data["mcpServers"] = {}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read MCP config at %s: %s", path, exc)
        return {"mcpServers": {}}


def _write_config(config: dict[str, Any]) -> str:
    """Write the MCP config file, creating directories as needed. Returns path."""
    path = _config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    logger.info("Wrote MCP config: %s (%d servers)", path, len(config.get("mcpServers", {})))
    return path


# ── Public API ────────────────────────────────────────────────


def list_servers() -> dict[str, Any]:
    """Return all configured MCP servers.

    Returns a dict mapping server name → server config entry.
    """
    config = _read_config()
    return config.get("mcpServers", {})


def get_server(name: str) -> Optional[dict[str, Any]]:
    """Return a single server's config, or None if not found."""
    return list_servers().get(name)


def add_server(
    name: str,
    server_type: str,
    *,
    url: Optional[str] = None,
    command: Optional[str] = None,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    headers: Optional[dict[str, str]] = None,
    tools: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Add an MCP server to the Copilot CLI config.

    Parameters
    ----------
    name : str
        Unique server name (e.g. "playwright", "github", "fetch").
    server_type : str
        One of "http", "sse", "stdio".
    url : str, optional
        Required for http/sse type servers.
    command : str, optional
        Required for stdio type servers (the executable to run).
    args : list[str], optional
        Arguments for stdio command.
    env : dict, optional
        Environment variables for the server.
    headers : dict, optional
        HTTP headers (for http/sse servers).
    tools : list[str], optional
        Tool filter list (default: ["*"]).

    Returns
    -------
    dict
        The server entry that was written.
    """
    entry: dict[str, Any] = {"type": server_type}

    if server_type in ("http", "sse"):
        if not url:
            raise ValueError(f"url is required for {server_type} MCP servers")
        entry["url"] = url
    elif server_type == "stdio":
        if not command:
            raise ValueError("command is required for stdio MCP servers")
        entry["command"] = command
        if args:
            entry["args"] = args
    else:
        raise ValueError(f"Unsupported server type: {server_type}. Must be http, sse, or stdio.")

    if env:
        entry["env"] = env
    if headers:
        entry["headers"] = headers
    if tools:
        entry["tools"] = tools

    config = _read_config()
    config["mcpServers"][name] = entry
    _write_config(config)

    logger.info("Added MCP server '%s' (type=%s)", name, server_type)
    return entry


def remove_server(name: str) -> bool:
    """Remove an MCP server by name. Returns True if it was found and removed."""
    config = _read_config()
    if name not in config.get("mcpServers", {}):
        return False
    del config["mcpServers"][name]
    _write_config(config)
    logger.info("Removed MCP server '%s'", name)
    return True


def run_install_command(install_cmd: str, timeout: int = 120) -> str:
    """Run a package installation command (e.g. npm install -g ...).

    Returns the command output. Raises RuntimeError on failure.
    """
    logger.info("Running install command: %s", install_cmd)
    try:
        result = subprocess.run(
            install_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        if result.returncode != 0:
            raise RuntimeError(
                f"Install command failed (exit {result.returncode}): {output[:2000]}"
            )
        logger.info("Install command succeeded: %s", install_cmd)
        return output[:2000]
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Install command timed out after {timeout}s") from exc


def get_user_servers_for_merge() -> dict[str, Any]:
    """Return user-installed servers suitable for merging into task configs.

    Excludes the 'copenclaw' entry (which is managed separately by
    write_mcp_config) to avoid conflicts.
    """
    servers = list_servers()
    # Don't include copenclaw — that's managed by write_mcp_config
    servers.pop("copenclaw", None)
    return servers