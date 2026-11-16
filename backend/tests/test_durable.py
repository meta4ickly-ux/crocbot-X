import pytest

from app.agent import durable
from app.db import SessionLocal, init_db


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    yield


def test_journal_checkpoints_and_resume_replays():
    db = SessionLocal()
    try:
        run = durable.create_run(db, conversation_id=None, bot_id="time-auditor",
                                 engine="stub", model="grok-4.3")
        durable.checkpoint(db, run, 1, "watch", {"text": "hi"})
        durable.checkpoint(db, run, 2, "judge", {"verdict": "APPROVE"})
        durable.checkpoint(db, run, 3, "tool_result",
                           {"tool": "web_search", "result": {"stub": True}})
        resumed, steps = durable.resume_run(db, run.id)
        assert resumed.id == run.id
        assert resumed.restart_count == 1
        assert [s.seq for s in steps] == [1, 2, 3]
        assert steps[2].kind == "tool_result"
    finally:
        db.close()


def test_restart_limit_ends_the_run():
    db = SessionLocal()
    try:
        run = durable.create_run(db, conversation_id=None, bot_id=None,
                                 engine="stub", model="grok-4.3")
        for _ in range(durable.RESTART_LIMIT):
            durable.resume_run(db, run.id)
        with pytest.raises(RuntimeError, match="restart limit"):
            durable.resume_run(db, run.id)
        assert run.status == "failed"
        assert run.stop_reason == "restart_limit"
    finally:
        db.close()


def test_unknown_run_resume_is_404_not_a_crash():
    db = SessionLocal()
    try:
        with pytest.raises(ValueError, match="unknown run"):
            durable.resume_run(db, "no-such-run")
    finally:
        db.close()
