import os

from copenclaw.core.gateway import _find_src_dir_for_restart, _prepend_pythonpath


def test_find_src_dir_for_restart_from_workspace(tmp_path) -> None:
    workspace = tmp_path / "repo"
    src_dir = workspace / "src" / "copenclaw"
    src_dir.mkdir(parents=True)

    found = _find_src_dir_for_restart(str(workspace))
    assert found == os.path.abspath(str(workspace / "src"))


def test_find_src_dir_for_restart_when_workspace_is_src(tmp_path) -> None:
    src_root = tmp_path / "src"
    (src_root / "copenclaw").mkdir(parents=True)

    found = _find_src_dir_for_restart(str(src_root))
    assert found == os.path.abspath(str(src_root))


def test_prepend_pythonpath_is_idempotent() -> None:
    env = {"PYTHONPATH": f"first{os.pathsep}second"}
    _prepend_pythonpath("new-path", env)
    _prepend_pythonpath("new-path", env)

    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == "new-path"
    assert parts.count("new-path") == 1
