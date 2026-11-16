"""Bot-to-bot handoffs: one bot passes a task to another, logged.

A handoff is a record, not an execution. The receiving bot still runs
through the normal loop — judge, guardrails, approval gate — when it
picks the task up. Nothing here moves money or sends anything outward.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import Bot, Handoff, _utcnow, get_db
from ..observability import get_logger, log_event

logger = get_logger("crocbot.handoffs")
router = APIRouter(prefix="/api/v1/handoffs", tags=["handoffs"])


class HandoffCreate(BaseModel):
    from_bot_id: str
    to_bot_id: str
    task: str
    run_id: str | None = None


def _out(h: Handoff) -> dict:
    return {
        "id": h.id, "from_bot_id": h.from_bot_id, "to_bot_id": h.to_bot_id,
        "task": h.task, "status": h.status, "run_id": h.run_id,
        "created_at": h.created_at.isoformat() if h.created_at else None,
        "completed_at": h.completed_at.isoformat() if h.completed_at else None,
    }


@router.post("")
def create_handoff(body: HandoffCreate, db: Session = Depends(get_db)):
    if not (body.task or "").strip():
        raise HTTPException(status_code=400, detail="task must not be empty")
    if body.from_bot_id == body.to_bot_id:
        raise HTTPException(status_code=400, detail="a bot cannot hand off to itself")
    for bot_id in (body.from_bot_id, body.to_bot_id):
        if db.get(Bot, bot_id) is None:
            raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    handoff = Handoff(from_bot_id=body.from_bot_id, to_bot_id=body.to_bot_id,
                      task=body.task.strip(), run_id=body.run_id)
    db.add(handoff)
    db.commit()
    db.refresh(handoff)
    log_event(logger, "info", "handoff created", bot_id=body.from_bot_id,
              run_id=handoff.run_id or "", handoff_id=handoff.id,
              to_bot_id=body.to_bot_id, task=body.task.strip()[:200])
    return _out(handoff)


@router.get("")
def list_handoffs(to_bot_id: str | None = None, from_bot_id: str | None = None,
                  status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Handoff).order_by(Handoff.created_at.desc())
    if to_bot_id:
        query = query.filter(Handoff.to_bot_id == to_bot_id)
    if from_bot_id:
        query = query.filter(Handoff.from_bot_id == from_bot_id)
    if status:
        if status not in ("pending", "completed", "failed"):
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
        query = query.filter(Handoff.status == status)
    return [_out(h) for h in query.all()]


@router.get("/{handoff_id}")
def get_handoff(handoff_id: str, db: Session = Depends(get_db)):
    handoff = db.get(Handoff, handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    return _out(handoff)


def _finish(handoff_id: str, status: str, db: Session) -> dict:
    handoff = db.get(Handoff, handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    if handoff.status != "pending":
        raise HTTPException(status_code=409, detail=f"handoff already {handoff.status}")
    handoff.status = status
    handoff.completed_at = _utcnow()
    db.commit()
    db.refresh(handoff)
    log_event(logger, "info", f"handoff {status}", bot_id=handoff.to_bot_id,
              run_id=handoff.run_id or "", handoff_id=handoff.id)
    return _out(handoff)


@router.post("/{handoff_id}/complete")
def complete_handoff(handoff_id: str, db: Session = Depends(get_db)):
    return _finish(handoff_id, "completed", db)


@router.post("/{handoff_id}/fail")
def fail_handoff(handoff_id: str, db: Session = Depends(get_db)):
    return _finish(handoff_id, "failed", db)
