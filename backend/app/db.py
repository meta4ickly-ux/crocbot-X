"""Database: SQLAlchemy 2.0 models, engine, sessions, bot roster seed.

SQLite by default (local dev, file under CROCBOT_DATA_DIR / the
DATABASE_URL). Postgres on the VPS via
DATABASE_URL=postgresql+psycopg://... — same models, same schema, the
dialect is chosen by the URL. No auth tables in v1: single-user service
behind Caddy on the VPS; the deploy agent adds auth before exposing it.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    # Naive UTC: SQLite returns naive datetimes, so aware values would
    # fail comparisons on read-back. All timestamps in this service are UTC.
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Bot(Base):
    __tablename__ = "bots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="grok-4.3")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    tools: Mapped[list] = mapped_column(JSON, default=list)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # Schedule mode: "on_demand" or "daily_background+on_demand".
    schedule_mode: Mapped[str] = mapped_column(String(64), default="on_demand")
    schedule_cron: Mapped[str] = mapped_column(String(64), default="")
    # Behaviour flags. Time Auditor: all three False by founder decision.
    asks_permission: Mapped[bool] = mapped_column(Boolean, default=True)
    interrupts_user: Mapped[bool] = mapped_column(Boolean, default=False)
    messages_others: Mapped[bool] = mapped_column(Boolean, default=False)
    owner: Mapped[str] = mapped_column(String(128), default="")
    # Crash-loop guard (observability): paused by the backend when a
    # bot restarts too often; the founder unpauses it.
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str] = mapped_column(String(256), default="")


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    bot_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("bots.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="New conversation")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(String(64), ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(32), nullable=False)  # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Run(Base):
    """Durable agent run (Pi Durable pattern — see app/agent/durable.py)."""
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engine: Mapped[str] = mapped_column(String(32), default="muse")
    model: Mapped[str] = mapped_column(String(64), default="grok-4.7")
    status: Mapped[str] = mapped_column(String(32), default="running")
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    steps_total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RunStep(Base):
    """One checkpointed step of a run's journal. Completed replayable
    steps are re-used on resume, never re-executed."""
    __tablename__ = "run_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    replayable: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id"))
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UsageDay(Base):
    __tablename__ = "usage_days"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD UTC
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class ReviewAudit(Base):
    """Trust-layer audit log: every review request AND every decision.

    One row per event (requested / approved / denied / expired), with
    bot, action, target and reason. Nothing in this table is ever
    deleted by the backend.
    """
    __tablename__ = "review_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str] = mapped_column(String(512), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Handoff(Base):
    """Bot-to-bot handoff: one bot passes a task to another.

    Recorded in the DB and in the structured JSON log; nothing here
    moves money or sends anything outward — the receiving bot still
    goes through the normal loop, judge, and approval gate when it
    picks the task up.
    """
    __tablename__ = "handoffs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    from_bot_id: Mapped[str] = mapped_column(String(64), ForeignKey("bots.id"), nullable=False)
    to_bot_id: Mapped[str] = mapped_column(String(64), ForeignKey("bots.id"), nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending | completed | failed
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(Base):
    """Observability alert: crash-loop, disk warn/critical, budget."""
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(32), default="warn")
    message: Mapped[str] = mapped_column(Text, default="")
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


def make_engine(database_url: str | None = None):
    url = database_url or settings.database_url
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    if settings.database_url.startswith("sqlite"):
        path = settings.database_url.split("sqlite:///")[-1]
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Bot roster seed -------------------------------------------------------
# Names from the Grok Bot reference image the founder sent
# (workspace/user/files/1000005461_0_k5vt.png). "Hidden Bots 7" is a
# count on that image, not seven named bots — the seven hidden entries
# below are placeholders, hidden from the roster until configured.

SEED_BOTS: list[dict] = [
    {
        "id": "time-auditor",
        "name": "Time Auditor",
        "description": (
            "Watches how time is spent, flags time leaks, logs them. "
            "Runs daily in the background, quiet, log-only, plus on-demand "
            "when the founder asks. It does not ask anyone anything, does not "
            "message or interrupt anyone, and does not ask the founder for "
            "permission each run. The founder reviews the log when he wants."
        ),
        "model": "grok-4.3",
        "system_prompt": (
            "You are Time Auditor. Watch the founder's day, flag time leaks "
            "(scrolling, idle gaps, repeated context switches), log each one "
            "with a timestamp and a one-line note. Never message anyone, "
            "never interrupt, never ask for permission. Log only."
        ),
        "tools": ["read_file", "fetch_url", "web_search"],
        "approval_required": False,
        "hidden": False,
        "schedule_mode": "daily_background+on_demand",
        "schedule_cron": "0 9 * * *",
        "asks_permission": False,
        "interrupts_user": False,
        "messages_others": False,
    },
    {
        "id": "calorie-tracker",
        "name": "Calorie Tracker",
        "description": "Logs meals and estimates calories from a description or photo.",
        "model": "grok-4.3",
        "system_prompt": "You are Calorie Tracker. Estimate calories and macros from meal descriptions. Log only; never give medical advice beyond general information.",
        "tools": ["read_file", "web_search"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "technical-explainer",
        "name": "Technical Explainer",
        "description": "Explains technical topics in plain language.",
        "model": "grok-4.7",
        "system_prompt": "You are Technical Explainer. Explain technical topics plainly, with a short example. No jargon without a definition.",
        "tools": ["web_search", "fetch_url"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "david-todoist",
        "name": "David Todoist",
        "description": "Keeps David's task list: capture, prioritise, remind.",
        "model": "grok-4.3",
        "system_prompt": "You are David Todoist. Capture tasks, keep the list short, prioritise by due date. Never delete a task without confirmation.",
        "tools": ["read_file", "write_file"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "github-viewer",
        "name": "GitHub Viewer",
        "description": "Reads repos, issues and pull requests and summarises them.",
        "model": "grok-4.7",
        "system_prompt": "You are GitHub Viewer. Summarise repos, issues and PRs accurately; quote file paths and line numbers when you have them.",
        "tools": ["fetch_url", "web_search", "read_file"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "youtube-titles",
        "name": "YouTube Titles",
        "description": "Writes and scores YouTube titles for a video topic.",
        "model": "grok-4.3",
        "system_prompt": "You are YouTube Titles. Write 10 title options for a topic, score each for curiosity and clarity, pick a winner.",
        "tools": ["web_search"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "new-discord-manager",
        "name": "New Discord Manager",
        "description": "Helps set up and run a Discord server: channels, roles, rules.",
        "model": "grok-4.3",
        "system_prompt": "You are New Discord Manager. Help plan channels, roles and rules for a Discord server. Never take an action on a live server without approval.",
        "tools": ["read_file", "write_file"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "developer",
        "name": "Developer",
        "description": "Writes and fixes code, opens a branch, writes tests. Never merges or deploys without founder approval.",
        "model": "grok-build-0.1",
        "system_prompt": (
            "You are Developer. Write and fix code in the run's scratch "
            "workspace, write tests for what you change, and report what "
            "you did in one short summary. Never merge, never deploy, "
            "never delete files without founder approval."
        ),
        "tools": ["read_file", "write_file", "web_search"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "researcher",
        "name": "Researcher",
        "description": "Researches a topic on the web, returns a short brief with sources.",
        "model": "grok-4.7",
        "system_prompt": (
            "You are Researcher. Research the topic, check the sources, "
            "and return a short brief: what is true, what is unverified, "
            "and the source links. Never present an unverified claim as fact."
        ),
        "tools": ["web_search", "fetch_url", "read_file"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "inbox",
        "name": "Inbox",
        "description": "Triages incoming messages, drafts replies. Never sends anything without founder approval.",
        "model": "grok-4.3",
        "system_prompt": (
            "You are Inbox. Triage incoming messages: urgent, later, "
            "ignore. Draft replies in the founder's voice, short and "
            "plain. Never send anything — drafts only, the founder sends."
        ),
        "tools": ["read_file"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
    {
        "id": "llamaxia-judge",
        "name": "llamaXia Judge",
        "description": (
            "The llamaXia comment judge: Watch → Judge → Pay-guard → Log. "
            "Pay is disabled — $0 moves, always."
        ),
        "model": "grok-4.3",
        "system_prompt": (
            "You are the llamaXia Judge. Watch each comment, judge it "
            "(helpful / pass / spam), then the pay-guard: pay is disabled, "
            "so no tip is ever sent. Log the verdict and rest. Creators "
            "are paid first — by the platform side, never by keeping tips."
        ),
        "tools": ["read_file", "web_search"],
        "approval_required": True,
        "schedule_mode": "on_demand",
    },
]

for _i in range(1, 8):
    SEED_BOTS.append(
        {
            "id": f"hidden-bot-{_i}",
            "name": f"Hidden Bot {_i}",
            "description": "Hidden bot from the Grok Bot reference roster — not yet configured.",
            "model": "grok-4.3",
            "system_prompt": "",
            "tools": [],
            "approval_required": True,
            "hidden": True,
            "schedule_mode": "on_demand",
        }
    )


def seed_bots(db: Session) -> int:
    """Insert roster bots missing from the DB. Returns count inserted."""
    inserted = 0
    for data in SEED_BOTS:
        existing = db.get(Bot, data["id"])
        if existing is None:
            db.add(Bot(**{k: v for k, v in data.items() if k in Bot.__table__.columns}))
            inserted += 1
    db.commit()
    return inserted


def get_bot(db: Session, bot_id: str) -> Bot | None:
    return db.get(Bot, bot_id)


def list_bots(db: Session, include_hidden: bool = False) -> list[Bot]:
    stmt = select(Bot).order_by(Bot.name)
    if not include_hidden:
        stmt = stmt.where(Bot.hidden.is_(False))
    return list(db.scalars(stmt))
