"""Engine adapters — one interface, interchangeable engines.

EngineAdapter.stream() yields event dicts with a ``type`` of token,
tool_call, tool_result, citation, approval_required, done or error —
the same event names the SSE chat endpoint emits, so the frontend
never has to know which engine produced them.

Engines
-------
* ``muse`` (default) — the in-process engine. There is NO ``muse`` CLI
  binary on this machine (verified: ``which muse`` finds nothing), so
  the Muse engine is the orchestrator itself, backed by the xAI
  Responses API when XAI_API_KEY is set, otherwise the stub engine.
  It is not a subprocess.
* ``xai`` — xAI Responses API directly (grok-4.7 / 4.3 / build-0.1).
* ``codex`` — Codex CLI subprocess. Verified locally: codex-cli
  0.164.0-alpha.9 at /opt/hatch-image/bin/codex.
  ``codex exec --json -s read-only -a never -C <workdir>
  --skip-git-repo-check <prompt>``; resume with
  ``codex exec resume --json <session-id> <prompt>``. JSONL on stdout.
  Default sandbox is read-only and headless approvals auto-cancel —
  we never pass --dangerously-bypass-approvals-and-sandbox or --yolo.
* ``pi`` — Pi coding-agent CLI subprocess (NOT installed here:
  ``which pi`` finds nothing, no ~/.pi directory). Verified from
  upstream docs: npm ``@earendil-works/pi-coding-agent``,
  ``pi -p --mode json`` emits one AgentSessionEvent per JSONL line,
  sessions are JSONL transcripts under
  ``~/.pi/agent/sessions/<cwd-slug>/``, resume with
  ``--session <id>`` / ``--continue``. Pi asks for NO permission
  before running commands or editing files — that is why the backend
  never gives Pi a writable workdir and routes its write tool calls
  through our own approval gate first. The adapter reports
  not-installed cleanly instead of faking a run.
* ``stub`` — deterministic dry-run, $0, no key, no network. Used by
  tests and by ``muse``/``xai`` when no key is configured.
* ``pocketpal`` — PocketPal AI Local API server (Plus, v1.23.0) on a
  phone: OpenAI-compatible ``POST /chat/completions`` against a GGUF
  model running on-device via llama.cpp. Proof-of-concept /
  experimental, no TLS, LAN-only. $0, no cloud key. NOT live-tested
  here — no phone is reachable from this VM (see docs/BACKEND.md
  §6.1). ``available()`` probes ``GET {base_url}/models`` and says
  so honestly when the phone is unreachable.

CLI research sources (see docs/BACKEND.md §CLI research):
- Codex: developers.openai.com/codex/noninteractive/ and the local
  ``codex --help`` / ``codex exec --help`` output (0.164.0-alpha.9).
- Pi CLI: github.com/earendil-works/pi, docs at pi.dev, and the
  pi-mono-docs CLI reference; Pi Durable: developers.cloudflare.com/
  agents/harnesses/pi/ and the Pi Durable 1.5 changelog.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

import httpx

from ..config import settings
from ..models import MODELS, estimate_cost_usd, validate_model


def _event(type_: str, **fields) -> dict:
    event = {"type": type_}
    event.update(fields)
    return event


class EngineNotAvailable(RuntimeError):
    """The requested engine cannot run here (binary/key missing)."""


class EngineAdapter(ABC):
    """One interface for every engine."""

    name: str = "base"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(can_run, reason). Never raises for a missing binary/key."""

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        *,
        system: str = "",
        history: list[dict] | None = None,
        model: str = "grok-4.7",
        bot_name: str = "Crocbot X",
        workdir: str = ".",
        session_id: str | None = None,
        timeout_s: int = 300,
        max_steps: int = 10,
    ) -> AsyncIterator[dict]:
        """Yield events until done/error. Must not use shell=True."""

    async def resume(self, session_id: str, prompt: str, **kwargs) -> AsyncIterator[dict]:
        """Resume a previous engine session, or start fresh if unsupported."""
        async for event in self.stream(prompt, session_id=session_id, **kwargs):
            yield event


