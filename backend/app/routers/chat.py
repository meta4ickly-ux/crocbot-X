"""Chat router: POST /api/v1/chat (SSE) + conversations + runs.

SSE event names match the design blueprint exactly: token,
tool_call, tool_result, citation, approval_required, done, error.
"""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..agent import durable
from ..agent.loop import daily_budget_remaining, run_turn
from ..agent.tools import execute_tool
from ..config import settings
from ..db import Approval, Conversation, Message, Run, _utcnow, get_db, list_bots
from ..models import RetiredModelError, validate_model

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    bot_id: str | None = None
    engine: str | None = None
    model: str = "grok-4.7"
    user_id: str = "founder"


class MessageCreate(BaseModel):
    role: str = Field(default="user")
    content: str


class ConversationCreate(BaseModel):
    bot_id: str | None = None
    title: str | None = None


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _conversation_out(conv: Conversation, db: Session) -> dict:
    messages = (
        db.query(Message).filter(Message.conversation_id == conv.id)
        .order_by(Message.created_at).all()
    )
    return {
        "id": conv.id,
        "bot_id": conv.bot_id,
        "title": conv.title,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content,
             "tool_calls": m.tool_calls, "citations": m.citations,
             "tokens": m.tokens, "cost_usd": m.cost_usd,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ],
    }


@router.post("/chat")
async def chat(req: ChatRequest, db: Session = Depends(get_db)):
    try:
        validate_model(req.model)
    except RetiredModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if daily_budget_remaining(db, req.user_id, settings.daily_budget_usd) <= 0:
        raise HTTPException(status_code=429, detail="daily budget exhausted ($0 left)")

    if req.conversation_id:
        conversation = db.get(Conversation, req.conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
    else:
        conversation = Conversation(bot_id=req.bot_id, title=(req.message or "")[:80] or "New conversation")
        db.add(conversation)
        db.commit()
        db.refresh(conversation)

    bot = None
    if req.bot_id:
        bots = {b.id: b for b in list_bots(db, include_hidden=True)}
        bot = bots.get(req.bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail=f"bot {req.bot_id!r} not found")
        if bot.paused:
            raise HTTPException(
                status_code=423, detail=f"bot {req.bot_id!r} is paused: {bot.paused_reason}")
    bot_name = bot.name if bot else "Crocbot X"
    system_prompt = bot.system_prompt if bot else ""

    user_message = Message(conversation_id=conversation.id, role="user", content=req.message)
    db.add(user_message)
    db.commit()

    async def stream():
        async for event in run_turn(
            db, conversation=conversation, bot_id=req.bot_id, bot_name=bot_name,
            system_prompt=system_prompt, user_text=req.message,
            engine_name=req.engine, model=req.model, user_id=req.user_id,
        ):
            yield _sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/conversations")
def get_conversations(db: Session = Depends(get_db)):
    convs = db.query(Conversation).order_by(Conversation.updated_at.desc()).all()
    return [_conversation_out(c, db) for c in convs]


@router.post("/conversations")
def create_conversation(body: ConversationCreate, db: Session = Depends(get_db)):
    conv = Conversation(bot_id=body.bot_id, title=body.title or "New conversation")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return _conversation_out(conv, db)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _conversation_out(conv, db)


@router.post("/conversations/{conversation_id}/messages")
def add_message(conversation_id: str, body: MessageCreate, db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    if body.role not in ("user", "assistant", "tool"):
        raise HTTPException(status_code=400, detail="role must be user, assistant or tool")
    message = Message(conversation_id=conversation_id, role=body.role, content=body.content)
    db.add(message)
    conv.updated_at = _utcnow()
    db.commit()
    db.refresh(message)
    return {"id": message.id, "role": message.role, "content": message.content,
            "created_at": message.created_at.isoformat() if message.created_at else None}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    db.query(Message).filter(Message.conversation_id == conversation_id).delete()
    db.delete(conv)
    db.commit()
    return {"deleted": conversation_id}


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    steps = durable.load_journal(db, run_id)
    return {
        "id": run.id, "conversation_id": run.conversation_id, "bot_id": run.bot_id,
        "engine": run.engine, "model": run.model, "status": run.status,
        "stop_reason": run.stop_reason, "restart_count": run.restart_count,
        "steps": [{"seq": s.seq, "kind": s.kind, "status": s.status,
                   "replayable": s.replayable, "payload": s.payload} for s in steps],
    }


@router.post("/runs/{run_id}/resume")
async def resume_run_endpoint(run_id: str, db: Session = Depends(get_db)):
    """Pi's session.continue(): resume a run from its journal.

    Completed replayable steps are re-used, never re-executed. If the
    run is awaiting an approval that has since been approved, the
    approved tool runs exactly once and the result is returned as an
    SSE tool_result + done.
    """
    try:
        run, steps = durable.resume_run(db, run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    approval = (
        db.query(Approval).filter(Approval.run_id == run_id)
        .order_by(Approval.created_at.desc()).first()
    )

    async def stream():
        # Replay: emit the journal's completed steps as tool_results.
        for step in steps:
            if step.kind == "tool_result":
                yield _sse({"type": "tool_result", "tool": step.payload.get("tool"),
                            "result": step.payload.get("result"), "replayed": True})
        if approval is not None and approval.status == "approved":
            try:
                result = execute_tool(
                    approval.tool_name, approval.args or {},
                    workdir=settings.workdir, allowlist=settings.fetch_allowlist,
                    allow_shell=settings.allow_shell, approved=True,
                )
            except Exception as exc:
                result = {"error": str(exc)}
            durable.checkpoint(db, run, (steps[-1].seq + 1) if steps else 1,
                               "tool_result", {"tool": approval.tool_name, "result": result})
            yield _sse({"type": "tool_result", "tool": approval.tool_name, "result": result})
            durable.finish_run(db, run, status="completed", stop_reason="completed")
            yield _sse({"type": "done", "text": "", "run_id": run.id, "resumed": True,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}})
        elif approval is not None and approval.status in ("pending", "expired"):
            durable.finish_run(db, run, status="awaiting_approval", stop_reason="awaiting_approval")
            yield _sse({"type": "approval_required", "approval_id": approval.id,
                        "tool": approval.tool_name, "run_id": run.id})
            yield _sse({"type": "done", "text": "", "run_id": run.id, "status": "awaiting_approval",
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}})
        else:
            durable.finish_run(db, run, status="completed", stop_reason="completed")
            yield _sse({"type": "done", "text": "", "run_id": run.id, "resumed": True,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
