"""Git-based update checker and applier for COpenClaw."""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("copenclaw.updater")


@dataclass
class UpdateInfo:
    """Information about an available update."""

    commits_behind: int = 0
    current_hash: str = ""
    remote_hash: str = ""
    changed_files: list[str] = field(default_factory=list)
    locally_modified: list[str] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflict_files) > 0


@dataclass
class UpdateResult:
    """Result of applying an update."""

    success: bool = False
    old_hash: str = ""
    new_hash: str = ""
    error: str = ""
    files_updated: list[str] = field(default_factory=list)
    pip_output: str = ""


def _run_git(repo_dir: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given repo directory."""
    cmd = ["git", "-C", repo_dir] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def _resolve_repo_root() -> str:
    """Resolve the COpenClaw repo root directory."""
    env_root = os.getenv("copenclaw_REPO_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", ".."))


def is_git_repo(repo_dir: str | None = None) -> bool:
    """Check if the given directory is a git repository."""
    if repo_dir is None:
        repo_dir = _resolve_repo_root()
    try:
        result = _run_git(repo_dir, "rev-parse", "--is-inside-work-tree", check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_current_hash(repo_dir: str | None = None) -> str:
    """Get the current HEAD commit hash."""
    if repo_dir is None:
        repo_dir = _resolve_repo_root()
    try:
        result = _run_git(repo_dir, "rev-parse", "HEAD")
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def get_locally_modified_files(repo_dir: str | None = None) -> list[str]:
    """Get list of files with uncommitted local changes."""
    if repo_dir is None:
        repo_dir = _resolve_repo_root()
    try:
        # Include both staged and unstaged changes
        result = _run_git(repo_dir, "status", "--porcelain")
        files = []
        for line in result.stdout.strip().splitlines():
            if not line or len(line) < 4:
                continue
            # Porcelain format: XY<space>filename  (first 3 chars are status)
            fname = line[3:]
            # Handle renames: "R  old -> new"
            if " -> " in fname:
                fname = fname.split(" -> ")[1]
            files.append(fname)
        return files
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []


def check_for_updates(repo_dir: str | None = None) -> UpdateInfo | None:
    """Check if updates are available from the remote.

    Returns None if up-to-date or if not a git repo.
    Returns UpdateInfo with details about the available update.
    """
    if repo_dir is None:
        repo_dir = _resolve_repo_root()

    if not is_git_repo(repo_dir):
        logger.debug("Not a git repo: %s", repo_dir)
        return None

    # Determine the default branch
    branch = _get_default_branch(repo_dir)

    try:
        # Fetch latest from remote
        _run_git(repo_dir, "fetch", "origin", branch, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("git fetch failed")
        return None

    try:
        current_hash = get_current_hash(repo_dir)

        # Get remote hash
        result = _run_git(repo_dir, "rev-parse", f"origin/{branch}", check=False)
        if result.returncode != 0:
            logger.debug("Could not resolve origin/%s", branch)
            return None
        remote_hash = result.stdout.strip()

        if current_hash == remote_hash:
            return None  # Up to date

        # Count commits behind
        result = _run_git(
            repo_dir, "rev-list", "--count", f"HEAD..origin/{branch}", check=False
        )
        commits_behind = int(result.stdout.strip()) if result.returncode == 0 else 0

        if commits_behind == 0:
            return None

        # Get list of changed files between HEAD and remote
        result = _run_git(
            repo_dir, "diff", "--name-only", f"HEAD..origin/{branch}", check=False
        )
        changed_files = [f for f in result.stdout.strip().splitlines() if f]

        # Get locally modified files
        locally_modified = get_locally_modified_files(repo_dir)

        # Find conflicts: files that are both locally modified and changed in the update
        conflict_files = sorted(set(locally_modified) & set(changed_files))

        return UpdateInfo(
            commits_behind=commits_behind,
            current_hash=current_hash[:12],
            remote_hash=remote_hash[:12],
            changed_files=changed_files,
            locally_modified=locally_modified,
            conflict_files=conflict_files,
        )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Update check failed: %s", exc)
        return None


def apply_update(repo_dir: str | None = None) -> UpdateResult:
    """Apply the update by pulling from remote and reinstalling.

    Returns an UpdateResult with success/failure details.
    """
    if repo_dir is None:
        repo_dir = _resolve_repo_root()

    old_hash = get_current_hash(repo_dir)
    branch = _get_default_branch(repo_dir)

    try:
        # Pull from remote
        result = _run_git(repo_dir, "pull", "origin", branch, check=False)
        if result.returncode != 0:
            return UpdateResult(
                success=False,
                old_hash=old_hash[:12],
                error=f"git pull failed: {result.stderr.strip() or result.stdout.strip()}",
            )

        new_hash = get_current_hash(repo_dir)

        # Get list of files that changed
        diff_result = _run_git(
            repo_dir, "diff", "--name-only", f"{old_hash}..{new_hash}", check=False
        )
        files_updated = [f for f in diff_result.stdout.strip().splitlines() if f]

        # Reinstall the package
        pip_output = ""
        try:
            pip_result = subprocess.run(
                ["pip", "install", "-e", repo_dir, "--quiet"],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            pip_output = pip_result.stdout + pip_result.stderr
            if pip_result.returncode != 0:
                return UpdateResult(
                    success=False,
                    old_hash=old_hash[:12],
                    new_hash=new_hash[:12],
                    error=f"pip install failed: {pip_output.strip()}",
                    files_updated=files_updated,
                    pip_output=pip_output,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return UpdateResult(
                success=False,
                old_hash=old_hash[:12],
                new_hash=new_hash[:12],
                error=f"pip install error: {exc}",
                files_updated=files_updated,
            )

        logger.info(
            "Update applied: %s -> %s (%d files changed)",
            old_hash[:12], new_hash[:12], len(files_updated),
        )

        return UpdateResult(
            success=True,
            old_hash=old_hash[:12],
            new_hash=new_hash[:12],
            files_updated=files_updated,
            pip_output=pip_output,
        )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return UpdateResult(
            success=False,
            old_hash=old_hash[:12],
            error=str(exc),
        )


def format_update_check(info: UpdateInfo | None) -> str:
    """Format an UpdateInfo into a human-readable message."""
    if info is None:
        return "✅ COpenClaw is up to date."

    lines = [
        f"🔄 **Update available!** ({info.commits_behind} commit{'s' if info.commits_behind != 1 else ''} behind)",
        f"   Current: `{info.current_hash}` → Latest: `{info.remote_hash}`",
    ]

    if info.changed_files:
        lines.append(f"\n📦 **{len(info.changed_files)} file{'s' if len(info.changed_files) != 1 else ''} changed:**")
        for f in info.changed_files[:20]:
            lines.append(f"  • `{f}`")
        if len(info.changed_files) > 20:
            lines.append(f"  … and {len(info.changed_files) - 20} more")

    if info.conflict_files:
        lines.append(f"\n⚠️ **Local modifications that would be overwritten:**")
        for f in info.conflict_files:
            lines.append(f"  • `{f}`")
        lines.append("\nThese files have local changes. Back them up before updating!")
    elif info.locally_modified:
        lines.append(f"\n📝 You have {len(info.locally_modified)} locally modified file(s), but none conflict with the update.")

    lines.append("\nUse `/update apply` to apply the update.")

    return "\n".join(lines)


def format_update_result(result: UpdateResult) -> str:
    """Format an UpdateResult into a human-readable message."""
    if not result.success:
        return f"❌ **Update failed:** {result.error}"

    lines = [
        f"✅ **Update applied successfully!**",
        f"   `{result.old_hash}` → `{result.new_hash}`",
    ]

    if result.files_updated:
        lines.append(f"\n📦 {len(result.files_updated)} file{'s' if len(result.files_updated) != 1 else ''} updated:")
        for f in result.files_updated[:15]:
            lines.append(f"  • `{f}`")
        if len(result.files_updated) > 15:
            lines.append(f"  … and {len(result.files_updated) - 15} more")

    lines.append("\n🔄 Restart COpenClaw to load the new code: `/restart`")

    return "\n".join(lines)


def _get_default_branch(repo_dir: str) -> str:
    """Determine the default branch name (main or master)."""
    try:
        result = _run_git(
            repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD", "--short", check=False
        )
        if result.returncode == 0:
            # Returns something like "origin/main"
            branch = result.stdout.strip()
            if "/" in branch:
                return branch.split("/", 1)[1]
            return branch
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: check if origin/main exists
    try:
        result = _run_git(repo_dir, "rev-parse", "--verify", "origin/main", check=False)
        if result.returncode == 0:
            return "main"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "main"  # Default fallback