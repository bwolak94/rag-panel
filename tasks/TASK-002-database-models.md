# TASK-002: Database Models and Initial Migration

**Status:** TODO
**Priority:** P0 — required before TASK-003 through TASK-007
**Owner:** backend-dev
**Reviewer:** python-reviewer, data-engineer
**Related docs:** `docs/data-model.md` §2 | `docs/architecture.md` §5, §7
**Estimated effort:** 3–4 days

---

## Overview

Implement all SQLAlchemy 2.x ORM models for the 18 database tables defined in `docs/data-model.md`, plus the Alembic initial migration that creates the full schema in a single migration. This task covers all entity types: identity and access control tables, knowledge and ingest tables, conversation tables, and system tables.

This task also implements the `updated_at` auto-update trigger, the `audit_log` range partition by month, named indexes matching the architecture doc's index strategy, and the LangGraph checkpoint table acknowledgment (those tables are managed by the `langgraph-checkpoint-postgres` library, not by this migration).

## Usage

All other tasks import models from `src/db/models/`. The `get_db_session` dependency (from TASK-001) provides `AsyncSession` to repositories. No repository logic lives in this task — models are pure data definitions. Alembic migrations run in CI and on deployment via `alembic upgrade head`.

**Import pattern:** `from src.db.models import Tenant, User, Collection, Document` (single `__init__.py` re-exports all models).

## Tech Stack

- **SQLAlchemy 2.0+** — `mapped_column()`, `Mapped[T]`, `DeclarativeBase` (not the legacy 1.x `Column` API)
- **asyncpg 0.30+** — async Postgres driver
- **Alembic 1.14+** — database migration tool; async-compatible env setup
- **PostgreSQL 16** — range partitioning for `audit_log`, GIN indexes for JSONB, `gen_random_uuid()` for PKs
- `sqlalchemy.dialects.postgresql` — for `UUID`, `JSONB`, `INET`, `ARRAY`, `TEXT` types specific to Postgres

**Why SQLAlchemy 2.x patterns:** `Mapped[T]` provides full type inference for mypy without extra plugins. `mapped_column()` is explicit and avoids ambiguity from legacy `Column()`. `DeclarativeBase` replaces the `declarative_base()` factory for cleaner inheritance.

## Database Patterns

### Base Model

```python
# src/db/models/base.py
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """All models inherit from this base."""
    type_annotation_map: dict[type, Any] = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


class TimestampMixin:
    """Adds created_at and updated_at columns managed by DB trigger."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

The `updated_at` trigger is also set at the Postgres level (in the migration) so direct SQL updates are captured:

```sql
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

Applied to every table with `updated_at` via migration.

### Identity and Access Models

```python
# src/db/models/tenant.py
import uuid
from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.models.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'active'")
    )

    # Relationships (not loaded by default — use explicit joins)
    collections: Mapped[list["Collection"]] = relationship(back_populates="tenant", lazy="raise")
    user_tenants: Mapped[list["UserTenant"]] = relationship(back_populates="tenant", lazy="raise")
```

```python
# src/db/models/user.py
import uuid
from sqlalchemy import Boolean, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    keycloak_sub: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
```

```python
# src/db/models/user_tenant.py
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.db.models.base import Base


class UserTenant(Base):
    """Join table: users <-> tenants membership."""
    __tablename__ = "user_tenants"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_user_tenants"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(lazy="raise")
    tenant: Mapped["Tenant"] = relationship(back_populates="user_tenants", lazy="raise")
```

```python
# src/db/models/role.py
import uuid
from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base, TimestampMixin


class Role(Base, TimestampMixin):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column()
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
```

```python
# src/db/models/permission.py
import uuid
from datetime import datetime
from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"
    # (role_id, permission_id) unique constraint prevents duplicate grants
    __table_args__ = (
        __import__("sqlalchemy").UniqueConstraint(
            "role_id", "permission_id", name="uq_role_permissions"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        __import__("sqlalchemy").UniqueConstraint("user_id", "role_id", name="uq_user_roles"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

### Knowledge and Ingest Models

```python
# src/db/models/collection.py
import uuid
from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base, TimestampMixin


