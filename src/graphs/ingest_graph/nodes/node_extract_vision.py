"""node_extract_vision — vision-model description of images and tables in medical documents.

Runs after node_chunk and before node_embed (or node_extract_entities when Graph RAG is
enabled).  For each section in state.extracted_sections that has section_type "image",
"table", or "diagram" and carries base64 image data, the node calls a vision-capable
LLM to produce a structured description.  The resulting text is appended to state.chunks
as new ChunkData objects so they are embedded alongside the native text chunks.

Opt-in:
    This node is only active when the collection's chunk_config contains:
        "vision_extraction_enabled": true
    The vision model is resolved via chunk_config["vision_model_id"] (UUID string of a
    models_registry record with type="llm" and a vision-capable model_id).  If that key
    is absent, the node falls back to the collection's default LLM model
    (embedding_model_id) as a last resort, which may fail for non-vision models — the
    failure is non-fatal.

Graph RAG topology note:
    When both graph_rag_enabled and vision_extraction_enabled are True, the node order is:
        node_chunk → node_extract_vision → node_extract_entities → node_embed
    Vision chunks are present in state.chunks when node_extract_entities runs, so entities
    can also be extracted from image descriptions.

Security / GDPR:
    - image_b64 content is NEVER logged; only counts, section indices, and UUIDs appear
      in logs and Langfuse spans.
    - The vision model is called with capture_input=False, capture_output=False.
    - Prompt injection: the vision prompt instructs the model to ignore embedded
      instructions and to omit patient PII from the output.
    - LLM responses are parsed as JSON; raw response text is never stored or logged.
    - Fail-safe: any per-image failure is logged as a warning; the pipeline continues
      without that image's description.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.ingest_graph.helpers import get_collection, get_model, update_step, utcnow
from src.graphs.ingest_graph.state import ChunkData, IngestState, Section

logger = structlog.get_logger(__name__)

_PROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent / "graphs" / "prompts" / "vision_extract_v1.md"
)

_VISUAL_SECTION_TYPES = frozenset({"image", "table", "diagram"})
_VALID_CONTENT_TYPES = frozenset({"image", "table", "diagram"})


def _load_prompt() -> str:
    """Load the vision extraction prompt from disk (not cached — allows hot-reload)."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)


def _make_vision_point_id(
    document_id: uuid.UUID, section_index: int, base_chunk_count: int
) -> uuid.UUID:
    """Deterministic SHA-256-derived point ID for a vision chunk.

    Encodes document_id, section_index, and the offset into the chunk list so that
    vision chunks are idempotent and do not collide with text chunks from node_chunk.
    The "v:" prefix distinguishes the key from the text-chunk key used in node_chunk.
    """
    key = f"v:{document_id}:{section_index}:{base_chunk_count}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return uuid.UUID(digest[:32])


def _parse_vision_response(raw_content: str) -> dict[str, Any] | None:
    """Parse and validate the JSON response from the vision model.

    Returns None when the response cannot be parsed or is missing required fields.
    Never logs raw_content (may contain derived document text).
    """
    try:
        data = json.loads(raw_content)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    description = data.get("description", "").strip()
    if not description:
        return None

    content_type = data.get("content_type", "image")
    if content_type not in _VALID_CONTENT_TYPES:
        content_type = "image"

    key_values: list[str] = [str(v) for v in data.get("key_values", []) if v]

    return {
        "description": description,
        "content_type": content_type,
        "key_values": key_values,
    }


