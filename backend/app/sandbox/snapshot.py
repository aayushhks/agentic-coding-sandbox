"""The files an agent left in its workspace, read without following anything out of it."""

import os
from pathlib import Path

# caches the tools leave behind, which say nothing about what the agent did
_SKIPPED = {"__pycache__", ".pytest_cache"}
MAX_FILE_BYTES = 65_536


def _read(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(max_bytes).decode(errors="replace")
    except OSError as exc:
        return f"<unreadable: {exc.strerror}>"


def workspace_files(workspace: Path, *, max_bytes: int = MAX_FILE_BYTES) -> dict[str, str]:
    """Each regular file under the workspace as text, cut at max_bytes; a symlink is only named."""
    files: dict[str, str] = {}
    # os.walk does not descend into symlinked directories, and a symlink is never opened
    for root, dirs, names in os.walk(workspace):
        here = Path(root)
        for name in [*dirs]:
            if name in _SKIPPED or (here / name).is_symlink():
                dirs.remove(name)
            if (here / name).is_symlink():
                files[str((here / name).relative_to(workspace))] = (
                    f"<symlink to {os.readlink(here / name)}>"
                )
        for name in names:
            path = here / name
            relative = str(path.relative_to(workspace))
            if path.is_symlink():
                files[relative] = f"<symlink to {os.readlink(path)}>"
            elif path.is_file():
                files[relative] = _read(path, max_bytes)
    return dict(sorted(files.items()))
