# Crocbot X — Backend Architecture

> **Rename before release:** "Crocbot X" is a working name only. Rename
> the project, package, docs, and repo slug together before wrapping
> up / publishing, to avoid trademark / copyright conflicts.

Status: v0.2 backend built and tested locally (46 pytest tests pass,
live smoke test passed). No VPS deploy, no real API key wired, no
working chat UI yet — see "What is still missing".

## 0. Founder architecture priorities — read first

Ranked, in this order:

1. **Isolation**
2. **Resource limits**
3. **Clean state**

These three are **PRINCIPLES, not workers**. There is **one worker per
bot**, and **every worker gets all three**:

| Principle | What it means here |
|---|---|
| 1. Isolation | Each bot's worker runs in **its own container** (one compose service per bot, e.g. `worker-time-auditor`) and its own process. Bots share no filesystem, no memory, no credentials beyond the server-side `.env`. A bot that crashes cannot take another bot down. |
| 2. Resource limits | Every worker is capped on **CPU / memory / disk**: compose `deploy.resources.limits` (api 1 CPU / 512M, workers 0.5 CPU / 256M), the systemd unit (`MemoryMax=512M`, `CPUQuota=100%`), plus in-code caps (engine timeout 300s, max 10 steps per turn, $5/user/day budget, disk-critical write pause at 90%). |
| 3. Clean state | Every run starts from a **clean scratch state**: a fresh ephemeral workspace (`data/work/<run-id>-*/`, empty) created per run and **deleted when the run ends** (`loop.make_scratch_workdir` + `run_turn` finally-block). No leftover files, no leftover state leaking into the next run. |

The deploy agent creates one compose service per bot from the
`worker-time-auditor` template in `docker-compose.yml`.

## 1. What this backend is

A Python FastAPI service (`backend/`) that owns the agent loop,
the bot roster, approvals, durable runs, and the budget. It is built
**on top of a cloud code CLI, agent-agnostic by design**: one
`EngineAdapter` interface, interchangeable engines. In our case the
default engine is **Muse**; **Codex CLI** and **Pi** are interchangeable
engines behind the same interface.

```
browser / desktop UI (NOT built yet — see §9)
        │  HTTPS (Caddy on VPS) / http://127.0.0.1:8000 locally
        ▼
FastAPI  backend/app/main.py  (/health, /ready, /metrics, /api/v1/*)
        ├─ routers: chat (SSE), bots, approvals, media, metrics
        ├─ agent/loop.py      watch -> judge -> log -> rest
        ├─ agent/engines.py   muse / xai / codex / pi / stub
        ├─ agent/guardrails.py  trust layer (review BEFORE action)
        ├─ agent/durable.py   Pi-Durable-style run journal
        ├─ agent/tools.py     read vs write tool registry
        ├─ agent/payguard.py  pay hard-disabled, $0 moved
        ├─ agent/worker.py    one worker per bot (scheduled runs)
        ├─ observability.py   JSON logs, stats, crash-loop + disk alerts
        └─ db.py              SQLAlchemy: SQLite (dev) / Postgres (VPS)
```

## 2. CLI research — Codex + Pi (verified, with gaps)

### Codex CLI — verified locally

- Installed here: **codex-cli 0.164.0-alpha.9** at
  `/opt/hatch-image/bin/codex` (`codex --version`, `codex exec --help`).
- Non-interactive mode: `codex exec --json <prompt>` — JSONL events
  on stdout, progress on stderr, exits non-zero on error. Alias: `codex e`.
- Resume: `codex exec resume --json <session-id> <prompt>` (or
  `--last`); `codex exec fork` forks a session.
- Sandbox (`-s`): `read-only` (default for exec), `workspace-write`,
  `danger-full-access`. Approval policy (`-a`): `on-request`, `never`.
  Headless default: approvals auto-cancel. `--full-auto` =
  workspace-write + on-request.
