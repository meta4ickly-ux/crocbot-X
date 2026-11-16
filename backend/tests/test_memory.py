"""Memory both ways: DB stays the searchable store; plain files
(data/memory/<bot-id>.md) are the portable one."""
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal, init_db, seed_bots
from app.main import app
from app.memory import append_memory_note, export_memory, read_memory


def _client():
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
    finally:
        db.close()
    return TestClient(app)


def test_plain_file_roundtrip_and_traversal_rejected():
    content = append_memory_note("time-auditor", "Founder hates morning meetings", kind="rule")
    assert "Founder hates morning meetings" in content
    assert "(rule)" in content
    assert read_memory("time-auditor") == content
    # A second note appends, it does not replace.
    content2 = append_memory_note("time-auditor", "Prefers short summaries", kind="preference")
    assert "Founder hates morning meetings" in content2
    assert "Prefers short summaries" in content2
    # Bad kinds, empty notes, multi-line notes, and path traversal fail.
    with pytest.raises(ValueError):
        append_memory_note("time-auditor", "x", kind="bogus")
    with pytest.raises(ValueError):
        append_memory_note("time-auditor", "   ")
    with pytest.raises(ValueError):
        append_memory_note("time-auditor", "line one\nline two")
    with pytest.raises(ValueError):
        read_memory("../escape")
    with pytest.raises(ValueError):
        read_memory("UPPER")


def test_export_keeps_notes_and_adds_profile():
    append_memory_note("researcher", "Always cite sources", kind="rule")
    content = export_memory("researcher", {
        "name": "Researcher", "model": "grok-4.7", "description": "Researches topics."})
    assert "Bot: Researcher" in content
    assert "Always cite sources" in content
    # Exporting again does not duplicate the note.
    assert export_memory("researcher").count("Always cite sources") == 1


def test_memory_endpoints():
    with _client() as client:
        assert client.get("/api/v1/bots/nope/memory").status_code == 404
        assert client.post("/api/v1/bots/nope/memory", json={"note": "x"}).status_code == 404
        # Empty at first, then a note lands and reads back.
        assert client.get("/api/v1/bots/inbox/memory").json() == {"bot_id": "inbox", "content": ""}
        resp = client.post("/api/v1/bots/inbox/memory",
                           json={"note": "Drafts only, never send", "kind": "rule"})
        assert resp.status_code == 200
        assert "Drafts only, never send" in resp.json()["content"]
        assert client.post("/api/v1/bots/inbox/memory", json={"note": "x", "kind": "bogus"}).status_code == 400
        assert client.post("/api/v1/bots/inbox/memory", json={"note": "  "}).status_code == 400
        exported = client.post("/api/v1/bots/inbox/memory/export").json()
        assert "Bot: Inbox" in exported["content"]
        assert "Drafts only, never send" in exported["content"]


def test_completed_turn_writes_a_memory_line():
    with _client() as client:
        resp = client.post("/api/v1/chat", json={
            "message": "hello there, audit my day", "bot_id": "time-auditor",
            "engine": "stub"})
        assert resp.status_code == 200
        assert '"type": "done"' in resp.text
        memory = client.get("/api/v1/bots/time-auditor/memory").json()["content"]
        assert "(turn) turn: verdict=APPROVE" in memory
        assert "cost=$0.00" in memory
