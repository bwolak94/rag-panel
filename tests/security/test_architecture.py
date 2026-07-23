"""Architecture enforcement tests.

Verify that import boundaries defined in ADR-1 are respected by
statically scanning the AST of every Python file in src/.
"""

from __future__ import annotations

import ast
import pathlib


def test_no_qdrant_client_outside_retrieval_module() -> None:
    """AsyncQdrantClient must not be imported outside src/retrieval/.

    Any module outside src/retrieval/ that imports qdrant_client directly
    violates ADR-1 (single-access-point for Qdrant) and breaks tenant isolation.
    The only permitted exception is src/api/dependencies/retrieval.py which
    re-exports the client type for FastAPI dependency typing — but even that
    module should only reference AsyncQdrantClient for the type annotation,
    never for constructing a new client.

    NOTE: src/api/dependencies/retrieval.py imports AsyncQdrantClient solely
    for the return type annotation of get_qdrant_client(). This is the one
    permitted exception — it does NOT instantiate the client itself.
    """
    repo_root = pathlib.Path(__file__).parents[2]
    src_root = repo_root / "src"
    # Files allowed to import qdrant_client directly (outside src/retrieval/).
    # src/main.py: initializes AsyncQdrantClient once in app lifespan (app factory).
    # src/api/dependencies/retrieval.py: type annotation for FastAPI dependency only.
    allowed_exceptions = {
        "src/api/dependencies/retrieval.py",
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

    assert not violations, (
        f"qdrant_client imported outside src/retrieval/ in: {violations}"
    )
