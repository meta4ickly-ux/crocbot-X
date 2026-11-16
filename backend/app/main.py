"""Crocbot X backend — FastAPI entrypoint.

Run locally:
    cd backend && ../.venv/bin/uvicorn app.main:app --reload --port 8000

v1 notes (see docs/BACKEND.md):
- No auth in v1. Single-user service; bind to localhost locally and
  put Caddy + auth in front of it on the VPS before exposing it.
- /health is liveness (no DB), /ready checks the DB.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .agent.engines import engine_status
from .config import settings
from .db import SessionLocal, init_db, seed_bots
from .observability import get_logger, log_event
from .routers import approvals, bots, chat, handoffs, media, metrics
from .security import RateLimiter

limiter = RateLimiter(settings.rate_limit_per_min)
logger = get_logger("crocbot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Crocbot X", version="0.3.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)
app.include_router(chat.router)
app.include_router(bots.router)
app.include_router(handoffs.router)
app.include_router(approvals.router)
app.include_router(media.router)
app.include_router(metrics.router)

# Working chat UI (web/index.html, no build step). Served same-origin at
# /ui/ so the page can call /api/v1/* without CORS or a separate host.
# The design blueprint in ~/workspace/ts-spaces/crocbot-x/ is a
# document, not this UI — do not edit it here.
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
if _WEB_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        key = request.client.host if request.client else "unknown"
        if not limiter.allow(key):
            return JSONResponse(status_code=429, content={"detail": "rate limit exceeded"})
    return await call_next(request)


@app.get("/")
def root():
    return {
        "service": "crocbot-x",
        "version": "0.3.0",
        "engine": settings.default_engine,
        "pay": "disabled ($0 moved)",
        "docs": "/docs",
        "health": "/health",
        "ui": "/ui/",
    }


@app.get("/health")
def health():
    return {"status": "ok", "service": "crocbot-x"}


@app.get("/ready")
def ready():
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "not ready", "detail": str(exc)})
    return {"status": "ready", "engines": engine_status()}