async def _describe_section(
    section: Section,
    llm: Any,
    model_id: str,
    endpoint_url: str,
    prompt_template: str,
    document_id: str,
) -> dict[str, Any] | None:
    """Call the vision LLM to describe a single image/table section.

    Sends the base64 image as an OpenAI-compatible multimodal message.
    Returns the parsed response dict, or None on failure (non-fatal).

    Args:
        section: Must have image_b64 set and section_type in _VISUAL_SECTION_TYPES.
        llm: LLMClient instance.
        model_id: Vision model identifier (e.g. "llava:13b").
        endpoint_url: Base URL for the vision model endpoint.
        prompt_template: Contents of vision_extract_v1.md.
        document_id: String UUID for log correlation only.

    Returns:
        Parsed dict with keys description, content_type, key_values, or None.
    """
    if not section.image_b64:
        return None

    # Build an OpenAI-compatible multimodal user message.
    # The image is sent as a data URL so the payload is self-contained.
    user_message: dict[str, Any] = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    f"Describe the following medical {section.section_type} "
                    "in the JSON format specified in the system prompt. "
                    "Do NOT include any patient identifiers in your response."
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{section.image_b64}",
                    "detail": "high",
                },
            },
        ],
    }

    try:
        response = await llm.chat_completion(
            model=model_id,
            messages=[
                {"role": "system", "content": prompt_template},
                user_message,
            ],
            base_url=endpoint_url,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw_content: str = response.choices[0].message.content or "{}"
        parsed = _parse_vision_response(raw_content)
        return parsed
    except Exception as exc:
        logger.warning(
            "node_extract_vision.section_failed",
            document_id=document_id,
            section_index=section.section_index,
            section_type=section.section_type,
            error_type=type(exc).__name__,
        )
        return None


def _build_vision_chunk(
    parsed: dict[str, Any],
    section: Section,
    document_id: uuid.UUID,
    chunk_index: int,
) -> ChunkData:
    """Assemble a ChunkData from a parsed vision response.

    The chunk text prefixes the LLM description with a label so retrieval
    and embedding have clear context about the source type.

    Args:
        parsed: dict with description, content_type, key_values.
        section: The original Section (for heading and page metadata).
        document_id: Document UUID for deterministic point_id derivation.
        chunk_index: The absolute index this chunk will occupy in state.chunks
                     (used for point_id derivation and idempotency).

    Returns:
        ChunkData with metadata flagged as vision_extraction source.
    """
    description: str = parsed["description"]
    content_type: str = parsed["content_type"]
    key_values: list[str] = parsed.get("key_values", [])

    # Build human-readable text for the chunk — never includes image_b64.
    text_parts = [f"[{content_type.upper()}] {description}"]
    if key_values:
        text_parts.append("Key values: " + "; ".join(key_values))
    chunk_text = "\n".join(text_parts)

    point_id = _make_vision_point_id(document_id, section.section_index, chunk_index)

    return ChunkData(
        chunk_index=chunk_index,
        text=chunk_text,
        page=section.page,
        section=section.heading or f"Figure (section {section.section_index})",
        token_count=len(chunk_text.split()),  # approximate; embedding node uses real tokenizer
        point_id=point_id,
    )


@observe(name="node_extract_vision", capture_input=False, capture_output=False)
async def node_extract_vision(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Describe images and tables using a vision LLM and append results to state.chunks.

    Reads chunk_config.vision_extraction_enabled to gate activation.
    Reads chunk_config.vision_model_id to resolve the vision model; falls back to
    collection.embedding_model_id if the key is absent.

    For each section in state.extracted_sections with section_type in
    {"image", "table", "diagram"} and a non-empty image_b64 field, calls the
    vision model and appends a ChunkData to state.chunks.

    This node is fail-safe: any per-image failure is a warning; the pipeline
    continues regardless.  A total model-resolution or DB failure is also
    non-fatal — the node returns unchanged state with vision_chunks_count=0.

    Args:
        state: Must have extracted_sections and chunks set by earlier nodes.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        {"chunks": updated list[ChunkData], "vision_chunks_count": int}
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    llm = cfg["llm"]
    step_start = utcnow()

    # Resolve collection and gate on feature flag
    try:
        collection = await get_collection(session, state.collection_id)
        chunk_config: dict[str, Any] = collection.chunk_config or {}
    except Exception as exc:
        logger.warning(
            "node_extract_vision.collection_load_failed",
            document_id=str(state.document_id),
            error_type=type(exc).__name__,
        )
        return {"chunks": state.chunks, "vision_chunks_count": 0}

    if not chunk_config.get("vision_extraction_enabled", False):
        logger.info(
            "node_extract_vision.disabled_by_collection",
            document_id=str(state.document_id),
            collection_id=str(state.collection_id),
        )
        return {"chunks": state.chunks, "vision_chunks_count": 0}

    # Find visual sections with image data
    sections: list[Section] = state.extracted_sections or []
    visual_sections = [
        s for s in sections if s.section_type in _VISUAL_SECTION_TYPES and s.image_b64
    ]

    if not visual_sections:
        logger.info(
            "node_extract_vision.no_visual_sections",
            document_id=str(state.document_id),
            total_sections=len(sections),
        )
        await update_step(
            session,
            state.job_id,
            stage="extract_vision",
            status="completed",
            started_at=step_start,
            meta={
                "visual_sections_found": 0,
                "vision_chunks_added": 0,
                "latency_ms": _elapsed_ms(step_start),
            },
        )
        return {"chunks": state.chunks, "vision_chunks_count": 0}

    # Resolve vision model
    try:
        vision_model_id_str: str | None = chunk_config.get("vision_model_id")
        if vision_model_id_str:
            model_record = await get_model(session, uuid.UUID(vision_model_id_str))
        else:
            # Fall back: use embedding_model_id as a best-effort (may not support vision)
            model_record = await get_model(session, collection.embedding_model_id)
    except Exception as exc:
        logger.warning(
            "node_extract_vision.model_load_failed",
            document_id=str(state.document_id),
            error_type=type(exc).__name__,
        )
        with contextlib.suppress(Exception):
            await update_step(
                session,
                state.job_id,
                stage="extract_vision",
                status="warning",
                started_at=step_start,
                error=f"vision_model_load_failed: {type(exc).__name__}",
            )
        return {"chunks": state.chunks, "vision_chunks_count": 0}

    prompt_template = _load_prompt()
    existing_chunks: list[ChunkData] = list(state.chunks or [])
    base_chunk_index = len(existing_chunks)
    vision_chunks: list[ChunkData] = []

    for section in visual_sections:
        parsed = await _describe_section(
            section=section,
            llm=llm,
            model_id=model_record.model_id,
            endpoint_url=model_record.endpoint_url,
            prompt_template=prompt_template,
            document_id=str(state.document_id),
        )
        if parsed is None:
            # Failure already logged in _describe_section — skip and continue
            continue

        chunk_index = base_chunk_index + len(vision_chunks)
        vision_chunk = _build_vision_chunk(
            parsed=parsed,
            section=section,
            document_id=state.document_id,
            chunk_index=chunk_index,
        )
        vision_chunks.append(vision_chunk)

    elapsed = _elapsed_ms(step_start)
    vision_count = len(vision_chunks)

    _lf_update_span(
        metadata={
            "document_id": str(state.document_id),
            "tenant_id": str(state.tenant_id),
            "visual_sections_found": len(visual_sections),
            "vision_chunks_added": vision_count,
            "latency_ms": elapsed,
        }
    )
    logger.info(
        "node_extract_vision.completed",
        document_id=str(state.document_id),
        visual_sections_found=len(visual_sections),
        vision_chunks_added=vision_count,
        latency_ms=elapsed,
    )

    await update_step(
        session,
        state.job_id,
        stage="extract_vision",
        status="completed",
        started_at=step_start,
        meta={
            "visual_sections_found": len(visual_sections),
            "vision_chunks_added": vision_count,
            "latency_ms": elapsed,
        },
    )

    updated_chunks = existing_chunks + vision_chunks
    return {
        "chunks": updated_chunks,
        "vision_chunks_count": vision_count,
    }