- **We never pass `--dangerously-bypass-approvals-and-sandbox` or
  `--yolo`.** The backend always runs Codex `-s read-only -a never`,
  in the run's ephemeral scratch workdir, with `--skip-git-repo-check`.
- Sources: `developers.openai.com/codex/noninteractive/` (and
  `/codex/security`), local `codex --help` / `codex exec --help`
  output, and the `codex-rs/exec` behaviour documented at
  developertoolkit.ai/en/codex/automate/non-interactive/.

### Pi — two different things, do not conflate them

**Pi coding-agent CLI** (`@earendil-works/pi-coding-agent`, repo
`earendil-works/pi`, ex-`badlogic/pi-mono`):
- **NOT installed on this machine** — verified: `which pi` finds
  nothing, no `~/.pi` directory. The adapter reports that cleanly
  instead of faking a run.
- Invocation (verified from upstream docs, not from a local run):
  `pi -p --mode json` — print mode, one `AgentSessionEvent` per JSONL
  line; `--mode rpc` is its native stdio embedding protocol.
- Sessions: JSONL transcripts, version-3 format, under
  `~/.pi/agent/sessions/<cwd-slug>/`; resume with `--session <id>`
  (partial id works), `--continue` (latest for the cwd), `--resume`
  (picker), `--fork`, `--session-dir`, `--no-session` (ephemeral).
  Config dir override: `PI_CODING_AGENT_DIR` / `PI_SESSION_DIR`.
- Default tools: `read`, `bash`, `edit`, `write` (+ `grep`, `find`,
  `ls` off by default). **Pi asks for NO permission before running a
  command or editing a file** — that is why the backend never runs Pi
  in a writable workdir and routes its write tool calls through our
  own approval gate first.
- Sources: github.com/earendil-works/pi, pi.dev docs, the pi-mono-docs
  CLI reference, docs.kanaries.net Pi 1.0 guide.

**Pi Durable** (the crash-safe runtime — what the backend copies):
- Experimental TypeScript library from Earendil (Pi 1.0, 1 Oct 2026;
  Pi 1.5 with Agents SDK 0.33). Every model request and tool call
  becomes a small task that saves a checkpoint **before** the agent
  moves on. State lives in durable storage (SQLite tables prefixed
  `pi_` in a Cloudflare Durable Object via `PiHarness`, or JSONL /
  IndexedDB locally). After a crash, a new process opens the same
  storage and continues from the last checkpoint.
- Completed steps reuse recorded results; interrupted steps run again.
  Restart limit (default **5**) ends a task that keeps crashing.
  `session.continue()` starts a new run from the existing transcript
  without a new user message. Runs report explicit stop reasons
  (`turn_limit` / `restart_limit`).
- Streaming updates persist in batches (~1s; ~27x fewer SQLite
  transactions in Pi 1.5) — a crash can lose ~1s of streamed output.
- **Gaps Pi Durable does NOT solve** (Cloudflare docs say so
  plainly): **no built-in tool approval step** — our guardrails sit
  on top of the journal. Pi 1.5 data cannot be read by Pi 1.4.
- Sources: developers.cloudflare.com/agents/harnesses/pi/,
  Agents SDK changelog 0.33.0 (Nov 2026), halmob.de Pi Durable explainer.
- Our port (`app/agent/durable.py`): same journal semantics in
  SQLite (`runs` / `run_steps`), restart limit 5, same stop reasons,
  same replay rule — completed replayable steps are re-used, never
  re-executed.

### Gaps / not verified

- The exact Codex `--json` event schema varies by version; the
  adapter parses JSONL generically (text carriers `message` / `text` /
  `delta` / `output`, session ids, tool-call shapes) and ignores
  unknown lines. Capture a real Codex run's JSONL before tightening
  the parser.
- The exact Pi `--mode json` event schema and which field carries the
  resume/session id: upstream integrators use the first `session.id`
  JSONL event. Must be captured from a real Pi run before wiring
  resume — Pi is not installed here, so this is unverified.
