"""Pydantic schemas for the Tenant Management API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class TenantSettings(BaseModel):
    """JSONB column schema for tenants.settings."""

    max_docs: int = Field(default=10_000, ge=1)
    max_collections: int = Field(default=50, ge=1)
    max_users: int = Field(default=100, ge=1)
    max_storage_bytes: int = Field(default=50 * 1024**3)  # 50 GB
    retention_days: int = Field(default=365, ge=30)
    industry: str = Field(default="general", max_length=50)
    disclaimer_text: str = Field(default="", max_length=2000)


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")
    settings: TenantSettings = Field(default_factory=TenantSettings)

    @field_validator("slug")
    @classmethod
    def slug_no_leading_trailing_dash(cls, v: str) -> str:
        if v.startswith("-") or v.endswith("-"):
            raise ValueError("Slug must not start or end with a dash")
        return v


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: TenantSettings | None = None


class TenantResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    settings: TenantSettings
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AddUserRequest(BaseModel):
    """Request body for POST /tenants/{id}/users."""

    keycloak_sub: str = Field(..., min_length=1, max_length=255)


class AssignRoleRequest(BaseModel):
    """Request body for PUT /tenants/{id}/users/{user_id}/role."""

    role: str = Field(..., pattern=r"^(admin|contributor|viewer)$")


class TenantMemberResponse(BaseModel):
    user_id: uuid.UUID
    keycloak_sub: str
    email: str
    display_name: str | None
    role: str | None
    joined_at: datetime

    model_config = {"from_attributes": True}
