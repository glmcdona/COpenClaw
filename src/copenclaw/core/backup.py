"""Hardlink-based snapshot backups of the app source code.

Each snapshot is a complete directory tree.  Files unchanged since the
previous snapshot are hardlinked (zero extra disk space on NTFS / ext4).
Files that changed get a fresh copy.  To restore, simply copy the snapshot
directory back to the source location.
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("copenclaw.backup")

# Directories / files to always skip (in addition to dot-prefixed entries)
_EXTRA_SKIP = {"__pycache__"}


def _should_skip(name: str) -> bool:
    """Return True if *name* should be excluded from backup."""
    return name.startswith(".") or name in _EXTRA_SKIP


def _snapshot_name() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _latest_snapshot(backup_root: str) -> str | None:
    """Return the path of the most recent existing snapshot, or None."""
    if not os.path.isdir(backup_root):
        return None
    entries = sorted(
        (
            e
            for e in os.listdir(backup_root)
            if os.path.isdir(os.path.join(backup_root, e)) and not e.startswith(".")
        ),
        reverse=True,
    )
    return os.path.join(backup_root, entries[0]) if entries else None


def create_snapshot(
    source_dir: str,
    backup_root: str,
    max_snapshots: int = 10,
) -> str | None:
    """Create a hardlink-based snapshot of *source_dir* under *backup_root*.

    Returns the path of the new snapshot directory, or ``None`` on failure.
    """
    source_dir = os.path.abspath(source_dir)
    backup_root = os.path.abspath(backup_root)

    if not os.path.isdir(source_dir):
        logger.warning("Backup source not found: %s", source_dir)
        return None

    os.makedirs(backup_root, exist_ok=True)

    prev_snapshot = _latest_snapshot(backup_root)
    snap_name = _snapshot_name()
    snap_dir = os.path.join(backup_root, snap_name)

    # Avoid collision (e.g. two starts in the same second)
    if os.path.exists(snap_dir):
        snap_name += "_1"
        snap_dir = os.path.join(backup_root, snap_name)

    logger.info(
        "Creating backup snapshot %s (prev=%s)",
        snap_name,
        os.path.basename(prev_snapshot) if prev_snapshot else "none",
    )

    files_copied = 0
    files_linked = 0

    for dirpath, dirnames, filenames in os.walk(source_dir):
        # Filter out dot-prefixed and __pycache__ directories in-place
        dirnames[:] = [d for d in dirnames if not _should_skip(d)]

        rel_dir = os.path.relpath(dirpath, source_dir)
        dest_dir = os.path.join(snap_dir, rel_dir)
        os.makedirs(dest_dir, exist_ok=True)

        for fname in filenames:
            if _should_skip(fname):
                continue

            src_file = os.path.join(dirpath, fname)
            dest_file = os.path.join(dest_dir, fname)

            # If we have a previous snapshot, try to hardlink unchanged files
            if prev_snapshot:
                prev_file = os.path.join(prev_snapshot, rel_dir, fname)
                if os.path.isfile(prev_file):
                    try:
                        if filecmp.cmp(src_file, prev_file, shallow=False):
                            os.link(prev_file, dest_file)
                            files_linked += 1
                            continue
                    except OSError:
                        # Hardlink failed (cross-device, permissions, etc.)
                        pass

            # Copy the file (changed or no previous snapshot)
            try:
                shutil.copy2(src_file, dest_file)
                files_copied += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to copy %s: %s", src_file, exc)

    logger.info(
        "Backup snapshot %s complete: %d copied, %d hardlinked",
        snap_name,
        files_copied,
        files_linked,
    )

    # Prune old snapshots
    _prune_snapshots(backup_root, max_snapshots)

    return snap_dir


def _prune_snapshots(backup_root: str, max_keep: int) -> None:
    """Remove oldest snapshots so at most *max_keep* remain."""
    if max_keep <= 0:
        return
    entries = sorted(
        (
            e
            for e in os.listdir(backup_root)
            if os.path.isdir(os.path.join(backup_root, e)) and not e.startswith(".")
        )
    )
    while len(entries) > max_keep:
        oldest = entries.pop(0)
        oldest_path = os.path.join(backup_root, oldest)
        logger.info("Pruning old backup snapshot: %s", oldest)
        try:
            shutil.rmtree(oldest_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to prune snapshot %s: %s", oldest, exc)