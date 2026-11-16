"""Per-bot worker — one worker per bot.

Architecture principle (founder ranking): isolation, resource limits,
clean state are PRINCIPLES, not workers. There is ONE worker per bot,
and every worker gets all three:
1. Isolation — its own container (see docker-compose.yml, one service
   per bot on the VPS) and its own process here.
2. Resource limits — CPU/memory/disk caps from compose
   (deploy.resources) plus the in-code caps (engine timeout, max
   steps, daily budget, disk-critical write pause).
3. Clean state — every run starts from a fresh ephemeral scratch
   workspace (see loop.make_scratch_workdir), reset between runs; no
   leftover state leaks into the next run.

Usage (VPS, per bot — the deploy agent creates one service per bot):
    python -m app.agent.worker --bot time-auditor --once
    python -m app.agent.worker --bot time-auditor --interval-s 86400
The Time Auditor's daily background run is a scheduled invocation of
this worker. On-demand runs go through POST /api/v1/chat instead.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

sys.path.insert(0, ".")

from app.agent.loop import run_turn
from app.config import settings
from app.db import Conversation, SessionLocal, get_bot, init_db, seed_bots
from app.observability import check_crash_loop, check_disk, get_logger, log_event, set_log_context

logger = get_logger("crocbot.worker")

DAILY_AUDIT_PROMPT = (
    "Run your daily background audit: review the day, flag time leaks, "
    "log each one with a timestamp and a one-line note. Log only — do not "
    "message anyone, do not interrupt, do not ask for permission."
)


async def run_bot_once(bot_id: str) -> str:
    """Run one scheduled turn for a bot. Returns the run's stop status."""
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
        bot = get_bot(db, bot_id)
        if bot is None:
            log_event(logger, "error", "worker: unknown bot", bot_id=bot_id)
            return "error: unknown bot"
        if bot.paused:
            log_event(logger, "warning", "worker: bot paused, skipping run",
                      bot_id=bot_id, reason=bot.paused_reason)
            return "skipped: paused"
        check_disk(db)
        alert = check_crash_loop(db, bot_id)
        if alert is not None:
            return "skipped: crash-loop pause"
        conversation = Conversation(bot_id=bot_id, title=f"Scheduled run — {bot.name}")
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        prompt = DAILY_AUDIT_PROMPT if bot_id == "time-auditor" else (
            f"Run your scheduled background turn for {bot.name}. Log only."
        )
        set_log_context(bot_id=bot_id)
        status = "completed"
        async for event in run_turn(
            db, conversation=conversation, bot_id=bot_id, bot_name=bot.name,
            system_prompt=bot.system_prompt, user_text=prompt,
            engine_name=settings.default_engine, model=bot.model,
        ):
            if event["type"] == "error":
                status = f"error: {event.get('detail', '')}"
            if event["type"] == "done":
                status = str(event.get("status", "completed"))
        log_event(logger, "info", "worker: scheduled run finished",
                  bot_id=bot_id, status=status)
        return status
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="crocbot-worker")
    parser.add_argument("--bot", required=True, help="bot id, e.g. time-auditor")
    parser.add_argument("--once", action="store_true", help="run once and exit")
    parser.add_argument("--interval-s", type=int, default=86400,
                        help="seconds between runs in daemon mode (default: 86400 = daily)")
    args = parser.parse_args()
    status = asyncio.run(run_bot_once(args.bot))
    if args.once:
        return 0 if status.startswith(("completed", "skipped")) else 1
    while True:
        time.sleep(args.interval_s)
        asyncio.run(run_bot_once(args.bot))


if __name__ == "__main__":
    raise SystemExit(main())