class StubEngine(EngineAdapter):
    """Deterministic dry-run engine: $0, no key, no network."""

    name = "stub"

    def available(self) -> tuple[bool, str]:
        return True, "stub engine is always available (dry-run, $0)"

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[dict]:
        bot_name = kwargs.get("bot_name", "Crocbot X")
        model = kwargs.get("model", "grok-4.7")
        text = (
            f"[{bot_name} · dry-run · {model}] I heard: {prompt[:200]}. "
            "This is the stub engine — no API key, no network, $0 spent. "
            "Set XAI_API_KEY or pick the codex/pi engine for a live run."
        )
        for word in text.split(" "):
            yield _event("token", text=word + " ")
        yield _event(
            "done",
            text=text,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
            engine=self.name,
            model=model,
        )


class XaiEngine(EngineAdapter):
    """xAI Responses API (POST /v1/responses), streamed SSE."""

    name = "xai"

    def available(self) -> tuple[bool, str]:
        if not settings.xai_api_key:
            return False, "XAI_API_KEY is not set"
        return True, "xAI API key is configured"

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[dict]:
        model = validate_model(kwargs.get("model", "grok-4.7"))
        system = kwargs.get("system", "")
        history = kwargs.get("history") or []
        timeout_s = kwargs.get("timeout_s", 300)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        # Server-side tools first (research report §2): the model
        # orchestrates web_search / x_search itself, no client loop.
        payload = {
            "model": model,
            "input": messages,
            "stream": True,
            "tools": [{"type": "web_search"}, {"type": "x_search"}],
        }
        headers = {"Authorization": f"Bearer {settings.xai_api_key}"}
        text_parts: list[str] = []
        prompt_tokens = completion_tokens = 0
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                async with client.stream(
                    "POST", f"{settings.xai_base_url}/responses",
                    json=payload, headers=headers,
                ) as resp:
                    if resp.status_code == 429:
                        yield _event("error", detail="xAI rate limit (429) — retry with backoff")
                        return
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        yield _event("error", detail=f"xAI API {resp.status_code}: {body[:300].decode(errors='replace')}")
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        otype = obj.get("type", "")
                        if otype.endswith("output_text.delta"):
                            delta = obj.get("delta", "")
                            text_parts.append(delta)
                            yield _event("token", text=delta)
                        elif otype.endswith("web_search_call.completed") or otype.endswith("x_search_call.completed"):
                            yield _event("citation", detail=str(obj)[:500])
                        elif otype == "response.completed":
                            usage = obj.get("response", {}).get("usage", {}) or {}
                            prompt_tokens = usage.get("input_tokens", 0)
                            completion_tokens = usage.get("output_tokens", 0)
        except httpx.HTTPError as exc:
            yield _event("error", detail=f"xAI request failed: {exc}")
            return
        cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
        yield _event(
            "done",
            text="".join(text_parts),
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "cost_usd": cost},
            engine=self.name,
            model=model,
        )


