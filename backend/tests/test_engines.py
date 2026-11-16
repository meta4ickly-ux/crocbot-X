import asyncio
import json

import httpx
import pytest

from app.agent.engines import (
    CodexEngine,
    MuseEngine,
    PiEngine,
    PocketPalEngine,
    StubEngine,
    engine_status,
    get_engine,
)


def _sse_chunk(content: str) -> str:
    return 'data: {"choices": [{"delta": {"content": %s}}]}\n\n' % json.dumps(content)


def _pocketpal_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


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


def test_pocketpal_engine_is_registered():
    engine = get_engine("pocketpal")
    assert isinstance(engine, PocketPalEngine)
    assert engine.name == "pocketpal"


def test_pocketpal_engine_streams_tokens_and_done():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        body = _sse_chunk("Hel") + _sse_chunk("lo") + "data: [DONE]\n\n"
        return httpx.Response(200, text=body,
                              headers={"content-type": "text/event-stream"})

    async def collect():
        return [e async for e in PocketPalEngine(
            client=_pocketpal_client(handler)).stream("hello there")]

    events = asyncio.run(collect())
    assert seen["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    # The chat default model (grok-4.7) is not a local model id, so it
    # resolves to the configured PocketPal model.
    assert seen["body"]["model"] == "Qwen2.5-0.5B-Instruct"
    assert seen["body"]["messages"][-1] == {"role": "user", "content": "hello there"}
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert tokens == ["Hel", "lo"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["engine"] == "pocketpal"
    assert done["model"] == "Qwen2.5-0.5B-Instruct"
    assert done["text"] == "Hello"
    assert done["usage"]["cost_usd"] == 0.0


def test_pocketpal_engine_passes_through_local_model_name():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=_sse_chunk("ok") + "data: [DONE]\n\n")

    async def collect():
        return [e async for e in PocketPalEngine(
            client=_pocketpal_client(handler)).stream(
                "hello", model="Abliterated-Llama-3.2-3B")]

    events = asyncio.run(collect())
    assert seen["body"]["model"] == "Abliterated-Llama-3.2-3B"
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "Abliterated-Llama-3.2-3B"


def test_pocketpal_engine_non_stream_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "full answer"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    async def collect():
        return [e async for e in PocketPalEngine(
            client=_pocketpal_client(handler)).stream("hello")]

    events = asyncio.run(collect())
    assert [e["text"] for e in events if e["type"] == "token"] == ["full answer"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["text"] == "full answer"
    assert done["usage"]["prompt_tokens"] == 3
    assert done["usage"]["completion_tokens"] == 2
    assert done["usage"]["cost_usd"] == 0.0


def test_pocketpal_engine_connect_failure_is_error_event():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def collect():
        return [e async for e in PocketPalEngine(
            client=_pocketpal_client(handler)).stream("hello")]

    events = asyncio.run(collect())
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "cannot reach PocketPal" in events[0]["detail"]
    assert "127.0.0.1:8080" in events[0]["detail"]


def test_pocketpal_engine_http_error_is_error_event():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model not loaded")

    async def collect():
        return [e async for e in PocketPalEngine(
            client=_pocketpal_client(handler)).stream("hello")]

    events = asyncio.run(collect())
    assert events[-1]["type"] == "error"
    assert "503" in events[-1]["detail"]


def test_pocketpal_available_true_when_models_responds():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:8080/v1/models"
        return httpx.Response(200, json={"data": [{"id": "Qwen2.5-0.5B-Instruct"}]})

    ok, detail = PocketPalEngine(transport=httpx.MockTransport(handler)).available()
    assert ok is True
    assert "127.0.0.1:8080" in detail


def test_pocketpal_available_false_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    ok, detail = PocketPalEngine(transport=httpx.MockTransport(handler)).available()
    assert ok is False
    assert "not reachable" in detail


def test_engine_status_lists_all_engines():
    status = {s["engine"]: s for s in engine_status()}
    assert set(status) == {"muse", "xai", "codex", "pi", "stub", "pocketpal"}
    assert status["muse"]["available"] is True
    assert status["stub"]["available"] is True
    assert status["pi"]["available"] is False
    assert status["xai"]["available"] is False  # no key in tests
    assert status["pocketpal"]["available"] is False  # no phone reachable here
