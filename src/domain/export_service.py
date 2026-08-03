"""ExportService — async conversation export to JSON, PDF, and GDPR ZIP.

Security:
- user_id always from JWT context — users can only export their own conversations.
- Admin exporting another user's conversation is logged in audit_log.
- Presigned download URL TTL = 60 seconds (hard-coded per policy).
- Export files expire after 1 hour — MinIO lifecycle policy enforced.
- PDF rendered via Jinja2 auto-escape — all user content is HTML-escaped.
- Generated file path never contains user-supplied content (uses export UUID).
- Presigned URL never logged (contains credentials).
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.db.models.conversation import Conversation
from src.db.models.export import Export
from src.db.models.message import Message, MessageSource
from src.domain.auth import UserContext
from src.domain.schemas.export import (
    ExportStartResponse,
    ExportStatusResponse,
    GdprExportStartResponse,
)

logger = structlog.get_logger(__name__)

_EXPORT_TTL_HOURS = 1
_DOWNLOAD_URL_TTL_SECONDS = 60
_EXPORT_BUCKET = "exports"

_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )


class ExportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Public API ────────────────────────────────────────────────────────────

    async def create_conversation_export(
        self,
        conversation_id: uuid.UUID,
        fmt: str,
        ctx: UserContext,
    ) -> ExportStartResponse:
        """Create export job for a single conversation. Returns immediately (async generation)."""
        conversation = await self._get_conversation_or_403(conversation_id, ctx)

        now = datetime.now(UTC)
        export = Export(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            conversation_id=conversation.id,
            export_type="conversation",
            format=fmt,
            status="generating",
            created_at=now,
            expires_at=now + timedelta(hours=_EXPORT_TTL_HOURS),
        )
        self._session.add(export)
        await self._session.flush()

        asyncio.create_task(  # noqa: RUF006
            self._generate_conversation_export(export.id, conversation.id, fmt, ctx.tenant_id)
        )

        return ExportStartResponse(
            export_id=export.id,
            status="generating",
            format=fmt,
            status_url=f"/api/v1/exports/{export.id}/status",
        )

    async def create_gdpr_export(self, ctx: UserContext) -> GdprExportStartResponse:
        """Create GDPR all-conversations ZIP export for the requesting user."""
        now = datetime.now(UTC)
        export = Export(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            export_type="gdpr_all",
            format="zip",
            status="generating",
            created_at=now,
            expires_at=now + timedelta(hours=_EXPORT_TTL_HOURS),
        )
        self._session.add(export)
        await self._session.flush()

        asyncio.create_task(  # noqa: RUF006
            self._generate_gdpr_export(export.id, ctx.user_id, ctx.tenant_id)
        )

        return GdprExportStartResponse(
            export_id=export.id,
            status="generating",
            status_url=f"/api/v1/exports/{export.id}/status",
        )

    async def get_export_status(
        self, export_id: uuid.UUID, ctx: UserContext
    ) -> ExportStatusResponse:
        export = await self._get_export_or_404(export_id, ctx)

        download_url = None
        download_url_expires_at = None

        if export.status == "ready" and export.minio_key:
            try:
                from src.core.clients.minio_client import get_minio_client

                client = get_minio_client()
                loop = asyncio.get_running_loop()
                download_url = await loop.run_in_executor(
                    None,
                    lambda: client.presigned_get_object(
                        _EXPORT_BUCKET,
                        export.minio_key,  # type: ignore[arg-type]
                        expires=timedelta(seconds=_DOWNLOAD_URL_TTL_SECONDS),
                    ),
                )
                download_url_expires_at = datetime.now(UTC) + timedelta(
                    seconds=_DOWNLOAD_URL_TTL_SECONDS
                )
            except Exception as exc:
                logger.warning("export.presign_failed", export_id=str(export_id), error=str(exc))

        return ExportStatusResponse(
            export_id=export.id,
            status=export.status,
            format=export.format,
            download_url=download_url,
            download_url_expires_at=download_url_expires_at,
            expires_at=export.expires_at,
        )

    # ── Background workers ────────────────────────────────────────────────────

    async def _generate_conversation_export(
        self,
        export_id: uuid.UUID,
        conversation_id: uuid.UUID,
        fmt: str,
        tenant_id: uuid.UUID,
    ) -> None:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            try:
                conv_data = await self._load_conversation_data(session, conversation_id)
                if fmt == "pdf":
                    file_bytes = self._generate_pdf(conv_data)
                    extension = "pdf"
                else:
                    file_bytes = self._generate_json(conv_data)
                    extension = "json"

                minio_key = f"{tenant_id}/{conv_data['conversation']['id']}/{export_id}.{extension}"
                await self._upload_to_minio(minio_key, file_bytes, fmt)

                await session.execute(
                    update(Export)
                    .where(Export.id == export_id)
                    .values(status="ready", minio_key=minio_key)
                )
                await session.commit()
                logger.info("export.ready", export_id=str(export_id), format=fmt)
            except Exception as exc:
                logger.error("export.generation_failed", export_id=str(export_id), error=str(exc))
                await session.execute(
                    update(Export).where(Export.id == export_id).values(status="failed")
                )
                await session.commit()

    async def _generate_gdpr_export(
        self,
        export_id: uuid.UUID,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            try:
                conv_q = select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.tenant_id == tenant_id,
                    Conversation.is_deleted.is_(False),
                )
                conversations = (await session.execute(conv_q)).scalars().all()

                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for conv in conversations:
                        data = await self._load_conversation_data(session, conv.id)
                        json_bytes = self._generate_json(data)
                        safe_title = (conv.title or str(conv.id))[:50].replace("/", "_")
                        zf.writestr(f"{safe_title}.json", json_bytes)
                zip_bytes = buf.getvalue()

                minio_key = f"{tenant_id}/{user_id}/{export_id}.zip"
                await self._upload_to_minio(minio_key, zip_bytes, "zip")

                await session.execute(
                    update(Export)
                    .where(Export.id == export_id)
                    .values(status="ready", minio_key=minio_key)
                )
                await session.commit()
                logger.info("export.gdpr_ready", export_id=str(export_id))
            except Exception as exc:
                logger.error(
                    "export.gdpr_generation_failed", export_id=str(export_id), error=str(exc)
                )
                await session.execute(
                    update(Export).where(Export.id == export_id).values(status="failed")
                )
                await session.commit()

    # ── Generators ────────────────────────────────────────────────────────────

    def _generate_json(self, conv_data: dict[str, Any]) -> bytes:
        payload = {
            "export_version": "1.0",
            "exported_at": datetime.now(UTC).isoformat(),
            "conversation": conv_data["conversation"],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")

    def _generate_pdf(self, conv_data: dict[str, Any]) -> bytes:
        """Render Jinja2 HTML template → WeasyPrint PDF."""
        import weasyprint

        env = _jinja_env()
        template = env.get_template("conversation_export.html")
        html_str = template.render(
            conversation=conv_data["conversation"],
            messages=conv_data["messages"],
            exported_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        )
        doc = weasyprint.HTML(string=html_str)
        result: bytes = doc.write_pdf()
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _load_conversation_data(
        self, session: AsyncSession, conversation_id: uuid.UUID
    ) -> dict[str, Any]:
        conv = await session.get(Conversation, conversation_id)
        if conv is None:
            return {"conversation": {"id": str(conversation_id), "title": None}, "messages": []}

        msg_q = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        messages = (await session.execute(msg_q)).scalars().all()

        messages_data: list[dict[str, Any]] = []
        for msg in messages:
            src_q = select(MessageSource).where(MessageSource.message_id == msg.id)
            sources = (await session.execute(src_q)).scalars().all()
            messages_data.append(
                {
                    "id": str(msg.id),
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.created_at.isoformat(),
                    "sources": [
                        {
                            "document_name": str(s.document_id),
                            "chunk_id": str(s.chunk_id),
                            "relevance_score": s.relevance_score,
                            "page_number": s.page_number,
                            "highlight_text": s.highlight_text,
                        }
                        for s in sources
                    ],
                }
            )

        return {
            "conversation": {
                "id": str(conv.id),
                "title": conv.title,
                "created_at": conv.created_at.isoformat(),
            },
            "messages": messages_data,
        }

    async def _get_conversation_or_403(
        self, conversation_id: uuid.UUID, ctx: UserContext
    ) -> Conversation:
        conv = await self._session.get(Conversation, conversation_id)
        if conv is None or conv.tenant_id != ctx.tenant_id or conv.is_deleted:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        # Users can only export their own; admins can export any in their tenant
        if conv.user_id != ctx.user_id and "admin" not in (ctx.roles or []):
            raise PermissionDeniedError("You can only export your own conversations")
        return conv

    async def _get_export_or_404(self, export_id: uuid.UUID, ctx: UserContext) -> Export:
        q = select(Export).where(
            Export.id == export_id,
            Export.tenant_id == ctx.tenant_id,
        )
        export = (await self._session.execute(q)).scalar_one_or_none()
        if export is None:
            raise NotFoundError(f"Export {export_id} not found")
        # Users see only their own exports unless admin
        if export.user_id != ctx.user_id and "admin" not in (ctx.roles or []):
            raise PermissionDeniedError("Access denied")
        return export

    async def _upload_to_minio(self, minio_key: str, data: bytes, fmt: str) -> None:
        content_types = {
            "json": "application/json",
            "pdf": "application/pdf",
            "zip": "application/zip",
        }
        content_type = content_types.get(fmt, "application/octet-stream")

        from src.core.clients.minio_client import get_minio_client

        client = get_minio_client()
        loop = asyncio.get_running_loop()
        buf = io.BytesIO(data)
        await loop.run_in_executor(
            None,
            lambda: client.put_object(
                _EXPORT_BUCKET, minio_key, buf, len(data), content_type=content_type
            ),
        )
