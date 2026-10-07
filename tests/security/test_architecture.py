"""Architecture enforcement tests.

Verify that import boundaries defined in ADR-1 and ADR-022 are respected by
statically scanning the AST of every Python file in src/ and by running the
import-linter contracts defined in pyproject.toml.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys


def test_no_qdrant_client_outside_retrieval_module() -> None:
    """AsyncQdrantClient must not be imported outside src/retrieval/ (ADR-1, ADR-022).

    Any module outside src/retrieval/ that imports qdrant_client directly
    violates ADR-1 (single-access-point for Qdrant) and breaks tenant isolation.

    Only permitted exception: src/main.py initializes AsyncQdrantClient once in
    the app lifespan (app factory pattern). This is unavoidable and documented.
    """
    repo_root = pathlib.Path(__file__).parents[2]
    src_root = repo_root / "src"
    # Only src/main.py may import qdrant_client outside src/retrieval/.
    # src/api/dependencies/retrieval.py no longer imports qdrant_client (ADR-022).
    allowed_exceptions = {
        "src/main.py",
    }
    violations: list[str] = []

    for py_file in src_root.rglob("*.py"):
        if "retrieval" in py_file.parts:
            continue
        file_str = str(py_file.relative_to(repo_root))
        if file_str in allowed_exceptions:
            continue
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "qdrant_client" in module:
                    violations.append(file_str)
                    break
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "qdrant_client" in alias.name:
                        violations.append(file_str)
                        break

    assert not violations, f"qdrant_client imported outside src/retrieval/ in: {violations}"


def test_import_linter_contracts_all_pass() -> None:
    """All import-linter contracts defined in pyproject.toml must pass (ADR-022).

    This test runs `lint-imports` as a subprocess so that CI failures are
    surfaced in the test suite as well as the lint step.  A broken contract
    means either a new architectural violation was introduced or the
    ignore_imports list needs updating.
    """
    repo_root = pathlib.Path(__file__).parents[2]
    # Use the lint-imports entry point from the same venv as the test runner.
    # import-linter does not expose a -m entry point; only the CLI binary works.
    lint_imports_bin = pathlib.Path(sys.executable).parent / "lint-imports"
    result = subprocess.run(
        [str(lint_imports_bin)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"import-linter contracts broken:\n{result.stdout}\n{result.stderr}"
    )
