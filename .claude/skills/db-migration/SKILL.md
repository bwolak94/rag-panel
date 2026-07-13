---
name: db-migration
description: Creates an Alembic migration following project conventions (one logical change, downgrade, tenant_id). Use for every database schema change.
---

# Alembic Migration — procedure

1. Read `docs/04-Model-Danych.md` — verify the schema change is described there. If not, update the document first.
2. Check current migration state: `alembic current` and `alembic heads` (no divergence before starting).
3. Schema rules to verify before generating:
   - Every new business table must have a `tenant_id UUID NOT NULL` column with an index.
   - Foreign keys to `tenants.id` with `ON DELETE CASCADE` (or a justified exception in an ADR).
   - No PII columns without justification in `docs/05-Bezpieczenstwo-RODO.md`.
   - Table names snake_case, column names snake_case.
4. Generate the migration: `alembic revision --autogenerate -m "<change_description>"`.
5. Review the generated file in `src/db/migrations/versions/` — autogenerate misses some changes (types, check constraints, partial indexes); fill them in manually.
6. **Required:** implement `downgrade()` — every migration must be reversible.
7. Test both directions: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`.
8. One logical change = one migration. If you have multiple unrelated changes, split into separate migrations.
9. Update `docs/04-Model-Danych.md` if the final schema differs from the earlier description.