class Collection(Base, TimestampMixin):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    embedding_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models_registry.id"), nullable=False, index=True
    )
    chunk_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text(
            '\'{"strategy": "recursive", "chunk_size": 512, "overlap": 64, "min_chunk_size": 64}\''
        ),
    )
    validation_config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text('\'{"confidence_threshold": 0.7, "require_review": false}\''),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class CollectionAccess(Base):
    """Role-to-collection access grant."""
    __tablename__ = "collection_access"
    __table_args__ = (
        __import__("sqlalchemy").UniqueConstraint(
            "collection_id", "role_id", name="uq_collection_access"
        ),
        __import__("sqlalchemy").CheckConstraint(
            "access_level IN ('read', 'write')", name="ck_collection_access_level"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    access_level: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

```python
# src/db/models/document.py
import uuid
from sqlalchemy import ARRAY, BigInteger, CheckConstraint, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base, TimestampMixin


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256", name="idx_documents_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'uploaded'"), index=True
    )
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    language: Mapped[str | None] = mapped_column(String(10), index=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    validation_result: Mapped[dict | None] = mapped_column(JSONB)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

```python
# src/db/models/ingestion_job.py
import uuid
from sqlalchemy import ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base, TimestampMixin


class IngestionJob(Base, TimestampMixin):
    __tablename__ = "ingestion_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'pending'"), index=True
    )
    current_step: Mapped[str | None] = mapped_column(String(50))
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    langgraph_thread_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

```python
# src/db/models/chunks_registry.py
import uuid
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base


class ChunksRegistry(Base):
    __tablename__ = "chunks_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    qdrant_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(500))
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

### Conversation and Model Registry

```python
# src/db/models/models_registry.py
import uuid
from sqlalchemy import ARRAY, Boolean, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base, TimestampMixin


class ModelsRegistry(Base, TimestampMixin):
    __tablename__ = "models_registry"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), index=True
    )  # NULL = global model
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # llm | embedding
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    allowed_roles: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("'{}'")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), index=True)
```

### System Tables

```python
# src/db/models/audit_log.py
import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.db.models.base import Base


