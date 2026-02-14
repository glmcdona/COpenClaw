"""Template loader for system instruction files.

Loads markdown templates from ``copenclaw/templates/`` and renders
them with Python ``str.format()`` placeholders.
"""
from __future__ import annotations

import logging
import os
import platform
from functools import lru_cache

logger = logging.getLogger("copenclaw.templates")

# templates/ lives at the repo root: copenclaw/templates/
# This file lives at:                copenclaw/src/copenclaw/core/templates.py
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))                       # .../core/
_TEMPLATES_DIR = os.path.join(_THIS_DIR, "..", "..", "..", "templates")       # .../copenclaw/templates/
_TEMPLATES_DIR = os.path.normpath(_TEMPLATES_DIR)

# ── OS-specific defaults ─────────────────────────────────────
_OS_NAME = platform.system()  # "Windows", "Linux", "Darwin"

def _os_defaults() -> dict[str, str]:
    """Return OS-specific template variables."""
    if _OS_NAME == "Windows":
        return {
            "os_name": "Windows",
            "shell_hint": "cmd.exe (use PowerShell via `powershell -Command \"...\"` if needed)",
            "read_cmd": "type",
            "list_cmd": "dir",
            "mkdir_cmd": "mkdir",
            "sep": "\\\\",
        }
    elif _OS_NAME == "Darwin":
        return {
            "os_name": "macOS",
            "shell_hint": "/bin/zsh (or /bin/bash)",
            "read_cmd": "cat",
            "list_cmd": "ls",
            "mkdir_cmd": "mkdir -p",
            "sep": "/",
        }
    else:
        return {
            "os_name": _OS_NAME or "Linux",
            "shell_hint": "/bin/bash",
            "read_cmd": "cat",
            "list_cmd": "ls",
            "mkdir_cmd": "mkdir -p",
            "sep": "/",
        }


@lru_cache(maxsize=8)
def _read_template(name: str) -> str:
    """Read a raw template file and return its contents (cached)."""
    path = os.path.join(_TEMPLATES_DIR, f"{name}.md")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Template not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def load_template(name: str, **kwargs: str) -> str:
    """Load a template by name and render placeholders.

    OS-specific variables (``os_name``, ``shell_hint``, ``read_cmd``,
    ``list_cmd``, ``mkdir_cmd``, ``sep``) are injected automatically
    but can be overridden via ``**kwargs``.

    Parameters
    ----------
    name : str
        Template name without extension: ``"orchestrator"``, ``"worker"``,
        or ``"supervisor"``.
    **kwargs : str
        Values for ``{placeholder}`` substitution (e.g. ``task_id``,
        ``prompt``, ``workspace_root``).

    Returns
    -------
    str
        Rendered template content.
    """
    raw = _read_template(name)
    # Merge OS defaults with caller-supplied values (caller wins)
    merged = {**_os_defaults(), **kwargs}
    try:
        return raw.format(**merged)
    except KeyError:
        # If the template has no placeholders at all (e.g. orchestrator),
        # or has extras we don't recognise, return raw.
        if not kwargs:
            return raw
        raise

def orchestrator_template() -> str:
    """Return the orchestrator brain system prompt (no placeholders)."""
    return load_template("orchestrator")

def worker_template(*, task_id: str, prompt: str, workspace_root: str) -> str:
    """Return rendered worker instructions for a specific task."""
    return load_template("worker", task_id=task_id, prompt=prompt, workspace_root=workspace_root)

def supervisor_template(
    *,
    task_id: str,
    prompt: str,
    worker_session_id: str,
    supervisor_instructions: str,
    workspace_root: str,
) -> str:
    """Return rendered supervisor instructions for a specific task."""
    return load_template(
        "supervisor",
        task_id=task_id,
        prompt=prompt,
        worker_session_id=worker_session_id,
        supervisor_instructions=supervisor_instructions,
        workspace_root=workspace_root,
    )

def repair_template(**kwargs: str) -> str:
    """Return rendered repair instructions for a repair run."""
    return load_template("repair", **kwargs)