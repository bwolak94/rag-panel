# Coding Standards (Python)

- Python 3.12, full type hints (mypy strict for `src/`); Pydantic v2 for all DTOs.
- Formatting and linting: ruff (line-length 100). Google-style docstrings for public functions.
- Async-first: endpoints and I/O are async; no blocking calls in the event loop (CPU-bound → worker/executor).
- Dependencies: `uv` + lockfile; every new library requires justification in the PR.
- Import structure: api → domain → core; **importing from api into domain/core is forbidden** (layered architecture, enforced by import-linter test).
- Domain exceptions (`core/exceptions.py`) mapped centrally to HTTP responses; never catch bare `Exception`.
- Naming: modules snake_case, classes PascalCase; LangGraph nodes as functions `node_<name>`.
- Every LangGraph graph node: separate file, clean input/output on state (TypedDict/Pydantic), testable in isolation without LLM (LLM through an interface, mocked in tests).
- Tests: pytest, min. 80% coverage for `domain/`, `graphs/`, `retrieval/`; testcontainers for Postgres/Qdrant/MinIO/Redis; test data factories via factory-boy.
- Commits: Conventional Commits (`feat:`, `fix:`, `docs:`...); PR description references the related document from `docs/`.
- Alembic migrations: one logical change = one migration; always include `downgrade`.