- No `muse` CLI binary exists on this machine (`which muse` finds
  nothing). The **Muse engine is the in-process orchestrator**,
  backed by the xAI Responses API when a key is set, otherwise the
  stub. It is not a subprocess.

## 3. API contract (matches the design blueprint page)

The blueprint page (`~/workspace/ts-spaces/crocbot-x/index.html`) is a
**design document, not a working UI**. The backend implements its
contract; the working chat UI does not exist yet (§9).

| Endpoint | What it does |
|---|---|
| `GET /` | Service info, engine, pay status |
| `GET /health` | Liveness, no DB |
| `GET /ready` | DB check + engine availability |
| `GET /metrics` | Per-bot status, CPU/mem, restarts, disk, alerts, engines |
| `POST /api/v1/chat` | SSE stream: `token`, `tool_call`, `tool_result`, `citation`, `approval_required`, `done`, `error` |
| `GET/POST /api/v1/conversations` | List / create |
| `GET/POST/DELETE /api/v1/conversations/{id}[/messages]` | Read / append message / delete |
| `GET /api/v1/runs/{id}` | Run + journal steps |
| `POST /api/v1/runs/{id}/resume` | Pi `session.continue()`: resume from the journal |
| `GET/POST/PUT/DELETE /api/v1/bots[/{id}]` | Roster CRUD (hidden excluded by default) |
| `GET /api/v1/approvals` | List (pending/approved/denied/expired) |
| `POST /api/v1/approvals/{id}/approve` | Founder-only |
| `POST /api/v1/approvals/{id}/deny` | Founder-only |
| `GET /api/v1/approvals/audit/log` | Full review audit trail |
| `POST /api/v1/images` | **501 stub** — Imagine path unverified, see §7 |
| `GET /api/v1/voice/realtime` | Voice contract (stub) |
| `WS /api/v1/voice/ws` | Accepts, sends error frame, closes — stub, not a relay |
| `GET /api/v1/alerts` | List alerts |
| `POST /api/v1/alerts/{id}/acknowledge` | Acknowledge an alert |

Chat request: `{message, conversation_id?, bot_id?, engine?, model?,
user_id?}`. Model is validated against the registry (§6) for
xAI-backed engines only — retired slugs return 400, unknown slugs
return 400. Other engines use their own models (PocketPal: a local
GGUF id). Daily budget ($5 default,
hard stop) returns 429 when exhausted. A paused bot returns 423.

## 4. Bot roster (from the Grok Bot reference image)

Roster seeded from `~/workspace/user/files/1000005461_0_k5vt.png`:
Calorie Tracker, Technical Explainer, Time Auditor, David Todoist,
GitHub Viewer, YouTube Titles, New Discord Manager. "Hidden Bots 7"
is a count on that image, not seven named bots — seven hidden
placeholder entries are seeded (`hidden-bot-1..7`, `hidden=true`,
excluded from the default list).

### Time Auditor — first bot, founder-decided mode

**Runs BOTH ways: once daily in the background, quiet, log-only,
plus on-demand whenever the founder asks.**

- It does **not** ask anyone else anything, does **not** message or
  interrupt anyone, and does **not** ask the founder for permission
  each run.
- It watches, flags time leaks, logs them. The founder reviews the
  log when he wants.
- No other bot gets asked either (the same flags are the default for
  the roster; other bots are on-demand and still need founder review
  for write actions — see §5).
- Recorded in the roster config: `schedule_mode =
  "daily_background+on_demand"`, `schedule_cron = "0 9 * * *"`,
  `asks_permission = false`, `interrupts_user = false`,
  `messages_others = false`, model `grok-4.3`.
- The daily run is `python -m app.agent.worker --bot time-auditor`
  (one worker, its own container on the VPS). On-demand runs go
  through `POST /api/v1/chat` with `bot_id: "time-auditor"`.

## 5. Trust layer — guardrails / review (REQUIRED, not optional)

