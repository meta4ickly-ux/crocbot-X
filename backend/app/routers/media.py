"""Images + voice routers — stubs to the documented contract.

Images: POST /api/v1/images. The research report could NOT verify the
exact xAI Imagine REST endpoint path (model IDs and prices are
verified, the path is not — see docs/BACKEND.md). So this endpoint
does not call an unverified URL: it returns 501 with the contract the
deploy agent must finish. No money is spent, no fake image is returned.

Voice: GET /api/v1/voice/realtime returns the contract (the browser
never gets the xAI key; the relay is server-side). The WebSocket
/api/v1/voice/ws accepts a connection, sends one error frame stating
the relay is not implemented yet, and closes — a stub, not a relay.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1", tags=["media"])


class ImageRequest(BaseModel):
    prompt: str
    model: str = "grok-imagine-image-2.0"
    size: str = "1024x1024"


@router.post("/images", status_code=501)
def create_image(body: ImageRequest):
    return {
        "detail": "images not implemented in v1",
        "contract": {
            "endpoint": "POST /api/v1/images",
            "request": {"prompt": "string", "model": "grok-imagine-image-2.0",
                        "size": "1024x1024"},
            "response_when_live": {"image_url": "signed URL", "model": body.model,
                                   "cost_usd": 0.04},
            "blocked_by": "exact xAI Imagine REST endpoint path was not verified "
                          "in the research pass (model IDs + prices verified, path not). "
                          "Confirm at docs.x.ai/developers/model-capabilities/images/generation "
                          "before wiring this endpoint.",
        },
    }


@router.get("/voice/realtime")
def voice_realtime():
    return {
        "contract": {
            "upstream": "wss://api.x.ai/v1/realtime?model=grok-voice-latest",
            "model": "grok-voice-3.0",
            "note": "The xAI key stays server-side. The browser connects to this "
                    "backend's /api/v1/voice/ws; the backend relays to xAI. "
                    "The relay itself is not implemented in v1 — see below.",
        },
        "websocket": "/api/v1/voice/ws",
        "status": "stub",
    }


@router.websocket("/voice/ws")
async def voice_ws(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({
        "type": "error",
        "detail": "voice relay is not implemented in v1. Contract: browser mic -> "
                  "this WebSocket -> wss://api.x.ai/v1/realtime (key server-side) "
                  "-> audio deltas back to the browser.",
    })
    await websocket.close(code=1011)
