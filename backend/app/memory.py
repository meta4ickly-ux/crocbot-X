"""Memory, both ways.

1. Postgres/SQLite (the DB) is the searchable store — conversations,
   runs, approvals. That stays.
2. Plain files, one per bot under ``data/memory/<bot-id>.md``, are the
   portable store — SuperBot's pattern: rules and preferences as plain
   text a human can read, copy, and move. One line per note, newest at
   the bottom, timestamped UTC.

Files live under CROCBOT_DATA_DIR (``data/``), which is gitignored —
runtime memory is never committed. The code that writes them is.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .config import settings

_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
NOTE_KINDS = ("rule", "preference", "note", "turn")


def memory_dir() -> Path:
    path = Path(settings.data_dir) / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def memory_path(bot_id: str) -> Path:
    if not _BOT_ID_RE.match(bot_id or ""):
        raise ValueError(f"invalid bot id for a memory file: {bot_id!r}")
    return memory_dir() / f"{bot_id}.md"


def read_memory(bot_id: str) -> str:
    path = memory_path(bot_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def append_memory_note(bot_id: str, note: str, kind: str = "note") -> str:
    """Append one timestamped line. Returns the full file content."""
    if kind not in NOTE_KINDS:
        raise ValueError(f"unknown memory kind {kind!r}")
    note = (note or "").strip()
    if not note:
        raise ValueError("memory note must not be empty")
    if "\n" in note:
        raise ValueError("memory note must be a single line")
    path = memory_path(bot_id)
    if not path.exists():
        path.write_text(
            f"# Memory — {bot_id}\n\n"
            "Plain-file memory for this bot. One line per note, newest at\n"
            "the bottom. The database is the searchable store; this file\n"
            "is the portable one — read it, copy it, move it anywhere.\n\n",
            encoding="utf-8",
        )
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"- [{stamp}] ({kind}) {note}\n")
    return path.read_text(encoding="utf-8")


def export_memory(bot_id: str, profile: dict | None = None) -> str:
    """(Re)write the bot's plain-file memory from its profile + notes.

    If the file already has notes, they are kept and the profile header
    is refreshed above them. If there is no file yet, one is created
    from the profile alone.
    """
    path = memory_path(bot_id)
    existing_notes = ""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        notes = [ln for ln in lines if ln.startswith("- [")]
        existing_notes = "\n".join(notes)
    header = f"# Memory — {bot_id}\n\n"
    if profile:
        header += (
            f"Bot: {profile.get('name', bot_id)}\n"
            f"Model: {profile.get('model', '')}\n"
            f"About: {profile.get('description', '')}\n\n"
        )
    header += (
        "Plain-file memory for this bot. One line per note, newest at\n"
        "the bottom. The database is the searchable store; this file\n"
        "is the portable one — read it, copy it, move it anywhere.\n\n"
    )
    content = header + (existing_notes + "\n" if existing_notes else "")
    path.write_text(content, encoding="utf-8")
    return content


def record_turn(bot_id: str | None, verdict: str, cost_usd: float = 0.0) -> None:
    """One memory line per completed turn. Never breaks a turn."""
    if not bot_id:
        return
    try:
        append_memory_note(bot_id, f"turn: verdict={verdict} cost=${cost_usd:.2f}", kind="turn")
    except Exception:
        # Memory is a log, not a gate — a bad write must not fail a turn.
        pass
