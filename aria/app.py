# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""Aria's FastAPI app — an interactive, generated dream-world with memory + video.

Routes:
  GET  /api/health                          liveness + brain/video backend
  POST /api/aria/start {name,gender,face}   open the world; returns the first beat
  POST /api/aria/{id}/say {text}            advance the story / talk to the guide
  GET  /api/aria/{id}                        world state
  GET  /api/aria/{id}/memory                 the guide's living memory (engrams)

Turn-based: each call returns the guide's spoken line + the scene video for the
new beat. The Qwen key rides the per-request ``X-Qwen-Key`` header (per-tab, never
persisted); Qwen is the main brain. The memory engine is the copied Kioku
substrate — this project is standalone.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.config import REPO_ROOT, settings
from engine.qwen import LLMError, QwenClient
from engine.store import open_store
from engine.tenants import KiokuEngine, MindFull, TenantRegistry

from aria.video import build_video_backend
from aria.world import WorldManager

log = logging.getLogger("aria.app")
logging.basicConfig(level=os.environ.get("ARIA_LOG_LEVEL", "INFO"))


class SayRequest(BaseModel):
    text: str = Field(default="", max_length=2000)


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
        # Aria's OWN durable store (engrams), its own DSN or local SQLite.
        try:
            from aria.persistence import EngramStore
            db_dsn = (os.environ.get("DATABASE_URL") or os.environ.get("ARIA_DB")
                      or str(REPO_ROOT / "aria_data" / "aria.db"))
            app.state.db = EngramStore(db_dsn)
            app.state.engine.persistor.append(
                lambda tenant, engram: app.state.db.save_engram(tenant, engram)
            )
        except Exception as e:  # noqa: BLE001 — durability is best-effort
            log.warning("Aria durable store unavailable, memory is in-RAM only: %s", e)
            app.state.db = None
        app.state.world = WorldManager(app.state.engine, app.state.video)
        yield
        eng = app.state.engine
        await eng.drain_background()
        store = getattr(eng, "_store", None)
        if store is not None:
            store.close()
        await eng.qwen.aclose()
        await eng.aclose_brains()
        await app.state.video.aclose()
        db = getattr(app.state, "db", None)
        if db is not None:
            db.close()

    app = FastAPI(title="Aria", version="0.2.0", lifespan=lifespan)
    origins = os.environ.get("ARIA_WEB_ORIGIN", "http://localhost:8001").split(",")
    app.add_middleware(
        CORSMiddleware, allow_origins=[o.strip() for o in origins],
        allow_methods=["GET", "POST"], allow_headers=["*"],
    )

    def brain(request: Request) -> QwenClient:
        return request.app.state.engine.qwen_for(request.headers.get("X-Qwen-Key"))

    def _require(request: Request, run_id: str):
        run = request.app.state.world.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no such world")
        return run

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        eng = request.app.state.engine
        store = getattr(eng, "_store", None)
        return {"ok": True, "service": "aria", "version": "0.2.0",
                "backend": store.stats().backend if store else "unknown",
                "video": request.app.state.video.name, "brain": eng.qwen.config.provider}

    @app.post("/api/aria/start")
    async def start(request: Request) -> dict:
        try:
            return await request.app.state.world.start(qwen=brain(request))
        except LLMError as e:
            raise HTTPException(status_code=502, detail=f"LLM unavailable: {e}") from e

    @app.post("/api/aria/{run_id}/say")
    async def say(request: Request, run_id: str, body: SayRequest) -> dict:
        run = _require(request, run_id)
        try:
            return await request.app.state.world.say(run, body.text, qwen=brain(request))
        except MindFull as e:
            raise HTTPException(status_code=429, detail=str(e)) from e
        except LLMError as e:
            raise HTTPException(status_code=502, detail=f"LLM unavailable: {e}") from e

    @app.get("/api/aria/{run_id}")
    async def get_world(request: Request, run_id: str) -> dict:
        return _require(request, run_id).public()

    @app.get("/api/aria/{run_id}/clip/{turn_no}")
    async def clip(request: Request, run_id: str, turn_no: int) -> dict:
        """Poll for an exchange's talking video — {"status":"pending"} until ready."""
        run = _require(request, run_id)
        return request.app.state.world.get_clip(run, turn_no)

    @app.get("/api/aria/{run_id}/memory")
    async def memory(request: Request, run_id: str) -> dict:
        run = _require(request, run_id)
        mind = request.app.state.engine.registry.named_mind(run.tenant)
        engrams = mind.index.live_engrams()
        engrams.sort(key=lambda e: e.ts, reverse=True)
        return {"total": len(engrams),
                "engrams": [{"engram_id": e.engram_id, "meaning": e.meaning,
                             "message": e.message, "ts": e.ts} for e in engrams[:12]]}

    web_dir = REPO_ROOT / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    return app


def get_app() -> FastAPI:
    return create_app()
