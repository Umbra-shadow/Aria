# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""Aria's FastAPI app — split-screen live dialogue with memory + video.

Routes:
  GET  /api/health                       liveness + brain/video backend
  POST /api/aria/start  {topic,turns}    begin a two-agent conversation
  GET  /api/aria/{id}                    full conversation state
  GET  /api/aria/{id}/stream             SSE: live tokens + per-turn video jobs

The Qwen key rides the per-request ``X-Qwen-Key`` header (per-tab, never
persisted); Qwen is the main brain. The memory engine is the copied Kioku
substrate — this project is standalone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.config import REPO_ROOT, settings
from engine.qwen import LLMError, QwenClient
from engine.store import open_store
from engine.tenants import KiokuEngine, TenantRegistry

from aria.dialogue import DialogueManager
from aria.video import build_video_backend

log = logging.getLogger("aria.app")
logging.basicConfig(level=os.environ.get("ARIA_LOG_LEVEL", "INFO"))


class StartRequest(BaseModel):
    topic: str = Field(min_length=2, max_length=2000)
    turns: int = Field(default=6, ge=2, le=12)


def build_engine() -> KiokuEngine:
    cfg = settings()
    store = open_store(cfg.data_dir)
    qwen = QwenClient(cfg.llm)
    registry = TenantRegistry(store, qwen, cfg, message_cap=int(os.environ.get("ARIA_MESSAGE_CAP", "1000000")))
    engine = KiokuEngine(registry)
    engine._store = store  # type: ignore[attr-defined]
    return engine


def create_app(engine: KiokuEngine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or build_engine()
        app.state.video = build_video_backend()
        app.state.dialogue = DialogueManager(app.state.engine, app.state.video)
        yield
        eng = app.state.engine
        await eng.drain_background()
        store = getattr(eng, "_store", None)
        if store is not None:
            store.close()
        await eng.qwen.aclose()
        await eng.aclose_brains()
        await app.state.video.aclose()

    app = FastAPI(title="Aria", version="0.1.0", lifespan=lifespan)
    origins = os.environ.get("ARIA_WEB_ORIGIN", "http://localhost:8001").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins],
        allow_methods=["GET", "POST"], allow_headers=["*"],
    )

    def brain(request: Request) -> QwenClient:
        return request.app.state.engine.qwen_for(request.headers.get("X-Qwen-Key"))

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        eng = request.app.state.engine
        store = getattr(eng, "_store", None)
        return {
            "ok": True, "service": "aria", "version": "0.1.0",
            "backend": store.stats().backend if store else "unknown",
            "video": request.app.state.video.name,
            "brain": eng.qwen.config.provider,
        }

    @app.post("/api/aria/start")
    async def start(request: Request, body: StartRequest) -> dict:
        conv = request.app.state.dialogue.start(body.topic, body.turns, qwen=brain(request))
        return {"conv_id": conv.conv_id, "topic": conv.topic, "max_turns": conv.max_turns, "status": conv.status}

    @app.get("/api/aria/{conv_id}")
    async def get_conv(request: Request, conv_id: str) -> dict:
        conv = request.app.state.dialogue.get(conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        return conv.public()

    @app.get("/api/aria/{conv_id}/stream")
    async def stream(request: Request, conv_id: str) -> StreamingResponse:
        conv = request.app.state.dialogue.get(conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="no such conversation")

        async def event_source():
            for event in conv.recent_events:
                yield f"data: {json.dumps(event)}\n\n"
            if conv.is_terminal:
                yield f"data: {json.dumps({'stage': conv.status, 'terminal': True})}\n\n"
                return
            q = conv.subscribe()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {json.dumps(event)}\n\n"
                        if event.get("stage") in ("done", "error"):
                            break
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                conv.unsubscribe(q)

        return StreamingResponse(
            event_source(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    web_dir = REPO_ROOT / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


def get_app() -> FastAPI:
    return create_app()