File: `backend/app/agent/guardrails.py`. Every destructive or
outward-facing action goes through review **BEFORE** it runs:

| Action | Review? |
|---|---|
| `delete_file`, `run_shell` (writes/deletes) | Yes — always |
| `send_email`, `post_message` (X / any external service) | Yes — always |
| `pay` (spending money) | Yes — and refused outright by the PayGuard anyway |
| `web_search`, `fetch_url`, `read_file` (reads) | No — reads stay automatic |

Rules, enforced in code and tested:

- **No auto-approve** for review actions. No bot self-approval:
  `decide_review()` accepts only `decided_by == "founder"`; a bot
  id, a worker, or any other caller gets 403. Unknown actions are
  treated as review-required (fail closed).
- **Timeout fails closed**: pending past `expires_at` (default 900s)
  is marked `expired`; the waiting run stays `awaiting_approval`;
  a late founder approval cannot revive it; the action never runs.
- **Every request and decision is logged** in the `review_audit`
  table (bot, action, target, reason, decision, decided_by) and in
  the structured JSON log. Nothing in that table is ever deleted.
  Read it at `GET /api/v1/approvals/audit/log`.
- The agent loop pauses on a write tool call: it emits
  `approval_required` with the approval id, ends the turn as
  `awaiting_approval`, and only runs the tool after
  `POST /api/v1/runs/{id}/resume` finds an approved review — exactly
  once, from the journal.
- **Pay guard** (`agent/payguard.py`): `PAY_ENABLED = False`,
  hard-coded — not an env var, not a config flag. `attempt_pay()`
  always raises. $0 moved. Any future go is implemented by editing
  that file, never by flipping a setting.

## 6. Models + provider

xAI Responses API (`POST https://api.x.ai/v1/responses`), server-side
tools `web_search` + `x_search` first (the model orchestrates them).
Source: deep-research report 2026-11-16
(`~/workspace/research_notes/crocbot-x-grok-research-20261116-0156/report.md`).

| Model | Role | Price /1M in/out |
|---|---|---|
| `grok-4.7` | Flagship, default | $2.00 / $6.00 |
| `grok-4.3` | Cheap all-rounder | $1.25 / $2.50 |
| `grok-build-0.1` | Coding | $1.00 / $2.00 |

**Retired — refused, never redirected:** `grok-4`, `grok-4-fast`,
`grok-4-1-fast-*`, `grok-code-fast-1` (retired 15 May 2026). Unknown
slugs are refused too. Long context (≥200k prompt tokens) doubles
the request — history is truncated to the last 20 messages.
No key configured → the stub engine answers at $0 (dry-run).

The registry check applies only to xAI-backed engines (`muse`,
`xai`). Other engines use their own models, so
`POST /api/v1/chat` does not reject a local GGUF id when
`engine` is `pocketpal` (or `codex` / `pi` / `stub`).

### 6.1 PocketPal engine — offline local model on a phone

Engine `pocketpal` (`backend/app/agent/engines.py`,
`PocketPalEngine`) talks to the PocketPal AI **Local API server
(Plus)** — OpenAI-compatible `POST {base_url}/chat/completions`,
SSE `data:` chunks (`choices[0].delta.content`), `[DONE]`, with a
plain-JSON non-stream fallback. Cost is always $0: the model runs
on the phone, no cloud key. `available()` probes
`GET {base_url}/models` (2s timeout) and reports unreachable
honestly instead of faking a run. The client never uses a proxy
(`trust_env=False`, `proxy=None`): the phone is LAN-local and
must be reached directly.

Phone-side setup (founder does this on the phone; Muse cannot
install from pairing):

1. Install **PocketPal AI v1.23.0** from the Play Store
   (`com.pocketpalai`).
2. Download the **Qwen2.5-3B-Instruct Q4_K_M** GGUF
   (1.92GB, `bartowski/Qwen2.5-3B-Instruct-GGUF`) in-app from
   Hugging Face and load it. The 0.5B model in the code default
   was skipped — the founder's Razr Ultra 2025 (16GB RAM + 4GB
   Boost) runs the 3B comfortably at ~25 tokens/sec offline.
