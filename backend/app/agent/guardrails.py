"""Guardrails / review layer — the trust layer. REQUIRED, not optional.

Every destructive or outward-facing action goes through review BEFORE
it runs. Reads stay automatic.

Actions that ALWAYS require review (no auto-approve, ever):
- deleting files (delete_file)
- any shell command that writes or deletes (run_shell)
- sending email (send_email)
- posting to X or any external service (post_message)
- spending money (pay — refused outright by the PayGuard anyway)

Rules:
- No auto-approve for these. No bot self-approval: only the founder
  may approve or deny. A bot id, a worker, or any other caller is
  refused with 403.
- Timeout fails closed: a pending approval past expires_at is expired,
  the waiting run stays paused, and the action never runs.
- Every request AND every decision (approved / denied / expired) is
  written to the review_audit table with bot, action, target, reason
  and decided_by, and to the structured JSON log.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from ..config import settings
from ..db import Alert, Approval, ReviewAudit, _utcnow
from .tools import TOOLS

# Actions that require review. Read tools are not in this set.
REVIEW_REQUIRED_ACTIONS = frozenset(
    {"delete_file", "run_shell", "send_email", "post_message", "pay"}
)

# Only this identity may approve or deny. v1 is single-user; the
# deploy agent replaces this with real auth (see docs/BACKEND.md).
FOUNDER_ID = "founder"


def classify(action: str) -> str:
    """'review' for destructive/outward actions, 'auto' for reads.

    Unknown actions are treated as review-required (fail closed).
    """
    tool = TOOLS.get(action)
    if tool is None:
        return "review"
    if action in REVIEW_REQUIRED_ACTIONS or tool.side_effect == "write":
        return "review"
    return "auto"


def action_target(action: str, args: dict) -> str:
    """The concrete target of an action, for the audit log."""
    args = args or {}
    if action in ("delete_file", "write_file", "read_file"):
        return str(args.get("path", ""))
    if action == "run_shell":
        return str(args.get("command", ""))[:200]
    if action == "send_email":
        return str(args.get("to", ""))
    if action == "post_message":
        return str(args.get("channel", ""))
    if action == "pay":
        return str(args.get("recipient", ""))
    return ""


def _audit(db: Session, *, approval_id: str | None, run_id: str | None,
           bot_id: str | None, action: str, target: str, reason: str,
           decision: str, decided_by: str = "") -> ReviewAudit:
    row = ReviewAudit(
        approval_id=approval_id, run_id=run_id, bot_id=bot_id,
        action=action, target=target, reason=reason,
        decision=decision, decided_by=decided_by,
    )
    db.add(row)
    db.commit()
    return row


def request_review(db: Session, *, run_id: str, bot_id: str | None,
                   action: str, args: dict, reason: str) -> Approval:
    """Create a pending review request and audit it. The action must
    NOT run until decide_review() approves it."""
    target = action_target(action, args)
    approval = Approval(
        run_id=run_id, tool_name=action, args=args or {},
        expires_at=_utcnow() + dt.timedelta(seconds=settings.approval_timeout_s),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    _audit(db, approval_id=approval.id, run_id=run_id, bot_id=bot_id,
           action=action, target=target, reason=reason, decision="requested")
    return approval


def decide_review(db: Session, approval_id: str, decision: str,
                  *, decided_by: str) -> Approval:
    """Approve or deny a pending review. Founder-only.

    Raises PermissionError for anyone who is not the founder (including
    bots/workers — no bot self-approval), and ValueError if the review
    is no longer pending (already decided, or expired = fail closed).
    """
    if decided_by != FOUNDER_ID:
        raise PermissionError(
            f"only the founder may decide reviews; {decided_by!r} is not the founder"
        )
    if decision not in ("approved", "denied"):
        raise ValueError("decision must be 'approved' or 'denied'")
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise ValueError(f"unknown approval {approval_id!r}")
    if approval.status == "pending" and approval.expires_at <= _utcnow():
        approval.status = "expired"
        db.commit()
    if approval.status != "pending":
        raise ValueError(f"approval is already {approval.status} (fail closed)")
    approval.status = decision
    approval.decided_at = _utcnow()
    db.commit()
    db.refresh(approval)
    _audit(db, approval_id=approval.id, run_id=approval.run_id, bot_id=None,
           action=approval.tool_name, target=action_target(approval.tool_name, approval.args or {}),
           reason=f"founder {decision}", decision=decision, decided_by=decided_by)
    return approval


def expire_pending(db: Session) -> list[Approval]:
    """Mark timed-out pending reviews as expired (fail closed) and audit
    each one. Returns the expired approvals."""
    now = _utcnow()
    expired = []
    for approval in db.query(Approval).filter(Approval.status == "pending").all():
        if approval.expires_at and approval.expires_at <= now:
            approval.status = "expired"
            expired.append(approval)
            _audit(db, approval_id=approval.id, run_id=approval.run_id, bot_id=None,
                   action=approval.tool_name,
                   target=action_target(approval.tool_name, approval.args or {}),
                   reason="approval timed out", decision="expired")
    if expired:
        db.commit()
    return expired


def is_action_allowed(db: Session, approval_id: str) -> bool:
    """True only if the review was explicitly approved by the founder
    and has not expired. Everything else is denied (fail closed)."""
    approval = db.get(Approval, approval_id)
    if approval is None or approval.status != "approved":
        return False
    return True
