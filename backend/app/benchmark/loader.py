"""Load benchmark tasks from the versioned dataset directory."""

from pathlib import Path

from app.benchmark.schema import Task, TaskMetadata

DEFAULT_BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmark"


def _read_tree(root: Path) -> dict[str, str]:
    """Read every file under ``root`` into a {relative posix path: content} mapping."""
    if not root.is_dir():
        return {}
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return files


def load_task(task_dir: Path) -> Task:
    """Load and validate a single task directory."""
    metadata = TaskMetadata.model_validate_json((task_dir / "task.json").read_text(encoding="utf-8"))
    test_files = _read_tree(task_dir / "tests")
    if not test_files:
        raise ValueError(f"task '{task_dir.name}' has no hidden tests")
    reference_files = _read_tree(task_dir / "reference")
    if not reference_files:
        raise ValueError(f"task '{task_dir.name}' has no reference solution")
    return Task(
        metadata=metadata,
        workspace_files=_read_tree(task_dir / "workspace"),
        test_files=test_files,
        reference_files=reference_files,
    )


def load_benchmark(root: Path | None = None, version: str = "v1") -> list[Task]:
    """Load every task in a benchmark version, sorted by id, rejecting duplicates."""
    base = (root or DEFAULT_BENCHMARK_ROOT) / version
    if not base.is_dir():
        raise FileNotFoundError(f"benchmark directory not found: {base}")
    tasks = [load_task(d) for d in sorted(base.iterdir()) if (d / "task.json").is_file()]
    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        raise ValueError(f"duplicate task ids: {duplicates}")
    return tasks