class AuditLog(Base):
    """
    Append-only audit log. Partitioned RANGE(created_at) — one partition per month.
    NEVER issue UPDATE or DELETE against this table.
    Partitions are named: audit_log_YYYY_MM (e.g., audit_log_2026_07).
    The parent table is non-inheriting; all data lives in child partitions.
    """
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_log_tenant_created", "tenant_id", "created_at"),
        Index("idx_audit_log_user_id", "user_id"),
        Index("idx_audit_log_action", "action"),
        Index("idx_audit_log_resource_type", "resource_type"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ip: Mapped[str | None] = mapped_column(INET)
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
```

### Module `__init__.py` Re-export

```python
# src/db/models/__init__.py
from src.db.models.audit_log import AuditLog
from src.db.models.base import Base, TimestampMixin
from src.db.models.chunks_registry import ChunksRegistry
from src.db.models.collection import Collection, CollectionAccess
from src.db.models.document import Document
from src.db.models.feedback import Feedback
from src.db.models.ingestion_job import IngestionJob
from src.db.models.message import Message, MessageSource
from src.db.models.models_registry import ModelsRegistry
from src.db.models.permission import Permission, RolePermission, UserRole
from src.db.models.rag_pipeline import RagPipeline
from src.db.models.role import Role
from src.db.models.tenant import Tenant
from src.db.models.user import User
from src.db.models.user_tenant import UserTenant
from src.db.models.conversation import Conversation

__all__ = [
    "AuditLog", "Base", "TimestampMixin", "ChunksRegistry",
    "Collection", "CollectionAccess", "Conversation", "Document",
    "Feedback", "IngestionJob", "Message", "MessageSource",
    "ModelsRegistry", "Permission", "RagPipeline", "Role",
    "RolePermission", "Tenant", "User", "UserRole", "UserTenant",
]
```

### Alembic Configuration

```python
# src/db/migrations/env.py
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.config import settings
from src.db.models import Base  # Imports all models, populating metadata

config = context.config
config.set_main_option("sqlalchemy.url", str(settings.DATABASE_URL))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=str(settings.DATABASE_URL),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=__import__("sqlalchemy.pool", fromlist=["NullPool"]).NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

## API Contracts

No HTTP endpoints in this task. Models are used by repository classes, not exposed directly.

## Architecture — SOLID & DRY

### Layered Access — What Not to Do

- Routers MUST NOT import SQLAlchemy models directly. They import Pydantic schemas.
- Services MUST NOT write raw SQL. They call repository methods.
- Repositories receive `AsyncSession` via dependency injection and return domain types or ORM models.
- The `Base` metadata is imported by `env.py` for Alembic — this is the only place where all model imports are forced.

### Repository Pattern (set up in this task, implemented in later tasks)

```python
# src/db/repositories/base.py
from typing import Generic, TypeVar
from uuid import UUID
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, model_class: type[ModelT], id: UUID) -> ModelT | None:
        return await self._session.get(model_class, id)

    async def add(self, instance: ModelT) -> ModelT:
        self._session.add(instance)
        await self._session.flush()  # Flush but don't commit — caller manages transaction
        return instance
```

### JSONB Column Contracts

JSONB columns have documented schemas. These schemas are enforced at the Pydantic layer (request/response DTOs), not at the Postgres layer (too rigid for evolving schemas).

| Column | Python type hint | Validated by |
|---|---|---|
| `tenants.settings` | `TenantSettings` Pydantic model | `PATCH /tenants/{id}` schema |
| `collections.chunk_config` | `ChunkConfig` Pydantic model | `POST /collections` schema |
| `collections.validation_config` | `ValidationConfig` Pydantic model | `POST /collections` schema |
| `documents.validation_result` | `ValidationResult` Pydantic model | Ingest graph output |
| `ingestion_jobs.steps` | `list[StepRecord]` Pydantic model | Ingest graph writer |
| `rag_pipelines.prompt_config` | `PromptConfig` Pydantic model | `POST /pipelines` schema |
| `rag_pipelines.guardrails` | `GuardrailsConfig` Pydantic model | `POST /pipelines` schema |

## Implementation Steps

1. **Create directory structure**
   ```
   src/db/
     __init__.py
     models/
       __init__.py
       base.py
       tenant.py
       user.py
       user_tenant.py
       role.py
       permission.py
       collection.py
       document.py
       ingestion_job.py
       chunks_registry.py
       models_registry.py
       rag_pipeline.py
       conversation.py
       message.py
       feedback.py
       audit_log.py
     repositories/
       __init__.py
       base.py
     migrations/
       env.py
       versions/
   ```

2. **Implement `src/db/models/base.py`** — `Base`, `TimestampMixin`

3. **Implement all model files** in the order: models without FKs first, then dependent ones
   - Order: `ModelsRegistry` → `Tenant` → `User` → `Role` → `Permission` → `UserTenant` → `RolePermission` → `UserRole` → `Collection` → `CollectionAccess` → `Document` → `IngestionJob` → `ChunksRegistry` → `RagPipeline` → `Conversation` → `Message` → `MessageSource` → `Feedback` → `AuditLog`

4. **Implement `src/db/models/__init__.py`** re-exporting all models

5. **Configure Alembic:** `alembic init src/db/migrations` → update `env.py` as shown → update `alembic.ini` script_location

6. **Generate initial migration:** `alembic revision --autogenerate -m "initial_schema"`

7. **Edit the generated migration** to add:
   - `update_updated_at_column()` trigger function
   - Trigger applications for all tables with `updated_at`
   - `audit_log` partitioning DDL (Alembic does not autogenerate partitions)
   - Initial partition for current month: `audit_log_2026_07`
   - Seed data for `permissions` table with all permission codes
   - Note in migration comments about LangGraph checkpoint tables (managed by library)

8. **Add partition creation to migration `upgrade()`:**
   ```python
   def upgrade() -> None:
       # ... autogenerated DDL ...
       op.execute("""
           CREATE TABLE IF NOT EXISTS audit_log_2026_07
           PARTITION OF audit_log
           FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');
       """)
       op.execute("""
           CREATE TABLE IF NOT EXISTS audit_log_2026_08
           PARTITION OF audit_log
           FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
       """)
   ```

9. **Implement `downgrade()`** for the migration — drop all tables in reverse FK order

10. **Verify:** `alembic upgrade head` runs without error against a fresh Postgres 16 container

11. **Implement `src/db/repositories/base.py`** — `BaseRepository[T]` generic class

12. **Run `mypy src/db/ && ruff check --fix src/db/`**

## Security Checklist

- `audit_log` has no `UPDATE`/`DELETE` ORM methods — the repository exposes `add()` only
- `tenant_id` is present on every business table (verified by test asserting all ORM models have the column, except the documented exemptions)
- `validation_result` JSONB column: no ORM event listener logs its content; documented in model docstring as GDPR-sensitive
- `messages.content` TEXT column: documented as GDPR-sensitive; no ORM event listener logs it
- Foreign key `ON DELETE` behavior matches GDPR cascade requirements:
  - `documents` → `ingestion_jobs`: CASCADE
  - `documents` → `chunks_registry`: CASCADE
  - `messages` → `message_sources`: CASCADE
  - `users` → `documents.uploaded_by`: SET NULL (GDPR: preserve document, anonymize uploader)
- `users` table has no `tenant_id` column — this is intentional (global users); verified by test

## Terms of Use (relevant constraints)

- Partitioned `audit_log` table: records MUST NOT be deleted (GDPR exemption: audit log for medical data has minimum 24-month retention). Partition drops by the retention worker are the only deletion mechanism.
- `messages.content` and `documents.validation_result` are classified as GDPR personal data categories — deletion must be cascaded as per `DeletionService` (TASK-007).
- LangGraph checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) are created by the `langgraph-checkpoint-postgres` library via its own migration. Do NOT include them in Alembic migrations — call `await checkpointer.setup()` at application startup instead.

