---
name: commit
description: Creates a commit following Conventional Commits and project conventions (lint, types, tests before committing). Use instead of a manual git commit.
---

# Conventional Commit — procedure

1. Check repository state: `git status` and `git diff --staged`.
2. Run checks (all must pass before committing):
   ```bash
   ruff check --fix . && ruff format .
   mypy src/
   pytest -x -q --ignore=tests/integration --ignore=tests/eval
   ```
   If any check fails → fix it before committing.

3. Choose the commit type:
   - `feat:` — new feature (e.g. new node, endpoint, collection)
   - `fix:` — bug fix
   - `docs:` — documentation only (`docs/`, `CLAUDE.md`, docstrings)
   - `refactor:` — code change without behaviour change
   - `test:` — adding or changing tests
   - `chore:` — config, dependencies, CI
   - `security:` — security / GDPR fixes

4. Scope (optional, in parentheses): `feat(retrieval):`, `fix(ingest):`, `docs(api):`

5. Message format:
   ```
   <type>(<scope>): <imperative verb, max 72 chars>

   <optional: what and why, NOT how — max 3 sentences>

   Related document: docs/<file>.md
   ```

6. **Prohibitions:**
   - Do not commit `.env` files, secrets, or test data containing PII.
   - Never use `--no-verify`.
   - One commit = one logical change (do not bundle unrelated things).

7. Examples of valid messages:
   ```
   feat(retrieval): add score threshold filter per collection
   fix(ingest): prevent duplicate chunks on retry by using upsert
   docs(api): add missing 404 responses for cross-tenant requests
   security(auth): validate JWT audience claim in JWKS middleware
   ```