def _parse_cli_jsonl(line: str, engine_name: str):
    """Best-effort mapping of a CLI JSONL event to our events.

    Codex and Pi do not share an event schema, and the exact field that
    carries assistant text varies by version — so we scan for the
    common text carriers instead of asserting one shape. Unknown lines
    are ignored (never an error). Returns a list of our events.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(obj, dict):
        return []
    events: list[dict] = []
    # Session / thread id, for resume.
    for key in ("session_id", "thread_id", "sessionId", "threadId"):
        if obj.get(key):
            events.append(_event("tool_result", tool="session", result={"session_id": str(obj[key])}))
            break
    text = None
    for key in ("message", "text", "delta", "output", "content"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            text = value
            break
    if text:
        events.append(_event("token", text=text))
    # Tool-call shaped events.
    if obj.get("type", "") in ("tool_call", "tool-call", "function_call") or "tool_call" in obj:
        events.append(_event("tool_call", tool=str(obj.get("tool", obj.get("name", "unknown"))), args=obj.get("args", obj.get("arguments", {}))))
    return events


class _SubprocessEngine(EngineAdapter):
    """Shared subprocess plumbing: arg list only, never shell=True."""

    binary: str = ""
    not_installed_reason: str = ""

    def _binary_path(self) -> str | None:
        return shutil.which(self.binary)

    def available(self) -> tuple[bool, str]:
        path = self._binary_path()
        if not path:
            return False, self.not_installed_reason
        return True, f"{self.binary} found at {path}"

    async def _run_jsonl(self, argv: list[str], timeout_s: int) -> AsyncIterator[dict]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        text_parts: list[str] = []
        session_id: str | None = None
        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout_s)
                except TimeoutError:
                    proc.kill()
                    yield _event("error", detail=f"{self.name} engine timed out after {timeout_s}s")
                    return
                if not line:
                    break
                for event in _parse_cli_jsonl(line.decode(errors="replace").strip(), self.name):
                    if event["type"] == "token":
                        text_parts.append(event["text"])
                    if event["type"] == "tool_result" and event.get("tool") == "session":
                        session_id = event["result"]["session_id"]
                        continue  # session id is bookkeeping, not chat output
                    yield event
            await asyncio.wait_for(proc.wait(), timeout=30)
        finally:
            if proc.returncode is None:
                proc.kill()
        if proc.returncode not in (0, None):
            err = ""
            if proc.stderr is not None:
                err = (await proc.stderr.read()).decode(errors="replace")[:500]
            yield _event("error", detail=f"{self.name} exited {proc.returncode}: {err}")
            return
        yield _event(
            "done",
            text="".join(text_parts),
            usage={"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
            engine=self.name,
            session_id=session_id,
        )


class CodexEngine(_SubprocessEngine):
    """OpenAI Codex CLI. Verified: codex-cli 0.164.0-alpha.9 locally."""

    name = "codex"
    binary = "codex"
    not_installed_reason = "codex CLI is not installed (expected at `codex` on PATH)"

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[dict]:
        ok, reason = self.available()
        if not ok:
            yield _event("error", detail=reason)
            return
        workdir = kwargs.get("workdir", ".")
        timeout_s = kwargs.get("timeout_s", 300)
        argv = [
            settings.codex_bin, "exec", "--json",
            "-s", "read-only", "-a", "never",
            "-C", workdir, "--skip-git-repo-check", prompt,
        ]
        async for event in self._run_jsonl(argv, timeout_s):
            yield event

    async def resume(self, session_id: str, prompt: str, **kwargs) -> AsyncIterator[dict]:
        ok, reason = self.available()
        if not ok:
            yield _event("error", detail=reason)
            return
        timeout_s = kwargs.get("timeout_s", 300)
        argv = [settings.codex_bin, "exec", "resume", "--json", session_id, prompt]
        async for event in self._run_jsonl(argv, timeout_s):
            yield event


class PiEngine(_SubprocessEngine):
    """Pi coding-agent CLI (@earendil-works/pi-coding-agent).

    NOT installed on this machine — available() reports that cleanly.
    Invocation verified from upstream docs (not from a local run):
    ``pi -p --mode json`` (print mode, one AgentSessionEvent per JSONL
    line), resume with ``--session <id>`` or ``--continue``.
    Pi asks for no permission before commands/edits, so the backend
    never runs it in a writable workdir and routes its write tools
    through the approval gate.
    """

    name = "pi"
    binary = "pi"
    not_installed_reason = (
        "pi CLI is not installed (npm install -g @earendil-works/pi-coding-agent; "
        "Node >= 22 required). No ~/.pi directory exists on this machine."
    )

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[dict]:
        ok, reason = self.available()
        if not ok:
            yield _event("error", detail=reason)
            return
        timeout_s = kwargs.get("timeout_s", 300)
        argv = [
            settings.pi_bin, "-p", "--mode", "json",
            "--session-dir", settings.pi_session_dir, prompt,
        ]
        async for event in self._run_jsonl(argv, timeout_s):
            yield event

    async def resume(self, session_id: str, prompt: str, **kwargs) -> AsyncIterator[dict]:
        ok, reason = self.available()
        if not ok:
            yield _event("error", detail=reason)
            return
        timeout_s = kwargs.get("timeout_s", 300)
        argv = [
            settings.pi_bin, "-p", "--mode", "json",
            "--session-dir", settings.pi_session_dir,
            "--session", session_id, prompt,
        ]
        async for event in self._run_jsonl(argv, timeout_s):
            yield event


class PocketPalEngine(EngineAdapter):
    """PocketPal AI Local API server on a phone (OpenAI-compatible).

    PocketPal v1.23.0 (Plus) exposes the loaded GGUF model as
    ``POST {base_url}/chat/completions`` with SSE ``data:`` chunks
    (``choices[0].delta.content``) and ``[DONE]`` — the same shape as
    any OpenAI-compatible server. The server is a proof-of-concept:
    experimental, no TLS, LAN-only, and the phone must stay on,
    on the same Wi-Fi, with the model loaded and the server toggled
    on. There is no session/resume support, so ``resume()`` just
    starts a fresh turn (the base implementation).

    Cost is always $0: the model runs on the phone, no cloud key is
    involved. The optional ``POCKETPAL_API_KEY`` is sent only as a
    Bearer header and never logged.

    Model resolution: chat requests default to a grok slug
    (``grok-4.7``), which is not a local model id — so a grok slug
    (or an empty model) resolves to the configured
    ``POCKETPAL_MODEL`` (default ``Qwen2.5-0.5B-Instruct``). A
    non-grok model name is passed through verbatim, so the caller
    can pick another loaded GGUF (e.g. Abliterated Llama 3.2 3B).
    The xAI model registry is deliberately NOT applied here.

    ``available()`` is a real probe (``GET {base_url}/models``,
    2s timeout) and returns False with a reason when the phone is
    unreachable — it never fakes availability, and it never blocks
    startup for longer than the probe timeout.
    """

    name = "pocketpal"

    def __init__(self, *, base_url: str | None = None, model: str | None = None,
                 api_key: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 client: httpx.AsyncClient | None = None):
        self._base_url = (base_url or settings.pocketpal_base_url).rstrip("/")
        self._model = model or settings.pocketpal_model
        self._api_key = api_key if api_key is not None else settings.pocketpal_api_key
        self._transport = transport
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _resolve_model(self, requested: str | None) -> str:
        if not requested or requested in MODELS:
            return self._model
        return requested

    def available(self) -> tuple[bool, str]:
        try:
            # trust_env=False + proxy=None: PocketPal is a LAN-local
            # server and must be reached directly, never via an env
            # proxy (httpx also chokes parsing this VM's no_proxy list
            # otherwise).
            with httpx.Client(timeout=2.0, transport=self._transport,
                              proxy=None, trust_env=False) as client:
                resp = client.get(f"{self._base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            return False, (
                f"PocketPal not reachable at {self._base_url} "
                f"({exc.__class__.__name__}): load a model in PocketPal "
                "and turn on the Local API server"
            )
        if resp.status_code >= 400:
            return False, (
                f"PocketPal at {self._base_url} returned HTTP {resp.status_code}"
            )
        return True, f"PocketPal reachable at {self._base_url}"

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[dict]:
        model = self._resolve_model(kwargs.get("model", "grok-4.7"))
        system = kwargs.get("system", "")
        history = kwargs.get("history") or []
        timeout_s = kwargs.get("timeout_s", 300)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        payload = {"model": model, "messages": messages, "stream": True}
        text_parts: list[str] = []
        prompt_tokens = completion_tokens = 0

        client = self._client
        own_client = client is None
        if own_client:
            # trust_env=False + proxy=None: see available() — LAN-local, direct only.
            client = httpx.AsyncClient(timeout=timeout_s, transport=self._transport,
                                       proxy=None, trust_env=False)
        assert client is not None
        try:
            async with client.stream(
                "POST", f"{self._base_url}/chat/completions",
                json=payload, headers=self._headers(),
            ) as resp:
                if resp.status_code == 429:
                    yield _event("error", detail="PocketPal rate limit (429) — retry with backoff")
                    return
                if resp.status_code >= 400:
                    body = await resp.aread()
                    yield _event(
                        "error",
                        detail=f"PocketPal API {resp.status_code}: "
                               f"{body[:300].decode(errors='replace')}",
                    )
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                    else:
                        # Non-stream fallback: plain JSON body.
                        data = line
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    usage = obj.get("usage") or {}
                    prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                    completion_tokens = usage.get("completion_tokens", completion_tokens)
                    text = _pocketpal_chunk_text(obj)
                    if text:
                        text_parts.append(text)
                        yield _event("token", text=text)
        except httpx.ConnectError as exc:
            yield _event(
                "error",
                detail=f"cannot reach PocketPal at {self._base_url}: {exc}. "
                       "Is the phone on the same Wi-Fi, the model loaded, "
                       "and the Local API server on?",
            )
            return
        except httpx.TimeoutException:
            yield _event(
                "error",
                detail=f"PocketPal request timed out after {timeout_s}s "
                       f"(model {model} on {self._base_url})",
            )
            return
        except httpx.HTTPError as exc:
            yield _event("error", detail=f"PocketPal request failed: {exc}")
            return
        finally:
            if own_client:
                await client.aclose()
        yield _event(
            "done",
            text="".join(text_parts),
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                   "cost_usd": 0.0},
            engine=self.name,
            model=model,
        )


def _pocketpal_chunk_text(obj: dict) -> str:
    """Text carried by one OpenAI-compatible chat-completions chunk."""
    choices = obj.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    choice = choices[0]
    delta = choice.get("delta") or {}
    if isinstance(delta.get("content"), str):
        return delta["content"]
    message = choice.get("message") or {}
    if isinstance(message.get("content"), str):
        return message["content"]
    return ""


class MuseEngine(EngineAdapter):
    """The default engine: Muse itself, in-process.

    There is no ``muse`` CLI binary, so this is not a subprocess — it
    is the orchestrator running inside this backend, backed by the xAI
    provider when a key is configured, otherwise the stub. The
    ``engine`` field in done events always says ``muse`` so the
    frontend and the logs can tell which engine answered.
    """

    name = "muse"

    def available(self) -> tuple[bool, str]:
        return True, "muse engine is always available (xai-backed, or stub at $0)"

    async def stream(self, prompt: str, **kwargs) -> AsyncIterator[dict]:
        provider: EngineAdapter = XaiEngine() if settings.xai_api_key else StubEngine()
        async for event in provider.stream(prompt, **kwargs):
            if event["type"] == "done":
                event = {**event, "engine": "muse", "provider": provider.name}
            yield event


ENGINES: dict[str, type[EngineAdapter]] = {
    "muse": MuseEngine,
    "xai": XaiEngine,
    "codex": CodexEngine,
    "pi": PiEngine,
    "pocketpal": PocketPalEngine,
    "stub": StubEngine,
}


def get_engine(name: str | None = None) -> EngineAdapter:
    """Return the adapter for ``name`` (default: settings.default_engine)."""
    key = (name or settings.default_engine or "muse").lower()
    cls = ENGINES.get(key)
    if cls is None:
        raise ValueError(f"unknown engine {key!r}; available: {sorted(ENGINES)}")
    return cls()


def engine_status() -> list[dict]:
    """Availability of every engine, for /health and debugging."""
    return [
        {"engine": key, "available": cls().available()[0], "detail": cls().available()[1]}
        for key, cls in ENGINES.items()
    ]
