import asyncio

import pytest

from app.agent.engines import (
    CodexEngine,
    MuseEngine,
    PiEngine,
    StubEngine,
    engine_status,
    get_engine,
)


def test_stub_engine_is_deterministic_and_free():
    async def collect():
        return [e async for e in StubEngine().stream("hello there", bot_name="Time Auditor")]
    events = asyncio.run(collect())
    assert events[0]["type"] == "token"
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"]["cost_usd"] == 0.0
    assert "Time Auditor" in done["text"]


def test_muse_engine_defaults_to_stub_without_a_key():
    # No XAI_API_KEY in the test env, so Muse resolves to the stub.
    async def collect():
        return [e async for e in MuseEngine().stream("hello there")]
    events = asyncio.run(collect())
    done = events[-1]
    assert done["type"] == "done"
    assert done["engine"] == "muse"
    assert done["provider"] == "stub"
    assert done["usage"]["cost_usd"] == 0.0


def test_codex_engine_reports_availability_honestly():
    # Codex IS installed in this environment; if it were not, the
    # adapter must say so instead of faking a run.
    ok, detail = CodexEngine().available()
    assert ok is True, detail


def test_pi_engine_reports_not_installed():
    ok, detail = PiEngine().available()
    assert ok is False
    assert "not installed" in detail


def test_pi_engine_errors_cleanly_instead_of_faking():
    async def collect():
        return [e async for e in PiEngine().stream("hello")]
    events = asyncio.run(collect())
    assert events[-1]["type"] == "error"
    assert "not installed" in events[-1]["detail"]


def test_unknown_engine_is_rejected():
    with pytest.raises(ValueError, match="unknown engine"):
        get_engine("not-a-real-engine")


def test_engine_status_lists_all_engines():
    status = {s["engine"]: s for s in engine_status()}
    assert set(status) == {"muse", "xai", "codex", "pi", "stub"}
    assert status["muse"]["available"] is True
    assert status["stub"]["available"] is True
    assert status["pi"]["available"] is False
    assert status["xai"]["available"] is False  # no key in tests
