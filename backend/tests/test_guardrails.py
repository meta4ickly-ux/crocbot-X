import datetime as dt

import pytest

from app.agent import guardrails
from app.db import Approval, ReviewAudit, SessionLocal, _utcnow, init_db


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


def _db_session():
    return SessionLocal()


def test_classify_reads_auto_dangerous_review():
    assert guardrails.classify("web_search") == "auto"
    assert guardrails.classify("fetch_url") == "auto"
    assert guardrails.classify("read_file") == "auto"
    for action in ("delete_file", "run_shell", "send_email", "post_message", "pay"):
        assert guardrails.classify(action) == "review", action
    assert guardrails.classify("not-a-real-tool") == "review"  # fail closed


def test_request_review_audits_the_request():
    db = _db_session()
    try:
        approval = guardrails.request_review(
            db, run_id="r-g1", bot_id="time-auditor", action="delete_file",
            args={"path": "notes.txt"}, reason="cleanup requested")
        assert approval.status == "pending"
        rows = db.query(ReviewAudit).filter(
            ReviewAudit.approval_id == approval.id).all()
        assert len(rows) == 1
        assert rows[0].decision == "requested"
        assert rows[0].bot_id == "time-auditor"
        assert rows[0].action == "delete_file"
        assert rows[0].target == "notes.txt"
    finally:
        db.close()


def test_bot_self_approval_is_refused():
    db = _db_session()
    try:
        approval = guardrails.request_review(
            db, run_id="r-g2", bot_id="time-auditor", action="send_email",
            args={"to": "a@b.c"}, reason="test")
        with pytest.raises(PermissionError, match="only the founder"):
            guardrails.decide_review(db, approval.id, "approved", decided_by="time-auditor")
        with pytest.raises(PermissionError, match="only the founder"):
            guardrails.decide_review(db, approval.id, "approved", decided_by="bot:time-auditor")
        with pytest.raises(PermissionError, match="only the founder"):
            guardrails.decide_review(db, approval.id, "denied", decided_by="worker")
        # Still pending — the refusals changed nothing.
        assert db.get(Approval, approval.id).status == "pending"
    finally:
        db.close()


def test_founder_approve_and_audit_trail():
    db = _db_session()
    try:
        approval = guardrails.request_review(
            db, run_id="r-g3", bot_id="github-viewer", action="post_message",
            args={"channel": "#general"}, reason="announce release")
        decided = guardrails.decide_review(db, approval.id, "approved", decided_by="founder")
        assert decided.status == "approved"
        assert guardrails.is_action_allowed(db, approval.id) is True
        decisions = [r.decision for r in db.query(ReviewAudit).filter(
            ReviewAudit.approval_id == approval.id).order_by(ReviewAudit.id).all()]
        assert decisions == ["requested", "approved"]
    finally:
        db.close()


def test_denial_blocks_the_action():
    db = _db_session()
    try:
        approval = guardrails.request_review(
            db, run_id="r-g4", bot_id="github-viewer", action="delete_file",
            args={"path": "important.md"}, reason="test")
        guardrails.decide_review(db, approval.id, "denied", decided_by="founder")
        assert guardrails.is_action_allowed(db, approval.id) is False
        with pytest.raises(ValueError, match="already denied"):
            guardrails.decide_review(db, approval.id, "approved", decided_by="founder")
    finally:
        db.close()


def test_timeout_fails_closed():
    db = _db_session()
    try:
        approval = guardrails.request_review(
            db, run_id="r-g5", bot_id="time-auditor", action="run_shell",
            args={"command": "rm -rf /tmp/x"}, reason="test")
        # Fast-forward past expiry.
        approval.expires_at = _utcnow() - dt.timedelta(seconds=1)
        db.commit()
        expired = guardrails.expire_pending(db)
        assert approval.id in [a.id for a in expired]
        assert db.get(Approval, approval.id).status == "expired"
        assert guardrails.is_action_allowed(db, approval.id) is False
        # A late founder approval must NOT revive it.
        with pytest.raises(ValueError, match="already expired"):
            guardrails.decide_review(db, approval.id, "approved", decided_by="founder")
        decisions = [r.decision for r in db.query(ReviewAudit).filter(
            ReviewAudit.approval_id == approval.id).order_by(ReviewAudit.id).all()]
        assert decisions == ["requested", "expired"]
    finally:
        db.close()