3. Turn on the **Local API server (Plus)** in PocketPal.
   Default listener is `127.0.0.1:8080`; on Wi-Fi it serves at
   `http://192.168.1.247:8080/v1` (that LAN IP changes with the
   network — use the Tailscale address below instead).
4. Keep the phone plugged in, PocketPal open with the model
   loaded — the server stops / the model unloads when the app
   is backgrounded for a while. If the API says `no_model`,
   go back to Models, tap Load on Qwen 3B, get an answer in
   chat first, then retry.
5. Point the backend at it (server `.env`, never in git):
   `POCKETPAL_BASE_URL=http://100.121.19.70:8080/v1`
   `POCKETPAL_MODEL=Qwen2.5-3B-Instruct-Q4_K_M`
   `POCKETPAL_API_KEY=` (empty unless you set one in PocketPal)
   then `CROCBOT_ENGINE=pocketpal` (or per-request
   `"engine": "pocketpal"`).

**Tailscale setup — live, Nov 15, 2026 (the new setup):**
phone is `razorwudo` at `100.121.19.70` on the
`motorazrjem@gmail.com` tailnet; this VM joined as `muse` at
`100.79.72.72`. Install Tailscale on any backend host (VPS
included) and join the same tailnet — then the backend reaches
the phone directly at `http://100.121.19.70:8080/v1`, over
Wi-Fi **or 5G** (both verified live). This VM reaches it only
through its Tailscale proxy (port 3130); the engine client
currently sets `trust_env=False, proxy=None`, so a direct
`POST /api/v1/chat` with `engine: "pocketpal"` from *this* VM
still needs proxy support or a host with native Tailscale.
On a VPS with `tailscaled` running, no proxy is needed.

Honest limits:

- The Local API server is a **proof-of-concept / experimental**
  feature in v1.23.0, **no TLS** — never expose the phone's
  port to the internet. Tailscale only.
- **Qwen 3B is the live model.** Fine for simple tasks and
  Time Auditor (short summaries, flagging); weak at heavy tool
  use, long reasoning, and browsing. **Abliterated Llama 3.2 3B**
  (~2GB) carries more — set `POCKETPAL_MODEL` to its loaded id
  to use it. Neither replaces the xAI engines for hard work.
- **Live PocketPal testing DONE Nov 15, 2026, ~21:18 and
  ~21:21 MST, via Tailscale.** Direct `POST
  http://100.121.19.70:8080/v1/chat/completions` returned HTTP
  200 twice: on Wi-Fi Qwen answered "Hello! How can I", on
  5G (Wi-Fi off) it answered "Hello there". Model in the
  response: `bartowski/Qwen2.5-3B-Instruct-GGUF/Qwen2.5-3B-Instruct-Q4_K_M.gguf`.
  `GET /v1/models` returns an empty reply — that endpoint is
  not implemented in the POC; `chat/completions` is the working
  one, and `available()` probing `/models` will report
  unreachable even when chat works. PocketPal's "Allow other
  devices" toggle flips OFF when Serve turns ON in this build,
  but the Tailscale connection works anyway.
- A grok slug in a chat request (`grok-4.7` default) is not a
  local model id, so the engine resolves it to the configured
  `POCKETPAL_MODEL`. A non-grok model name is passed through
  verbatim.

Example curl against the phone over Tailscale (from a host on
the same tailnet — no proxy needed there; this VM uses its
Tailscale proxy on port 3130):

```bash
curl -N -X POST http://100.121.19.70:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen2.5-3B-Instruct-Q4_K_M", "messages": [{"role": "user", "content": "hello"}], "stream": true}'
# Through the backend instead (backend joined to the tailnet):
curl -N -X POST localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "hello", "engine": "pocketpal", "model": "Qwen2.5-3B-Instruct-Q4_K_M"}'
```