## Tests

References TASK-016. Specific test cases for this task:

**`tests/unit/test_models.py`**
- All business tables have `tenant_id` column (except documented exemptions: `users`, `user_tenants`, `user_roles`, `message_sources`, `permissions`, `role_permissions`)
- `audit_log` model has no `updated_at` column
- `User` model has `keycloak_sub` unique constraint
- `Document` model has `(tenant_id, sha256)` unique constraint
- `ChunksRegistry.qdrant_point_id` has unique constraint

**`tests/integration/test_migrations.py`** (requires Postgres testcontainer)
- `alembic upgrade head` completes without error on fresh database
- `alembic downgrade base` removes all tables
- Round-trip: upgrade → downgrade → upgrade succeeds
- All expected indexes exist after migration (query `pg_indexes`)
- `audit_log` is partitioned (query `pg_partitions`)
- `permissions` seed data is present

**`tests/integration/test_model_crud.py`** (requires Postgres testcontainer)
- Insert and select for each model
- `updated_at` trigger fires on UPDATE
- `ON DELETE CASCADE` cascades from `Document` to `ChunksRegistry`
- `ON DELETE SET NULL` on `Document.uploaded_by` when user is deleted
- `UNIQUE(tenant_id, sha256)` raises `IntegrityError` on duplicate

## Definition of Done

- [ ] All 18 tables implemented as SQLAlchemy 2.x models (`Mapped[T]`, `mapped_column()`)
- [ ] `Base` imported from `src.db.models` and all models registered in its metadata
- [ ] Alembic initial migration generates and applies without error
- [ ] `audit_log` table is `PARTITION BY RANGE(created_at)` with at least 2 monthly partitions
- [ ] `updated_at` DB trigger applied to all tables with that column
- [ ] `permissions` seed data (all 11 permission codes) inserted in migration
- [ ] All unique constraints and named indexes present after migration
- [ ] `ON DELETE` behaviors match data model spec (CASCADE vs SET NULL)
- [ ] `alembic downgrade base` cleans up completely
- [ ] `mypy src/db/ && ruff check src/db/` exit zero
- [ ] Integration tests for migrations and basic CRUD pass
