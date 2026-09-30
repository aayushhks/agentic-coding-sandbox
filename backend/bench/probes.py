"""Tasks that exist only to exercise the harness's unhappy paths."""

from app.benchmark.schema import Task, TaskCategory, TaskDifficulty, TaskMetadata

# the hidden tests demand two different results for the same input, so no implementation passes
UNSATISFIABLE_SPEC = Task(
    metadata=TaskMetadata(
        id="unsatisfiable_spec",
        title="Normalize user names",
        description=(
            "normalize(name) in names.py should strip surrounding whitespace and lowercase the "
            "name, but it currently returns the input unchanged. Fix it."
        ),
        category=TaskCategory.BUGFIX,
        difficulty=TaskDifficulty.EASY,
        tags=["expected-fail"],
    ),
    workspace_files={"names.py": "def normalize(name):\n    return name\n"},
    test_files={
        "test_names.py": (
            "# deliberately contradictory: no implementation can pass both tests\n"
            "from names import normalize\n\n\n"
            "def test_strips_and_lowercases():\n"
            '    assert normalize("  Alice ") == "alice"\n\n\n'
            "def test_keeps_case():\n"
            '    assert normalize("  Alice ") == "Alice"\n'
        ),
    },
    reference_files={"names.py": "def normalize(name):\n    return name.strip().lower()\n"},
)