## 7. What is still missing (do not claim otherwise)

- **No working chat UI.** `~/workspace/ts-spaces/crocbot-x/index.html`
  is a design blueprint page (models, pricing, calculator, code
  blocks). It is not a chat UI and it does not call this backend.
  The React/Vite UI from the blueprint is unbuilt.
- **No VPS deploy.** `docker-compose.yml`, `Caddyfile`,
  `deploy/crocbot-x.service` are files only. Docker is not installed
  on this machine, so the compose stack was validated as YAML only —
  never `docker compose up`. The deploy agent runs it on the VPS.
- **No real API key.** `XAI_API_KEY` is unset here; live xAI calls
  were never made. First engineering step on the VPS: `GET /v1/models`
  + one `POST /v1/responses` with `web_search`, using a real key.
- **Live PocketPal test DONE (direct curl only).** The phone
  answers `POST /v1/chat/completions` over Tailscale (see
  §6.1). What is NOT done yet: one `POST /api/v1/chat` with
  `engine: "pocketpal"` through the backend itself — that needs
  a host with native Tailscale (VPS) or proxy support in the
  engine client, and `available()` probing `/models` will
  still report unreachable (that endpoint is not implemented
  in the PocketPal POC).
- **Images:** `POST /api/v1/images` returns 501. The exact xAI
  Imagine REST path was not verified in the research pass (model IDs
  + prices verified, path not). Confirm at
  docs.x.ai/developers/model-capabilities/images/generation first.
- **Voice relay:** stub only. The WebSocket accepts, sends an error
  frame, and closes.
- **No auth in v1.** Single-user service. Bind to localhost locally;
  the deploy agent adds auth (and real founder identity for
  approvals) before exposing it. The `FOUNDER_ID = "founder"` check
  in guardrails is a placeholder for that auth.
- **Postgres path:** same SQLAlchemy models, selected by
  `DATABASE_URL`. The Postgres branch was not run here (no Postgres
  locally); compose wires it on the VPS.
- **Rate limiting** is in-memory per IP (Redis on the VPS).
  **web_search tool** is a stub (no network) until xAI `web_search` /
  Tavily is wired behind it.

## 8. Observability

File: `backend/app/observability.py`. One central stream, no
per-bot log files.

### Logs — journald

- The service writes **structured JSON lines to stdout** (one JSON
  object per line: `ts`, `level`, `logger`, `msg`, `bot_id`,
  `run_id`, plus event fields). Under systemd the unit sets
  `StandardOutput=journal` / `StandardError=journal`, so **journald
  is the central store**.
- Per-run detail also lands in `data/runs/<run_id>.jsonl` (one file
  per run, not per bot), flushed per step, not per token (Pi 1.5
  batches the same way — a crash can lose ~1s of streamed output).
- Secrets are never logged (`security.redact`).

