"""Durable runs — the Pi Durable pattern, ported to Python/SQLite.

What Pi Durable does (sources: developers.cloudflare.com/agents/
harnesses/pi/ ; Pi Durable 1.5 changelog, Agents SDK 0.33):
- Every model request and tool call becomes a small task that saves a
  checkpoint BEFORE the agent moves on.
- State lives in durable storage (Pi: SQLite tables prefixed ``pi_``;
  here: the ``runs`` / ``run_steps`` tables) so a new process can open
  the same storage and continue from the last checkpoint.
- Completed steps reuse their recorded results after a restart;
  interrupted steps run again. A step that crashes the process every
  time is ended after a restart limit (Pi default: 5).
- ``session.continue()`` starts a new run from the existing transcript
  without a new user message; runs report an explicit stop reason
  (turn_limit / restart_limit / completed / interrupted / error).
- Streaming updates are persisted in batches (Pi 1.5: ~1s batches,
  ~27x fewer SQLite transactions); a crash can lose ~1s of streamed
  output. We do the same: the JSONL run log is flushed per step, not
  per token.
- Gaps Pi Durable does NOT solve (Cloudflare docs say so plainly):
  no built-in tool approval step — our approval gate sits on top of
  the journal, it is not part of it. Pi Durable is experimental and
  its API can change in minor releases; Pi 1.5 data cannot be read by
  Pi 1.4.

Our port: same journal semantics, same restart limit (5), same stop
reasons, same replay rule — completed replayable steps are re-used,
never re-executed. Non-replayable steps (a live tool call that already
had a side effect) are marked as such and reported, not silently
re-run.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from sqlalchemy.orm import Session

from ..config import settings
from ..db import Run, RunStep, _utcnow

RESTART_LIMIT = 5  # Pi Durable default
STOP_REASONS = ("completed", "turn_limit", "restart_limit", "interrupted", "error", "awaiting_approval")


def create_run(db: Session, *, conversation_id: str | None, bot_id: str | None,
               engine: str, model: str) -> Run:
    run = Run(conversation_id=conversation_id, bot_id=bot_id, engine=engine, model=model)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def checkpoint(db: Session, run: Run, seq: int, kind: str, payload: dict,
               *, replayable: bool = True, status: str = "completed") -> RunStep:
    """Save one step BEFORE moving on. This is the durability primitive."""
    step = RunStep(run_id=run.id, seq=seq, kind=kind, payload=payload,
                   status=status, replayable=replayable)
    db.add(step)
    run.steps_total = seq
    run.updated_at = _utcnow()
    db.commit()
    return step


def load_journal(db: Session, run_id: str) -> list[RunStep]:
    return list(
        db.query(RunStep).filter(RunStep.run_id == run_id).order_by(RunStep.seq).all()
    )


def finish_run(db: Session, run: Run, *, status: str, stop_reason: str) -> Run:
    run.status = status
    run.stop_reason = stop_reason
    run.updated_at = _utcnow()
    db.commit()
    db.refresh(run)
    return run


def resume_run(db: Session, run_id: str) -> tuple[Run, list[RunStep]]:
    """Reopen a run after a restart/interruption (Pi: resume()).

    Returns (run, completed replayable steps). Raises RuntimeError if
    the restart limit is hit — the run is then ended with stop reason
    restart_limit instead of being retried forever.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise ValueError(f"unknown run {run_id!r}")
    run.restart_count += 1
    if run.restart_count > RESTART_LIMIT:
        finish_run(db, run, status="failed", stop_reason="restart_limit")
        raise RuntimeError(
            f"run {run_id} hit the restart limit ({RESTART_LIMIT}); "
            "ended with stop_reason=restart_limit"
        )
    run.status = "running"
    run.stop_reason = None
    run.updated_at = _utcnow()
    db.commit()
    steps = [s for s in load_journal(db, run_id) if s.status == "completed" and s.replayable]
    return run, steps


def continue_run(db: Session, run_id: str) -> tuple[Run, list[RunStep]]:
    """Pi's session.continue(): new run from the existing transcript,
    without a new user message. Same as resume_run here."""
    return resume_run(db, run_id)


def run_log_path(run_id: str) -> str:
    """Per-run JSONL log (flushed per step, not per token)."""
    directory = os.path.join(settings.data_dir, "runs")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{run_id}.jsonl")


def append_run_log(run_id: str, record: dict) -> None:
    """Append one JSONL line to the run log. Secrets are never logged —
    callers must redact before calling this."""
    path = run_log_path(run_id)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**record, "ts": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n")
