"""A hash of the code a task image is built from; standard library only, to run before a build."""

import hashlib
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def source_hash(root: Path = BACKEND_ROOT) -> str:
    """A hash of the code and lockfile a task image is built from, to tell a stale image apart."""
    digest = hashlib.sha256()
    files = [root / "pyproject.toml", root / "uv.lock", root / "Dockerfile.task"]
    for package in ("app", "bench", "fleet", "chaos"):
        files += (path for path in (root / package).rglob("*") if path.is_file())
    for path in sorted(files):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]
