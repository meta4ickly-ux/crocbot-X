"""Bots roster router: list / get / create / update / delete.

Hidden bots are excluded from the list by default (?include_hidden=true
shows them). Time Auditor is the first bot; its mode is set in the
seed (see app/db.py SEED_BOTS): daily background, quiet, log-only,
plus on-demand. It never asks permission, never interrupts, never
messages anyone.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import Bot, get_db, list_bots

router = APIRouter(prefix="/api/v1/bots", tags=["bots"])


class BotCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    model: str = "grok-4.3"
    system_prompt: str = ""
    tools: list[str] = []
    approval_required: bool = True
    hidden: bool = False
    schedule_mode: str = "on_demand"
    schedule_cron: str = ""
    asks_permission: bool = True
    interrupts_user: bool = False
    messages_others: bool = False
    owner: str = ""


class BotUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    tools: list[str] | None = None
    approval_required: bool | None = None
    hidden: bool | None = None
    schedule_mode: str | None = None
    schedule_cron: str | None = None
    asks_permission: bool | None = None
    interrupts_user: bool | None = None
    messages_others: bool | None = None
    owner: str | None = None


def _out(bot: Bot) -> dict:
    return {
        "id": bot.id, "name": bot.name, "description": bot.description,
        "model": bot.model, "system_prompt": bot.system_prompt,
        "tools": bot.tools or [], "approval_required": bot.approval_required,
        "hidden": bot.hidden, "schedule_mode": bot.schedule_mode,
        "schedule_cron": bot.schedule_cron, "asks_permission": bot.asks_permission,
        "interrupts_user": bot.interrupts_user, "messages_others": bot.messages_others,
        "owner": bot.owner,
    }


@router.get("")
def get_bots(include_hidden: bool = False, db: Session = Depends(get_db)):
    return [_out(b) for b in list_bots(db, include_hidden=include_hidden)]


@router.get("/{bot_id}")
def get_bot(bot_id: str, db: Session = Depends(get_db)):
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    return _out(bot)


@router.post("")
def create_bot(body: BotCreate, db: Session = Depends(get_db)):
    if db.get(Bot, body.id) is not None:
        raise HTTPException(status_code=409, detail=f"bot {body.id!r} already exists")
    from ..models import validate_model

    try:
        validate_model(body.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bot = Bot(**body.model_dump())
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return _out(bot)


@router.put("/{bot_id}")
def update_bot(bot_id: str, body: BotUpdate, db: Session = Depends(get_db)):
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "model" and value is not None:
            from ..models import validate_model

            try:
                validate_model(value)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        setattr(bot, key, value)
    db.commit()
    db.refresh(bot)
    return _out(bot)


@router.delete("/{bot_id}")
def delete_bot(bot_id: str, db: Session = Depends(get_db)):
    bot = db.get(Bot, bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
    db.delete(bot)
    db.commit()
    return {"deleted": bot_id}
