# Crocbot X

> **Rename before release:** "Crocbot X" is a working name only. Rename the project before wrapping up / publishing, to avoid trademark / copyright conflicts. Update the name, package, docs, and repo slug together.

Open-source, crew-run bot. Pay disabled, logged, revocable.

## What it is
A self-hosted Grok-style bot system: a FastAPI backend on your Linux
VPS calling the xAI Responses API, with a roster of bots (Time
Auditor first), an agent loop (watch → judge → log → rest), durable
resumable runs, and a trust layer that reviews every destructive or
outward-facing action before it runs. No money moves. No live
payments. Mainnet off.

**Architecture priorities, in order:** 1) isolation, 2) resource
limits, 3) clean state — principles, not workers. One worker per bot,
every worker gets all three: its own container, CPU/memory/disk caps,
and a fresh ephemeral scratch workspace per run.

## Quick start (backend)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
# /health, /ready, /metrics, /docs — dry-run chat at $0, no key needed
```

Or the original scaffold dry-run:
```bash
python3 -m crocbot_x --dry-run
```

## Structure
- `backend/` — FastAPI service (see `docs/BACKEND.md`)
  - `app/main.py` — app, `/health`, `/ready`
  - `app/agent/` — engines (muse/codex/pi/pocketpal/stub/xai), loop, durable runs, guardrails, tools, pay guard, per-bot worker
  - `app/routers/` — chat (SSE), bots, approvals, media, metrics
  - `tests/` — 46 pytest tests
- `src/crocbot_x/` — original scaffold core
- `docs/` — `BACKEND.md` (architecture), `ROADMAP.md`
- `docker-compose.yml` — Caddy + api + per-bot workers + Postgres + Redis (VPS)
- `Caddyfile`, `deploy/crocbot-x.service` — VPS deploy files
- `examples/` — (planned)

## Rules
- One pilot. Log first. Pay disabled until written go.
- Never store secrets in the repo. Use `.env`, never commit it.
- Destructive/outward actions need founder review first; timeout fails closed.
- The working chat UI does not exist yet — the page in `ts-spaces`
  is a design blueprint, not a UI.

## License
MIT — see LICENSE.
