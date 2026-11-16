"""Approvals router: approve / deny a pending write tool call.

Trust layer (see app/agent/guardrails.py):
- Only the founder may decide. Pass {"decided_by": "founder"}; any
  other identity — including a bot id — is refused with 403. No bot
  self-approval, ever.
- Timeout fails closed: a pending approval past expires_at is expired
  on read, and the run that waits on it stays paused; the action
  never runs.
- Every request and decision is written to the review_audit table.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agent import guardrails
from ..db import Approval, get_db

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


class DecideBody(BaseModel):
    decided_by: str = "founder"


def _out(approval: Approval) -> dict:
    return {
        "id": approval.id, "run_id": approval.run_id, "tool_name": approval.tool_name,
        "args": approval.args, "status": approval.status,
        "created_at": approval.created_at.isoformat() if approval.created_at else None,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
    }


@router.get("")
def list_approvals(status: str | None = None, db: Session = Depends(get_db)):
    guardrails.expire_pending(db)
    query = db.query(Approval).order_by(Approval.created_at.desc())
    if status:
        query = query.filter(Approval.status == status)
    return [_out(a) for a in query.all()]


@router.get("/{approval_id}")
def get_approval(approval_id: str, db: Session = Depends(get_db)):
    guardrails.expire_pending(db)
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    return _out(approval)


def _decide(approval_id: str, decision: str, body: DecideBody, db: Session) -> dict:
    try:
        approval = guardrails.decide_review(
            db, approval_id, decision, decided_by=body.decided_by)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        status_code = 404 if "unknown approval" in str(exc) else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _out(approval)


@router.post("/{approval_id}/approve")
def approve(approval_id: str, body: DecideBody = DecideBody(), db: Session = Depends(get_db)):
    return _decide(approval_id, "approved", body, db)


@router.post("/{approval_id}/deny")
def deny(approval_id: str, body: DecideBody = DecideBody(), db: Session = Depends(get_db)):
    return _decide(approval_id, "denied", body, db)


@router.get("/audit/log")
def audit_log(db: Session = Depends(get_db)):
    """Full review audit trail: every request and decision."""
    from ..db import ReviewAudit

    rows = db.query(ReviewAudit).order_by(ReviewAudit.created_at.desc()).limit(200).all()
    return [
        {"id": r.id, "approval_id": r.approval_id, "run_id": r.run_id,
         "bot_id": r.bot_id, "action": r.action, "target": r.target,
         "reason": r.reason, "decision": r.decision, "decided_by": r.decided_by,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]
