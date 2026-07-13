"""initial_schema

Revision ID: 0001
Revises:
Create Date: 2026-07-13

Creates all 18 application tables, the `updated_at` trigger, audit_log monthly partitions,
and seeds the `permissions` table.

Note: LangGraph checkpoint tables (checkpoints, checkpoint_blobs, checkpoint_writes,
checkpoint_migrations) are managed by the `langgraph-checkpoint-postgres` library via
`await checkpointer.setup()` at application startup. Do NOT include them here.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All 11 permission codes defined in the data model.
PERMISSION_CODES: list[tuple[str, str]] = [
    ("documents:upload", "Upload new documents to a collection"),
    ("documents:read", "Read documents and their content"),
    ("documents:delete", "Delete any document in a collection"),
    ("documents:delete_own", "Delete only documents uploaded by the current user"),
    ("documents:manage", "Full document management (upload, edit metadata, delete)"),
    ("documents:approve", "Approve or reject documents awaiting validation"),
    ("chat:query", "Submit queries to the RAG pipeline"),
    ("admin:users", "Manage tenant users and roles"),
    ("admin:collections", "Manage collections and embedding models"),
    ("admin:models", "Manage LLM and embedding model registry"),
    ("admin:audit", "View audit logs"),
]

# Tables that have an `updated_at` column and should get the DB trigger.
UPDATED_AT_TABLES: list[str] = [
    "tenants",
    "users",
    "roles",
    "collections",
    "documents",
    "ingestion_jobs",
    "models_registry",
    "rag_pipelines",
    "conversations",
    "feedback",
]


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # models_registry (no FK deps)                                        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "models_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("endpoint_url", sa.String(500), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("allowed_roles", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), server_default=sa.text("'{}'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("type IN ('llm', 'embedding')", name="ck_models_registry_type"),
    )
    op.create_index("ix_models_registry_tenant_id", "models_registry", ["tenant_id"])
    op.create_index("ix_models_registry_type", "models_registry", ["type"])
    op.create_index("ix_models_registry_is_active", "models_registry", ["is_active"])

    # ------------------------------------------------------------------ #
    # tenants                                                              #
    # ------------------------------------------------------------------ #
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)
    op.create_index("ix_tenants_status", "tenants", ["status"])

    # Add FK from models_registry → tenants (deferred because tenants didn't exist yet above)
    op.create_foreign_key(
        "fk_models_registry_tenant_id",
        "models_registry", "tenants",
        ["tenant_id"], ["id"],
        ondelete="CASCADE",
    )

    # ------------------------------------------------------------------ #
    # users                                                                #
    # ------------------------------------------------------------------ #
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("keycloak_sub", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_keycloak_sub", "users", ["keycloak_sub"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------------ #
    # user_tenants                                                         #
    # ------------------------------------------------------------------ #
    op.create_table(
        "user_tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "tenant_id", name="uq_user_tenants"),
    )
    op.create_index("ix_user_tenants_user_id", "user_tenants", ["user_id"])
    op.create_index("ix_user_tenants_tenant_id", "user_tenants", ["tenant_id"])

    # ------------------------------------------------------------------ #
    # roles                                                                #
    # ------------------------------------------------------------------ #
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])

    # ------------------------------------------------------------------ #
    # permissions                                                          #
    # ------------------------------------------------------------------ #
    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(100), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_permissions_code", "permissions", ["code"], unique=True)

    # ------------------------------------------------------------------ #
    # role_permissions                                                     #
    # ------------------------------------------------------------------ #
    op.create_table(
        "role_permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permissions"),
    )
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"])
    op.create_index("ix_role_permissions_permission_id", "role_permissions", ["permission_id"])

    # ------------------------------------------------------------------ #
    # user_roles                                                           #
    # ------------------------------------------------------------------ #
    op.create_table(
        "user_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "role_id", name="uq_user_roles"),
    )
    op.create_index("ix_user_roles_user_id", "user_roles", ["user_id"])
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    # ------------------------------------------------------------------ #
    # collections                                                          #
    # ------------------------------------------------------------------ #
    op.create_table(
        "collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("embedding_model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("models_registry.id"), nullable=False),
        sa.Column("chunk_config", postgresql.JSONB(), nullable=False, server_default=sa.text(
            '\'{"strategy": "recursive", "chunk_size": 512, "overlap": 64, "min_chunk_size": 64}\''
        )),
        sa.Column("validation_config", postgresql.JSONB(), nullable=False, server_default=sa.text(
            '\'{"confidence_threshold": 0.7, "require_review": false}\''
        )),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),
    )
    op.create_index("ix_collections_tenant_id", "collections", ["tenant_id"])
    op.create_index("ix_collections_embedding_model_id", "collections", ["embedding_model_id"])

    # ------------------------------------------------------------------ #
    # collection_access                                                    #
    # ------------------------------------------------------------------ #
    op.create_table(
        "collection_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("collections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("roles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_level", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("collection_id", "role_id", name="uq_collection_access"),
        sa.CheckConstraint("access_level IN ('read', 'write')", name="ck_collection_access_level"),
    )
    op.create_index("ix_collection_access_collection_id", "collection_access", ["collection_id"])
    op.create_index("ix_collection_access_role_id", "collection_access", ["role_id"])

    # ------------------------------------------------------------------ #
    # documents                                                            #
    # ------------------------------------------------------------------ #
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("collections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("minio_key", sa.String(1000), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'uploaded'")),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("validation_result", postgresql.JSONB(), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "sha256", name="idx_documents_sha256"),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_collection_id", "documents", ["collection_id"])
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_category", "documents", ["category"])
    op.create_index("ix_documents_language", "documents", ["language"])
    op.create_index("ix_documents_uploaded_by", "documents", ["uploaded_by"])

    # ------------------------------------------------------------------ #
    # ingestion_jobs                                                        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("current_step", sa.String(50), nullable=True),
        sa.Column("steps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("langgraph_thread_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ingestion_jobs_tenant_id", "ingestion_jobs", ["tenant_id"])
    op.create_index("idx_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"])
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])
    op.create_index("ix_ingestion_jobs_langgraph_thread_id", "ingestion_jobs", ["langgraph_thread_id"])

    # ------------------------------------------------------------------ #
    # chunks_registry                                                      #
    # ------------------------------------------------------------------ #
    op.create_table(
        "chunks_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("qdrant_point_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.String(500), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chunks_registry_tenant_id", "chunks_registry", ["tenant_id"])
    op.create_index("idx_chunks_registry_document_id", "chunks_registry", ["document_id"])
    op.create_index("idx_chunks_registry_qdrant_point_id", "chunks_registry", ["qdrant_point_id"], unique=True)

    # ------------------------------------------------------------------ #
    # rag_pipelines                                                        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "rag_pipelines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("collection_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("llm_model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("models_registry.id"), nullable=False),
        sa.Column("prompt_config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("guardrails", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_rag_pipelines_tenant_name"),
    )
    op.create_index("ix_rag_pipelines_tenant_id", "rag_pipelines", ["tenant_id"])
    op.create_index("ix_rag_pipelines_llm_model_id", "rag_pipelines", ["llm_model_id"])
    op.create_index("ix_rag_pipelines_is_active", "rag_pipelines", ["is_active"])

    # ------------------------------------------------------------------ #
    # conversations                                                        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pipeline_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rag_pipelines.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_pipeline_id", "conversations", ["pipeline_id"])
    op.create_index("ix_conversations_is_deleted", "conversations", ["is_deleted"])

    # ------------------------------------------------------------------ #
    # messages                                                             #
    # ------------------------------------------------------------------ #
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("models_registry.id", ondelete="SET NULL"), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_messages_role"),
    )
    op.create_index("idx_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    # ------------------------------------------------------------------ #
    # message_sources                                                      #
    # ------------------------------------------------------------------ #
    op.create_table(
        "message_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chunks_registry.id", ondelete="SET NULL"), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("highlight_text", sa.Text(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_message_sources_message_id", "message_sources", ["message_id"])
    op.create_index("ix_message_sources_document_id", "message_sources", ["document_id"])
    op.create_index("ix_message_sources_chunk_id", "message_sources", ["chunk_id"])

    # ------------------------------------------------------------------ #
    # feedback                                                             #
    # ------------------------------------------------------------------ #
    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.String(10), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("message_id", "user_id", name="uq_feedback_message_user"),
        sa.CheckConstraint("rating IN ('up', 'down')", name="ck_feedback_rating"),
    )
    op.create_index("ix_feedback_tenant_id", "feedback", ["tenant_id"])
    op.create_index("ix_feedback_message_id", "feedback", ["message_id"])
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])

    # ------------------------------------------------------------------ #
    # audit_log (partitioned by RANGE(created_at))                        #
    # ------------------------------------------------------------------ #
    op.execute("""
        CREATE TABLE audit_log (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
            action VARCHAR(100) NOT NULL,
            resource_type VARCHAR(50),
            resource_id UUID,
            ip INET,
            details JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at);
    """)
    op.execute("CREATE INDEX idx_audit_log_tenant_created ON audit_log (tenant_id, created_at);")
    op.execute("CREATE INDEX idx_audit_log_user_id ON audit_log (user_id);")
    op.execute("CREATE INDEX idx_audit_log_action ON audit_log (action);")
    op.execute("CREATE INDEX idx_audit_log_resource_type ON audit_log (resource_type);")

    # Initial partitions — two months to start
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

    # ------------------------------------------------------------------ #
    # updated_at trigger                                                   #
    # ------------------------------------------------------------------ #
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    for table in UPDATED_AT_TABLES:
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        """)

    # ------------------------------------------------------------------ #
    # Seed permissions                                                     #
    # ------------------------------------------------------------------ #
    for code, description in PERMISSION_CODES:
        op.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ).bindparams(code=code, description=description)
        )


def downgrade() -> None:
    # Drop triggers first
    for table in UPDATED_AT_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table};")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column();")

    # Drop audit_log partitions and parent
    op.execute("DROP TABLE IF EXISTS audit_log_2026_08;")
    op.execute("DROP TABLE IF EXISTS audit_log_2026_07;")
    op.execute("DROP TABLE IF EXISTS audit_log;")

    # Drop tables in reverse FK order
    op.drop_table("feedback")
    op.drop_table("message_sources")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("rag_pipelines")
    op.drop_table("chunks_registry")
    op.drop_table("ingestion_jobs")
    op.drop_table("documents")
    op.drop_table("collection_access")
    op.drop_table("collections")
    op.drop_table("user_roles")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
    op.drop_table("user_tenants")
    op.drop_table("users")

    # Remove deferred FK before dropping tables
    op.drop_constraint("fk_models_registry_tenant_id", "models_registry", type_="foreignkey")
    op.drop_table("tenants")
    op.drop_table("models_registry")
