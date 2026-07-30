"""Unit tests for SQLAlchemy ORM models (no database required)."""

import pytest
from sqlalchemy import inspect

from src.db.models import (
    AuditLog,
    Base,
    ChunksRegistry,
    Collection,
    CollectionAccess,
    Conversation,
    Document,
    Feedback,
    IngestionJob,
    Message,
    MessageSource,
    ModelsRegistry,
    Permission,
    RagPipeline,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
    UserTenant,
)

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

ALL_MODELS = [
    Tenant,
    User,
    UserTenant,
    Role,
    Permission,
    RolePermission,
    UserRole,
    Collection,
    CollectionAccess,
    Document,
    IngestionJob,
    ChunksRegistry,
    ModelsRegistry,
    RagPipeline,
    Conversation,
    Message,
    MessageSource,
    Feedback,
    AuditLog,
]

# Tables intentionally exempted from the `tenant_id` requirement (per data model spec).
TENANT_ID_EXEMPTIONS = {
    "tenants",  # root entity — is the tenant
    "users",  # global identity table
    "user_tenants",  # join table; no business data
    "user_roles",  # join table
    "message_sources",  # derives tenant via messages → conversations
    "messages",  # derives tenant via conversations
    "collection_access",  # derives tenant via collections
    "permissions",  # global permission dictionary
    "role_permissions",  # join table
}


def _column_names(model: type) -> set[str]:
    mapper = inspect(model)
    return {col.key for col in mapper.column_attrs}


def _unique_constraints(model: type) -> list:
    table = model.__table__  # type: ignore[attr-defined]
    return [c for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"]


# ------------------------------------------------------------------ #
# Tenant isolation                                                      #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("model", ALL_MODELS)
def test_business_tables_have_tenant_id(model: type) -> None:
    table_name = model.__tablename__  # type: ignore[attr-defined]
    if table_name in TENANT_ID_EXEMPTIONS:
        pytest.skip(f"{table_name} is exempt from tenant_id requirement")
    assert "tenant_id" in _column_names(model), (
        f"{model.__name__} is missing tenant_id (required for all business tables)"
    )


# ------------------------------------------------------------------ #
# users — intentionally has NO tenant_id                               #
# ------------------------------------------------------------------ #


def test_user_has_no_tenant_id() -> None:
    assert "tenant_id" not in _column_names(User), (
        "User must NOT have tenant_id — users are global entities (see data model §2.1)"
    )


def test_user_keycloak_sub_unique() -> None:
    table = User.__table__
    # unique=True on a column creates either a UniqueConstraint or a unique Index.
    for col in table.columns:
        if col.name == "keycloak_sub":
            assert col.unique, "keycloak_sub must be UNIQUE"
            return
    pytest.fail("keycloak_sub column not found on User")


# ------------------------------------------------------------------ #
# audit_log — no updated_at (append-only)                              #
# ------------------------------------------------------------------ #


def test_audit_log_has_no_updated_at() -> None:
    assert "updated_at" not in _column_names(AuditLog), (
        "audit_log must not have updated_at — it is append-only"
    )


def test_audit_log_has_created_at() -> None:
    assert "created_at" in _column_names(AuditLog)


# ------------------------------------------------------------------ #
# Document — (tenant_id, sha256) unique constraint                     #
# ------------------------------------------------------------------ #


def test_document_sha256_unique_per_tenant() -> None:
    table = Document.__table__
    constraint_col_sets = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert frozenset(["tenant_id", "sha256"]) in constraint_col_sets, (
        "Document must have UNIQUE(tenant_id, sha256)"
    )


# ------------------------------------------------------------------ #
# ChunksRegistry — qdrant_point_id unique                              #
# ------------------------------------------------------------------ #


def test_chunks_registry_qdrant_point_id_unique() -> None:
    table = ChunksRegistry.__table__
    for col in table.columns:
        if col.name == "qdrant_point_id":
            assert col.unique, "qdrant_point_id must be UNIQUE"
            return
    pytest.fail("qdrant_point_id column not found on ChunksRegistry")


# ------------------------------------------------------------------ #
# CollectionAccess — check constraint on access_level                  #
# ------------------------------------------------------------------ #


def test_collection_access_level_check_constraint() -> None:
    table = CollectionAccess.__table__
    check_names = [c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
    assert "ck_collection_access_level" in check_names


# ------------------------------------------------------------------ #
# Message — check constraint on role                                   #
# ------------------------------------------------------------------ #


def test_message_role_check_constraint() -> None:
    table = Message.__table__
    check_names = [c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
    assert "ck_messages_role" in check_names


# ------------------------------------------------------------------ #
# Feedback — check constraint on rating + unique(message_id, user_id) #
# ------------------------------------------------------------------ #


def test_feedback_rating_check_constraint() -> None:
    table = Feedback.__table__
    check_names = [c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"]
    assert "ck_feedback_rating" in check_names


def test_feedback_unique_message_user() -> None:
    table = Feedback.__table__
    constraint_col_sets = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert frozenset(["message_id", "user_id"]) in constraint_col_sets


# ------------------------------------------------------------------ #
# Base metadata — all models registered                                #
# ------------------------------------------------------------------ #


def test_all_models_registered_in_base_metadata() -> None:
    table_names = set(Base.metadata.tables.keys())
    expected = {
        "tenants",
        "users",
        "user_tenants",
        "roles",
        "permissions",
        "role_permissions",
        "user_roles",
        "collections",
        "collection_access",
        "documents",
        "ingestion_jobs",
        "chunks_registry",
        "models_registry",
        "rag_pipelines",
        "conversations",
        "messages",
        "message_sources",
        "feedback",
        "audit_log",
    }
    missing = expected - table_names
    assert not missing, f"Tables missing from Base.metadata: {missing}"


# ------------------------------------------------------------------ #
# MessageSource — no tenant_id (exempt)                                #
# ------------------------------------------------------------------ #


def test_message_source_has_no_tenant_id() -> None:
    assert "tenant_id" not in _column_names(MessageSource), (
        "MessageSource must NOT have tenant_id (derives tenant via messages → conversations)"
    )


# ------------------------------------------------------------------ #
# ON DELETE behaviour spot-checks                                       #
# ------------------------------------------------------------------ #


def test_document_uploaded_by_set_null_on_delete() -> None:
    """uploaded_by must be SET NULL (not CASCADE) — GDPR: preserve doc, anonymise uploader."""
    table = Document.__table__
    for fk in table.foreign_keys:
        if fk.parent.name == "uploaded_by":
            assert fk.ondelete == "SET NULL", (
                "Document.uploaded_by FK must be SET NULL (GDPR Art. 17)"
            )
            return
    pytest.fail("uploaded_by FK not found on Document")


def test_message_source_document_id_set_null_on_delete() -> None:
    """document_id on message_sources must be SET NULL — citation history is preserved."""
    table = MessageSource.__table__
    for fk in table.foreign_keys:
        if fk.parent.name == "document_id":
            assert fk.ondelete == "SET NULL"
            return
    pytest.fail("document_id FK not found on MessageSource")
