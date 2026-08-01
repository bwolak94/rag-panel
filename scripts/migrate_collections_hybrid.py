#!/usr/bin/env python3
"""Migration script: add sparse vector configuration to existing Qdrant collections.

Run this ONCE after deploying the hybrid retrieval update (TASK-019).

This script is an admin one-off tool and is explicitly EXEMPT from the architecture
rule that forbids direct qdrant_client usage outside src/retrieval/ — it does not
run as part of the application and is never imported by application code.

Usage:
    python scripts/migrate_collections_hybrid.py [--dry-run] [--collection emb_bge_m3]

Steps for each collection:
1. Check whether sparse_vectors_config already includes the "sparse" field.
2. If not, call update_collection to add the SparseVectorParams config.
3. Scroll through ALL points in the collection.
4. For each batch of points, compute sparse vectors via fastembed BM25.
5. Re-upsert each point with both dense ("dense" key) and sparse ("sparse" key) vectors.

Exit codes:
    0 — success (or dry-run completed)
    1 — fatal error
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    PointStruct,
    SparseVector,
    SparseVectorParams,
)

# Lazy import — fastembed is a heavy dependency; we want a clear error if missing.
try:
    from fastembed import SparseTextEmbedding  # type: ignore[import-untyped]
except ImportError:
    print("ERROR: fastembed is not installed. Run: pip install fastembed>=0.4", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration — read from environment (same as application settings)
# ---------------------------------------------------------------------------

import os

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")

# Batch size for scroll + re-upsert (balance memory vs. throughput)
SCROLL_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Sparse encoder — single instance for the whole script run
# ---------------------------------------------------------------------------

_encoder: SparseTextEmbedding | None = None


def get_encoder() -> SparseTextEmbedding:
    """Return (and lazily initialise) the fastembed BM25 encoder."""
    global _encoder  # noqa: PLW0603
    if _encoder is None:
        print("Initialising fastembed BM25 encoder (Qdrant/bm25) …")
        _encoder = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _encoder


def encode_sparse(text: str) -> SparseVector:
    """Encode *text* to a Qdrant SparseVector using fastembed BM25."""
    result = list(get_encoder().embed([text]))[0]
    return SparseVector(indices=list(result.indices), values=list(result.values))


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


async def collection_has_sparse(client: AsyncQdrantClient, collection_name: str) -> bool:
    """Return True if the collection already has a 'sparse' sparse_vectors_config entry."""
    info = await client.get_collection(collection_name)
    sparse_cfg = getattr(info.config, "sparse_vectors_config", None) or {}
    if hasattr(sparse_cfg, "keys"):
        return "sparse" in sparse_cfg
    # Some SDK versions return a dict-like object; try mapping access.
    try:
        return "sparse" in dict(sparse_cfg)
    except Exception:
        return False


async def add_sparse_vector_config(
    client: AsyncQdrantClient, collection_name: str, dry_run: bool
) -> None:
    """Add SparseVectorParams 'sparse' config to the collection if not already present."""
    if await collection_has_sparse(client, collection_name):
        print(f"  [skip] '{collection_name}' already has sparse vector config.")
        return

    print(f"  [update] Adding sparse vector config to '{collection_name}' …")
    if not dry_run:
        await client.update_collection(
            collection_name=collection_name,
            sparse_vectors_config={"sparse": SparseVectorParams()},
        )
        print(f"  [ok] Sparse vector config added to '{collection_name}'.")
    else:
        print(f"  [dry-run] Would add sparse vector config to '{collection_name}'.")


async def reindex_collection(
    client: AsyncQdrantClient, collection_name: str, dry_run: bool
) -> None:
    """Scroll all points and re-upsert with sparse vectors."""
    print(f"\nRe-indexing '{collection_name}' with sparse vectors …")

    offset: Any = None
    total_reindexed = 0

    while True:
        scroll_result = await client.scroll(
            collection_name=collection_name,
            limit=SCROLL_BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        points_batch, next_offset = scroll_result

        if not points_batch:
            break

        new_points: list[PointStruct] = []
        for point in points_batch:
            # Extract dense vector — handle both flat and named-vector formats.
            raw_vectors = point.vector
            if isinstance(raw_vectors, dict):
                dense_vec: list[float] = raw_vectors.get("dense", [])
            elif isinstance(raw_vectors, list):
                dense_vec = raw_vectors
            else:
                print(f"  [warn] Unexpected vector type for point {point.id}, skipping.")
                continue

            # Extract text payload for BM25 encoding.
            payload: dict[str, Any] = point.payload or {}
            text: str = str(payload.get("text", ""))

            if text:
                sparse_vec = encode_sparse(text)
            else:
                print(f"  [warn] Point {point.id} has no 'text' payload — empty sparse vector.")
                sparse_vec = SparseVector(indices=[], values=[])

            new_points.append(
                PointStruct(
                    id=point.id,
                    vector={"dense": dense_vec, "sparse": sparse_vec},
                    payload=payload,
                )
            )

        if not dry_run and new_points:
            await client.upsert(
                collection_name=collection_name,
                points=new_points,
                wait=True,
            )

        total_reindexed += len(new_points)
        print(
            f"  {'[dry-run] Would upsert' if dry_run else 'Upserted'} "
            f"{len(new_points)} points (total so far: {total_reindexed})"
        )

        if next_offset is None:
            break
        offset = next_offset

    print(
        f"  [done] '{collection_name}': "
        f"{'would re-index' if dry_run else 're-indexed'} {total_reindexed} points."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add sparse vector config and re-index Qdrant collections for hybrid search."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without actually modifying Qdrant.",
    )
    parser.add_argument(
        "--collection",
        metavar="NAME",
        help="Migrate only the named collection (e.g. emb_bge_m3). "
        "Default: all emb_* collections.",
    )
    args = parser.parse_args()

    client = AsyncQdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    print(f"Connected to Qdrant at {QDRANT_URL}")
    if args.dry_run:
        print("DRY-RUN mode — no changes will be written.\n")

    try:
        all_collections = (await client.get_collections()).collections
        collection_names = [c.name for c in all_collections]

        # Filter to emb_* unless a specific collection was requested.
        if args.collection:
            if args.collection not in collection_names:
                print(
                    f"ERROR: Collection '{args.collection}' not found in Qdrant.", file=sys.stderr
                )
                sys.exit(1)
            targets = [args.collection]
        else:
            targets = [n for n in collection_names if n.startswith("emb_")]

        if not targets:
            print("No emb_* collections found. Nothing to migrate.")
            return

        print(f"Collections to migrate: {targets}\n")

        for collection_name in targets:
            print(f"Processing '{collection_name}' …")
            await add_sparse_vector_config(client, collection_name, dry_run=args.dry_run)
            await reindex_collection(client, collection_name, dry_run=args.dry_run)

        print("\nMigration complete.")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
