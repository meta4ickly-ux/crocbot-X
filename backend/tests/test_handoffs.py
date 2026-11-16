"""Bot-to-bot handoffs: create, filter, complete, fail — all logged."""
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db, seed_bots
from app.main import app


def _client():
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
    finally:
        db.close()
    return TestClient(app)


def _create(client, frm="researcher", to="developer", task="Build the thing"):
    return client.post("/api/v1/handoffs", json={
        "from_bot_id": frm, "to_bot_id": to, "task": task})


def test_handoff_create_list_complete():
    with _client() as client:
        resp = _create(client)
        assert resp.status_code == 200
        h = resp.json()
        assert h["status"] == "pending"
        assert h["from_bot_id"] == "researcher"
        assert h["to_bot_id"] == "developer"
        # Pending handoffs for the receiving bot.
        pending = client.get("/api/v1/handoffs?to_bot_id=developer&status=pending").json()
        assert [p["id"] for p in pending] == [h["id"]]
        # Complete it once; a second finish is a conflict.
        done = client.post(f"/api/v1/handoffs/{h['id']}/complete").json()
        assert done["status"] == "completed"
        assert done["completed_at"]
        assert client.post(f"/api/v1/handoffs/{h['id']}/complete").status_code == 409
        assert client.get("/api/v1/handoffs?status=pending").json() == []


def test_handoff_fail():
    with _client() as client:
        h = _create(client, frm="inbox", to="researcher", task="Check this").json()
        failed = client.post(f"/api/v1/handoffs/{h['id']}/fail").json()
        assert failed["status"] == "failed"


def test_handoff_validation():
    with _client() as client:
        # Unknown bot on either side.
        assert _create(client, frm="nope", to="developer").status_code == 404
        assert _create(client, frm="researcher", to="nope").status_code == 404
        # A bot cannot hand off to itself; task must not be empty.
        assert _create(client, frm="developer", to="developer").status_code == 400
        assert _create(client, task="   ").status_code == 400
        assert _create(client, task="").status_code == 400
        # Bad status filter and unknown handoff id.
        assert client.get("/api/v1/handoffs?status=bogus").status_code == 400
        assert client.get("/api/v1/handoffs/nope").status_code == 404
        assert client.post("/api/v1/handoffs/nope/complete").status_code == 404
