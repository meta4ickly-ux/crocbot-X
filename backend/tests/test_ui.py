"""Working chat UI: served at /ui/ and wired to the real API paths."""
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


def test_ui_served_and_wired_to_api():
    with _client() as client:
        resp = client.get("/ui/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        html = resp.text
        assert "Crocbot X" in html
        # The UI must call the real backend paths — not a mock, not a
        # different prefix.
        assert "/api/v1/bots" in html
        assert "/api/v1/chat" in html
        assert "/api/v1/approvals" in html
        assert "/api/v1/handoffs" in html
        assert "/memory" in html
        assert "/metrics" in html
        assert "Pay: DISABLED" in html
        # Our edge stays in the UI: the offline PocketPal engine is offered.
        assert "pocketpal" in html
        # Same file at its direct path.
        assert client.get("/ui/index.html").status_code == 200
        # Root advertises the UI.
        assert client.get("/").json()["ui"] == "/ui/"
