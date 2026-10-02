import os
from pathlib import Path

from app.sandbox.snapshot import workspace_files


def test_the_snapshot_reads_every_file_and_skips_tool_caches(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "add.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "pkg" / "util.py").write_text("X = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "add.cpython-313.pyc").write_bytes(b"\x00\x01")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "README.md").write_text("cache")
    assert workspace_files(tmp_path) == {
        "add.py": "def add(a, b):\n    return a + b\n",
        "pkg/util.py": "X = 1\n",
    }


def test_a_symlink_out_of_the_workspace_is_named_and_never_read(tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("host secret")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.symlink(secret, workspace / "leak.txt")
    os.symlink(tmp_path, workspace / "up")
    files = workspace_files(workspace)
    assert files == {"leak.txt": f"<symlink to {secret}>", "up": f"<symlink to {tmp_path}>"}
    assert "host secret" not in "".join(files.values())


def test_a_big_file_is_cut_and_bytes_that_are_not_text_are_replaced(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 100)
    (tmp_path / "blob.bin").write_bytes(b"ok\xff")
    files = workspace_files(tmp_path, max_bytes=10)
    assert files["big.txt"] == "x" * 10
    assert files["blob.bin"] == "ok�"
