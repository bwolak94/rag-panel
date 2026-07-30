"""Minimal health server for the ingest worker on port 9090."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

health_app = FastAPI(docs_url=None, redoc_url=None)


@health_app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "component": "ingest-worker"}


async def run_health_server() -> None:
    config = uvicorn.Config(health_app, host="0.0.0.0", port=9090, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
