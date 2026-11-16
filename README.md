# Crocbot X

> **Rename before release:** "Crocbot X" is a working name only. Rename the project before wrapping up / publishing, to avoid trademark / copyright conflicts. Update the name, package, docs, and repo slug together.

Open-source, crew-run bot. Pay disabled, logged, revocable.

## What it is
A small bot scaffold: watch → judge → log → rest.
No money moves. No live payments. Mainnet off.

## Quick start
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 -m crocbot_x --dry-run
```

## Structure
- `src/crocbot_x/` — bot core
- `tests/` — tests
- `docs/` — docs
- `examples/` — examples

## Rules
- One pilot. Log first. Pay disabled until written go.
- Never store secrets in the repo. Use `.env`, never commit it.

## License
MIT — see LICENSE.
