import datetime as dt

from fastapi.testclient import TestClient

from app.db import SessionLocal, _utcnow, init_db, seed_bots
from app.db import Approval
from app.main import app


def _client():
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
    finally:
        db.close()
    return TestClient(app)


def test_health_and_ready():
    with _client() as client:
        assert client.get("/health").json() == {"status": "ok", "service": "crocbot-x"}
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        engines = {e["engine"]: e for e in ready.json()["engines"]}
        assert engines["pi"]["available"] is False
        assert engines["muse"]["available"] is True


def test_bots_roster_and_time_auditor_mode():
    with _client() as client:
        bots = client.get("/api/v1/bots").json()
        by_id = {b["id"]: b for b in bots}
        # 7 named bots from the reference image + Developer, Researcher,
        # Inbox, llamaXia Judge; hidden bots excluded.
        assert set(by_id) == {
            "time-auditor", "calorie-tracker", "technical-explainer",
            "david-todoist", "github-viewer", "youtube-titles",
            "new-discord-manager", "developer", "researcher", "inbox",
            "llamaxia-judge",
        }
        auditor = by_id["time-auditor"]
        assert auditor["schedule_mode"] == "daily_background+on_demand"
        assert auditor["asks_permission"] is False
        assert auditor["interrupts_user"] is False
        assert auditor["messages_others"] is False
        assert auditor["model"] == "grok-4.3"
        judge = by_id["llamaxia-judge"]
        assert "Pay" in judge["description"] and "disabled" in judge["description"]
        assert by_id["developer"]["model"] == "grok-build-0.1"
        assert by_id["researcher"]["model"] == "grok-4.7"
        hidden = client.get("/api/v1/bots?include_hidden=true").json()
        assert len(hidden) == 18  # 11 named + 7 hidden placeholders


def test_chat_dry_run_sse():
    with _client() as client:
        resp = client.post("/api/v1/chat", json={"message": "hello there, audit my day"})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert '"type": "done"' in resp.text
        assert '"cost_usd": 0.0' in resp.text
        assert '"engine": "muse"' in resp.text


def test_chat_retired_model_is_rejected():
    with _client() as client:
        resp = client.post("/api/v1/chat", json={"message": "hi", "model": "grok-4"})
        assert resp.status_code == 400
        assert "retired" in resp.json()["detail"]


def test_chat_pocketpal_model_is_not_rejected_as_unknown():
    # A local GGUF id is not in the xAI registry, so the router must
    # not 400 it when the engine is pocketpal. No phone is reachable
    # here, so the stream ends with an honest error event, not a fake
    # answer.
    with _client() as client:
        resp = client.post("/api/v1/chat", json={
            "message": "hello", "engine": "pocketpal",
            "model": "Qwen2.5-0.5B-Instruct",
        })
        assert resp.status_code == 200
        assert '"type": "error"' in resp.text
        assert "PocketPal" in resp.text


def test_chat_spam_is_logged_not_run():
    with _client() as client:
        resp = client.post("/api/v1/chat", json={"message": "FREE MONEY click here to win"})
        assert resp.status_code == 200
        assert "spam" in resp.text.lower()


def test_conversations_crud():
    with _client() as client:
        conv = client.post("/api/v1/conversations", json={}).json()
        assert conv["id"]
        msg = client.post(f"/api/v1/conversations/{conv['id']}/messages",
                          json={"role": "user", "content": "hello"}).json()
        assert msg["content"] == "hello"
        fetched = client.get(f"/api/v1/conversations/{conv['id']}").json()
        assert len(fetched["messages"]) == 1
        assert client.get("/api/v1/conversations/does-not-exist").status_code == 404


def test_approvals_fail_closed_on_timeout_and_approve_once():
    with _client() as client:
        db = SessionLocal()
        try:
            expired = Approval(run_id="r1", tool_name="write_file", args={"path": "x"},
                               expires_at=_utcnow() - dt.timedelta(seconds=1))
            pending = Approval(run_id="r2", tool_name="write_file", args={"path": "y"},
                               expires_at=_utcnow() + dt.timedelta(hours=1))
            db.add_all([expired, pending])
            db.commit()
            expired_id, pending_id = expired.id, pending.id
        finally:
            db.close()
        listed = client.get("/api/v1/approvals").json()
        by_id = {a["id"]: a for a in listed}
        assert by_id[expired_id]["status"] == "expired"  # timeout fails closed
        assert by_id[pending_id]["status"] == "pending"
        approved = client.post(f"/api/v1/approvals/{pending_id}/approve").json()
        assert approved["status"] == "approved"
        assert client.post(f"/api/v1/approvals/{pending_id}/approve").status_code == 409


def test_images_endpoint_is_a_stub_not_a_fake():
    with _client() as client:
        resp = client.post("/api/v1/images", json={"prompt": "a llama"})
        assert resp.status_code == 501
        assert "not implemented" in resp.json()["detail"]


def test_voice_contract_endpoint():
    with _client() as client:
        body = client.get("/api/v1/voice/realtime").json()
        assert body["status"] == "stub"
        assert "wss://api.x.ai/v1/realtime" in body["contract"]["upstream"]