**journald setup (VPS, systemd unit in `deploy/crocbot-x.service`):**
```bash
sudo cp deploy/crocbot-x.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crocbot-x
journalctl -u crocbot-x -f -o json            # follow, JSON
journalctl -u crocbot-x -o json | jq 'select(.bot_id=="time-auditor")'
journalctl -u crocbot-x --since "1 hour ago" -p warning
```
For docker compose instead, the same JSON lines go to the container
stdout: `docker compose logs -f api` / `docker compose logs -f
worker-time-auditor`, or forward them to journald with
`logging.driver: journald` in compose (deploy agent's call).

### Stats — CPU / memory / disk

- Per-bot: runs, restarts in the last 10 min, last run status, paused
  state — from the DB (`observability.bot_status`).
- Process: CPU seconds + RSS from the container cgroup v2
  (`memory.current`, `cpu.stat`) when present, else `/proc/self`.
- Disk: `shutil.disk_usage` on the data dir.
- All of it is exposed at `GET /metrics` (JSON), plus `GET /ready`
  (engine availability) and `GET /api/v1/alerts`.

### Alerts

| Alert | Threshold | Action |
|---|---|---|
| Crash-loop | **3 restarts / failed runs in 10 minutes** | Bot is **paused** (stopped, not restarted forever) + critical alert. Founder unpauses it when fixed. |
| Disk warn | **80%** used | Warn alert. |
| Disk critical | **90%** used | Critical alert + **non-essential writes pause** (write tools refuse until disk is freed). |

Alerts live in the `alerts` table: `GET /api/v1/alerts`,
`POST /api/v1/alerts/{id}/acknowledge`.

## 9. Run it locally

```bash
cd backend
../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# /health, /ready, /metrics, /docs
# Chat (dry-run, stub engine, $0):
curl -N -X POST localhost:8000/api/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello there","bot_id":"time-auditor"}'
# One scheduled Time Auditor run:
../.venv/bin/python -m app.agent.worker --bot time-auditor --once
# Tests:
../.venv/bin/python -m pytest backend/tests -q   # from repo root
```

## 10. Deploy notes (for the deploy agent)

1. Ubuntu 24.04 VPS, 2 vCPU / 4 GB RAM minimum (xAI-hosted LLM).
   UFW: 22/80/443 only, SSH key-only + fail2ban.
2. `git clone` the repo, `cp .env.example .env`, fill it **on the
   server**, `chmod 600 .env`. Never commit a real key.
3. `docker compose up -d --build`. Caddy provisions TLS for
   `$CROCBOT_DOMAIN`. Postgres/Redis are not host-exposed.
4. **Add one compose service per bot** from the
   `worker-time-auditor` template (one worker per bot; each gets
   isolation + resource limits + clean state).
5. Add auth in front of the API before exposing it, and wire real
   founder identity into `guardrails.FOUNDER_ID`.
6. Smoke test on the VPS: `GET /v1/models` + one
   `POST /v1/responses` with `web_search` using the real key.
7. Backups: nightly `pg_dump` off-site; test the restore.
8. If using systemd instead of compose, install
   `deploy/crocbot-x.service` (journald logging, restart burst
   limit 3 per 10 min, `MemoryMax=512M`).

## 11. File map

```
backend/
  app/main.py            FastAPI app, CORS allow-list, rate limit, /health, /ready
  app/config.py          settings from env/.env (stdlib .env parser)
  app/models.py          xAI model registry + retired-slug refusal + pricing
  app/security.py        secret redaction, SSRF allow-list, rate limiter
  app/db.py              SQLAlchemy models, engine, bot roster seed
  app/observability.py   JSON logs, cgroup stats, disk + crash-loop alerts
  app/agent/engines.py   EngineAdapter: muse / xai / codex / pi / pocketpal / stub
  app/agent/loop.py      watch -> judge -> log -> rest + ephemeral scratch
  app/agent/durable.py   Pi-Durable-style run journal + resume/continue
  app/agent/guardrails.py  trust layer: review BEFORE action, founder-only
  app/agent/tools.py     tool registry (read auto, write needs review)
  app/agent/payguard.py  pay hard-disabled
  app/agent/worker.py    one worker per bot (scheduled runs)
  app/routers/chat.py    SSE chat, conversations, runs + resume
  app/routers/bots.py    roster CRUD
  app/routers/approvals.py  founder-only approve/deny + audit log
  app/routers/media.py   images (501 stub) + voice (stub)
  app/routers/metrics.py /metrics + alerts
  requirements.txt       fastapi, uvicorn, httpx, sqlalchemy, psycopg, pytest
  Dockerfile             python:3.12-slim, non-root, healthcheck
  tests/                 55 tests (models, payguard, tools, engines,
                         durable, guardrails, observability, API)
docker-compose.yml       caddy, api, worker-time-auditor (+ template), postgres, redis
Caddyfile                {$CROCBOT_DOMAIN} -> api:8000
deploy/crocbot-x.service systemd unit (journald, restart limits, caps)
docs/BACKEND.md          this file
```
