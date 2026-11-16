"""Agent loop: watch -> judge -> log -> rest.

One turn of the loop, with a durable journal (see durable.py) and the
PayGuard always on. The judge is deterministic in v1: it classifies
the incoming text (empty / spam / normal) and only normal text reaches
an engine. A future step can route the judge through an engine; the
loop shape does not change.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import tempfile
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from ..config import settings
from ..db import Approval, Bot, Conversation, Message, _utcnow
from ..observability import check_crash_loop, set_log_context
from ..security import redact
from . import durable, guardrails
from .engines import EngineAdapter, get_engine
from .payguard import assert_pay_disabled
from .tools import TOOLS, execute_tool

SPAM_MARKERS = ("free money", "click here to win", "double your crypto", "send 1 get 2")


def judge_text(text: str) -> tuple[str, float, str]:
    """Classify incoming text. Returns (verdict, confidence, reason).

    Verdicts: APPROVE (normal — send to the engine), PASS (empty or
    low-signal — answer with a short canned reply, no engine call),
    SPAM (refuse and log).
    """
    stripped = (text or "").strip()
    if not stripped:
        return "PASS", 1.0, "empty message"
    lowered = stripped.lower()
    if any(marker in lowered for marker in SPAM_MARKERS):
        return "SPAM", 0.95, "matches a spam marker"
    if len(stripped) < 3:
        return "PASS", 0.8, "too short to act on"
    return "APPROVE", 0.9, "normal message"


def _event(type_: str, **fields) -> dict:
    event = {"type": type_}
    event.update(fields)
    return event


async def _run_turn_inner(
    db: Session,
    *,
    conversation: Conversation,
    bot_id: str | None,
    bot_name: str,
    system_prompt: str,
    user_text: str,
    engine_name: str | None = None,
    model: str = "grok-4.7",
    user_id: str = "founder",
    workdir: str | None = None,
) -> AsyncIterator[dict]:
    """One watch->judge->log->rest turn (see run_turn for the wrapper)."""
    assert_pay_disabled()  # hard guard, every turn
    workdir = workdir or settings.workdir
    # Crash-loop guard: a paused bot does not run. A bot that failed
    # too often is paused here and an alert is raised.
    if bot_id:
        bot = db.get(Bot, bot_id)
        if bot is not None and bot.paused:
            yield _event("error", detail=f"bot {bot_id!r} is paused: {bot.paused_reason}")
            return
        check_crash_loop(db, bot_id)
        bot = db.get(Bot, bot_id)
        if bot is not None and bot.paused:
            yield _event("error", detail=f"bot {bot_id!r} was paused: {bot.paused_reason}")
            return
    engine: EngineAdapter = get_engine(engine_name)
    run = durable.create_run(
        db, conversation_id=conversation.id, bot_id=bot_id,
        engine=engine.name, model=model,
    )
    set_log_context(bot_id=bot_id or "", run_id=run.id)
    seq = 0

    # --- watch: the input arrived -------------------------------------
    seq += 1
    durable.checkpoint(db, run, seq, "watch",
                       {"conversation_id": conversation.id, "text": redact(user_text)})
    durable.append_run_log(run.id, {"step": "watch", "run_id": run.id})

    # --- judge ---------------------------------------------------------
    verdict, confidence, reason = judge_text(user_text)
    seq += 1
    durable.checkpoint(db, run, seq, "judge",
                       {"verdict": verdict, "confidence": confidence, "reason": reason})
    durable.append_run_log(run.id, {"step": "judge", "verdict": verdict})

    if verdict == "SPAM":
        text = "That message looks like spam, so I logged it and did nothing with it."
        _save_message(db, conversation.id, "assistant", text)
        seq += 1
        durable.checkpoint(db, run, seq, "log", {"text": text})
        durable.finish_run(db, run, status="completed", stop_reason="completed")
        yield _event("token", text=text)
        yield _event("done", text=text, run_id=run.id,
                     usage={"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
                     engine=engine.name, model=model)
        return
    if verdict == "PASS":
        text = "Noted — nothing to act on there."
        _save_message(db, conversation.id, "assistant", text)
        seq += 1
        durable.checkpoint(db, run, seq, "log", {"text": text})
        durable.finish_run(db, run, status="completed", stop_reason="completed")
        yield _event("token", text=text)
        yield _event("done", text=text, run_id=run.id,
                     usage={"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
                     engine=engine.name, model=model)
        return

    # --- engine: stream the answer ------------------------------------
    history = [
        {"role": m.role, "content": m.content}
        for m in db.query(Message).filter(Message.conversation_id == conversation.id)
        .order_by(Message.created_at).all()[-20:]
        if m.role in ("user", "assistant")
    ]
    text_parts: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    steps_used = 0
    async for event in engine.stream(
        user_text, system=system_prompt, history=history, model=model,
        bot_name=bot_name, workdir=workdir,
        timeout_s=settings.engine_timeout_s, max_steps=settings.max_steps,
    ):
        if event["type"] == "token":
            text_parts.append(event.get("text", ""))
            yield event
        elif event["type"] == "tool_call":
            steps_used += 1
            if steps_used > settings.max_steps:
                yield _event("error", detail=f"turn limit hit ({settings.max_steps} steps)")
                durable.finish_run(db, run, status="failed", stop_reason="turn_limit")
                return
            tool_name = str(event.get("tool", ""))
            tool = TOOLS.get(tool_name)
            if tool is None:
                yield _event("tool_result", tool=tool_name, result={"error": f"unknown tool {tool_name!r}"})
                continue
            if tool.side_effect == "write" and tool.requires_approval:
                # Trust layer: pause for founder review BEFORE the action
                # runs. No auto-approve, no bot self-approval.
                approval = guardrails.request_review(
                    db, run_id=run.id, bot_id=bot_id, action=tool_name,
                    args=event.get("args", {}) or {},
                    reason="write tool requires founder review",
                )
                seq += 1
                durable.checkpoint(db, run, seq, "approval_required",
                                   {"approval_id": approval.id, "tool": tool_name},
                                   replayable=False)
                durable.finish_run(db, run, status="awaiting_approval", stop_reason="awaiting_approval")
                yield _event("approval_required", approval_id=approval.id,
                             tool=tool_name, args=approval.args, run_id=run.id)
                yield _event("done", text="".join(text_parts), run_id=run.id,
                             status="awaiting_approval", usage=usage,
                             engine=engine.name, model=model)
                return
            try:
                result = execute_tool(
                    tool_name, event.get("args", {}) or {},
                    workdir=workdir, allowlist=settings.fetch_allowlist,
                    allow_shell=settings.allow_shell, approved=True,
                )
            except Exception as exc:  # tool failure is a result, not a crash
                result = {"error": redact(str(exc))}
            seq += 1
            durable.checkpoint(db, run, seq, "tool_result",
                               {"tool": tool_name, "result": result})
            yield _event("tool_result", tool=tool_name, result=result)
        elif event["type"] == "citation":
            yield event
        elif event["type"] == "done":
            usage = event.get("usage", usage)
            text_parts = [event.get("text", "")] or text_parts
        elif event["type"] == "error":
            durable.finish_run(db, run, status="failed", stop_reason="error")
            yield event
            return

    # --- log: persist the answer ---------------------------------------
    text = "".join(text_parts)
    assistant = _save_message(db, conversation.id, "assistant", text,
                              tokens=usage.get("completion_tokens", 0),
                              cost_usd=usage.get("cost_usd", 0.0))
    _record_usage(db, user_id, usage)
    seq += 1
    durable.checkpoint(db, run, seq, "log",
                       {"message_id": assistant.id, "usage": usage})
    durable.append_run_log(run.id, {"step": "log", "usage": usage, "cost_usd": usage.get("cost_usd", 0.0)})
    durable.finish_run(db, run, status="completed", stop_reason="completed")
    yield _event("done", text=text, run_id=run.id, usage=usage,
                 engine=engine.name, model=model)

    # --- rest: nothing left to do this turn ----------------------------


def make_scratch_workdir(run_id: str) -> str:
    """Fresh ephemeral workspace for one run (clean-state principle).

    Every run starts from a clean scratch state: an empty directory
    under data/work/<run_id>/. It is deleted when the run ends, so no
    leftover state leaks into the next run.
    """
    base = os.path.join(settings.data_dir, "work")
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"{run_id}-", dir=base)


async def run_turn(
    db: Session,
    *,
    conversation: Conversation,
    bot_id: str | None,
    bot_name: str,
    system_prompt: str,
    user_text: str,
    engine_name: str | None = None,
    model: str = "grok-4.7",
    user_id: str = "founder",
) -> AsyncIterator[dict]:
    """Run one watch->judge->log->rest turn, yielding SSE events.

    The caller (chat router) owns the DB session and the HTTP stream;
    this generator checkpoints every step into the run journal. Each
    run gets a fresh ephemeral scratch workspace (clean state) that is
    deleted when the run ends.
    """
    scratch = make_scratch_workdir(os.urandom(8).hex())
    try:
        async for event in _run_turn_inner(
            db, conversation=conversation, bot_id=bot_id, bot_name=bot_name,
            system_prompt=system_prompt, user_text=user_text,
            engine_name=engine_name, model=model, user_id=user_id,
            workdir=scratch,
        ):
            yield event
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        set_log_context(bot_id="", run_id="")


def _save_message(db: Session, conversation_id: str, role: str, content: str,
                  *, tokens: int = 0, cost_usd: float = 0.0) -> Message:
    message = Message(conversation_id=conversation_id, role=role, content=content,
                      tokens=tokens, cost_usd=cost_usd)
    db.add(message)
    conversation = db.get(Conversation, conversation_id)
    if conversation is not None:
        conversation.updated_at = _utcnow()
    db.commit()
    db.refresh(message)
    return message


def _record_usage(db: Session, user_id: str, usage: dict) -> None:
    from ..db import UsageDay

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    row = db.get(UsageDay, (user_id, today))
    if row is None:
        row = UsageDay(user_id=user_id, day=today)
        db.add(row)
    row.tokens = (row.tokens or 0) + int(usage.get("prompt_tokens", 0) or 0) + int(usage.get("completion_tokens", 0) or 0)
    row.cost_usd = (row.cost_usd or 0.0) + float(usage.get("cost_usd", 0.0) or 0.0)
    db.commit()


def daily_budget_remaining(db: Session, user_id: str, budget_usd: float) -> float:
    """Dollars left today for this user (hard stop at $0)."""
    from ..db import UsageDay

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    row = db.get(UsageDay, (user_id, today))
    spent = row.cost_usd if row else 0.0
    return max(0.0, budget_usd - spent)
