"""Observability: one central structured JSON log stream (journald),
per-bot / per-run fields, container stats, crash-loop + disk alerts.

Logging: the service writes structured JSON lines to stdout. Under
systemd the unit sets StandardOutput=journal / StandardError=journal,
so journald IS the central store — there are no per-bot log files.
Per-run detail still lands in data/runs/<run_id>.jsonl (one file per
run, not per bot), and the journal carries the same events.

Stats: CPU/memory come from the container cgroup (v2: memory.current,
cpu.stat) when present, falling back to /proc/self. Disk comes from
shutil.disk_usage on the data dir.

Alert thresholds (founder-set, see docs/BACKEND.md):
- Crash-loop: 3 restarts / failed runs in 10 minutes -> stop the bot
  (paused) and alert. Never restart forever.
- Disk: 80% warn, 90% critical -> alert and pause non-essential writes.
"""
from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import resource
import shutil
import sys
import time

from sqlalchemy.orm import Session

from .config import settings
from .db import Alert, Bot, Run, _utcnow

CRASH_LOOP_RESTARTS = 3
CRASH_LOOP_WINDOW_MINUTES = 10
DISK_WARN_PCT = 80.0
DISK_CRITICAL_PCT = 90.0

_bot_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("crocbot_bot_id", default="")
_run_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("crocbot_run_id", default="")


def set_log_context(*, bot_id: str = "", run_id: str = "") -> None:
    _bot_id_ctx.set(bot_id)
    _run_id_ctx.set(run_id)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: level, message, bot_id, run_id, fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "bot_id": _bot_id_ctx.get(),
            "run_id": _run_id_ctx.get(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload)


def get_logger(name: str = "crocbot") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: str, msg: str, **fields) -> None:
    """Log one structured event with per-bot / per-run fields."""
    getattr(logger, level)(msg, extra={"fields": fields})


# --- stats ---------------------------------------------------------------

def _read_cgroup_file(name: str) -> str | None:
    for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/system.slice"):
        path = os.path.join(base, name)
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            continue
    return None


def process_stats() -> dict:
    """CPU seconds + RSS for this process, from the cgroup if present."""
    mem_bytes: int | None = None
    cpu_seconds: float | None = None
    current = _read_cgroup_file("memory.current")
    if current is not None:
        try:
            mem_bytes = int(current)
        except ValueError:
            mem_bytes = None
    cpu_stat = _read_cgroup_file("cpu.stat")
    if cpu_stat:
        for line in cpu_stat.splitlines():
            if line.startswith("usage_usec"):
                try:
                    cpu_seconds = int(line.split()[1]) / 1_000_000
                except (IndexError, ValueError):
                    pass
    if mem_bytes is None:
        mem_bytes = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if cpu_seconds is None:
        cpu_seconds = resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_stime
    return {"cpu_seconds": round(cpu_seconds, 2), "memory_bytes": mem_bytes,
            "memory_mb": round(mem_bytes / 1024 / 1024, 1)}


def disk_stats(path: str | None = None) -> dict:
    """Disk usage for the data dir, with warn/critical thresholds."""
    target = path or settings.data_dir
    usage = shutil.disk_usage(target)
    pct = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
    if pct >= DISK_CRITICAL_PCT:
        level = "critical"
    elif pct >= DISK_WARN_PCT:
        level = "warn"
    else:
        level = "ok"
    return {"path": target, "used_pct": pct, "used_gb": round(usage.used / 1e9, 2),
            "total_gb": round(usage.total / 1e9, 2), "level": level,
            "warn_pct": DISK_WARN_PCT, "critical_pct": DISK_CRITICAL_PCT}


def disk_writes_paused(path: str | None = None) -> bool:
    """True when disk is critical (>=90%): non-essential writes pause."""
    return disk_stats(path)["level"] == "critical"


# --- alerts + crash-loop ---------------------------------------------------

def create_alert(db: Session, *, kind: str, bot_id: str | None, severity: str,
                 message: str) -> Alert:
    alert = Alert(kind=kind, bot_id=bot_id, severity=severity, message=message)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    get_logger().warning("alert created", extra={"fields": {
        "kind": kind, "bot_id": bot_id or "", "severity": severity, "alert_id": alert.id}})
    return alert


def failed_runs_in_window(db: Session, bot_id: str, *, minutes: int = CRASH_LOOP_WINDOW_MINUTES) -> int:
    """Failed runs + restarts for this bot in the last N minutes."""
    since = _utcnow() - dt.timedelta(minutes=minutes)
    runs = (
        db.query(Run)
        .filter(Run.bot_id == bot_id, Run.updated_at >= since)
        .all()
    )
    return sum(1 for r in runs if r.status == "failed" or (r.restart_count or 0) > 0)


def check_crash_loop(db: Session, bot_id: str) -> Alert | None:
    """Crash-loop guard: >=3 restarts/failed runs in 10 minutes -> stop
    the bot (paused) and alert. Returns the alert, or None."""
    bot = db.get(Bot, bot_id)
    if bot is None or bot.paused:
        return None
    count = failed_runs_in_window(db, bot_id)
    if count < CRASH_LOOP_RESTARTS:
        return None
    bot.paused = True
    bot.paused_reason = (
        f"crash-loop: {count} restarts/failed runs in {CRASH_LOOP_WINDOW_MINUTES} minutes"
    )
    db.commit()
    return create_alert(
        db, kind="crash_loop", bot_id=bot_id, severity="critical",
        message=f"Bot {bot_id} stopped: {count} restarts/failed runs in "
                f"{CRASH_LOOP_WINDOW_MINUTES} minutes. Unpause it manually when fixed.",
    )


def check_disk(db: Session) -> Alert | None:
    """Disk guard: warn at 80%, critical at 90% (alert + pause writes)."""
    stats = disk_stats()
    if stats["level"] == "ok":
        return None
    severity = "critical" if stats["level"] == "critical" else "warn"
    return create_alert(
        db, kind="disk_usage", bot_id=None, severity=severity,
        message=f"Disk {stats['path']} at {stats['used_pct']}% "
                f"({stats['used_gb']} / {stats['total_gb']} GB). "
                + ("Non-essential writes are paused." if severity == "critical" else ""),
    )


def bot_status(db: Session, bot: Bot) -> dict:
    """Per-bot status for /metrics."""
    runs = db.query(Run).filter(Run.bot_id == bot.id).order_by(Run.created_at.desc()).all()
    last = runs[0] if runs else None
    return {
        "id": bot.id,
        "name": bot.name,
        "paused": bot.paused,
        "paused_reason": bot.paused_reason,
        "schedule_mode": bot.schedule_mode,
        "runs_total": len(runs),
        "restarts_last_10m": failed_runs_in_window(db, bot.id),
        "last_run_status": last.status if last else None,
        "last_run_stop_reason": last.stop_reason if last else None,
    }
