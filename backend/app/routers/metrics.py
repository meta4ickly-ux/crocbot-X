"""Metrics + alerts router.

GET /metrics — per-bot status, process CPU/memory, restart counts,
disk usage + thresholds, last alerts, engine availability.
GET /api/v1/alerts — unacknowledged alerts first.
POST /api/v1/alerts/{id}/acknowledge — founder acknowledges an alert.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import Alert, get_db, list_bots
from ..observability import bot_status, disk_stats, process_stats
from ..agent.engines import engine_status

router = APIRouter(tags=["observability"])
_STARTED = time.monotonic()


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)):
    bots = list_bots(db, include_hidden=True)
    alerts = (
        db.query(Alert).filter(Alert.acknowledged.is_(False))
        .order_by(Alert.created_at.desc()).limit(20).all()
    )
    return {
        "service": "crocbot-x",
        "uptime_seconds": round(time.monotonic() - _STARTED, 1),
        "process": process_stats(),
        "disk": disk_stats(),
        "bots": [bot_status(db, b) for b in bots],
        "engines": engine_status(),
        "alerts": [
            {"id": a.id, "kind": a.kind, "bot_id": a.bot_id, "severity": a.severity,
             "message": a.message,
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in alerts
        ],
    }


@router.get("/api/v1/alerts")
def list_alerts(acknowledged: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(Alert).order_by(Alert.created_at.desc())
    if acknowledged is not None:
        query = query.filter(Alert.acknowledged.is_(acknowledged))
    return [
        {"id": a.id, "kind": a.kind, "bot_id": a.bot_id, "severity": a.severity,
         "message": a.message, "acknowledged": a.acknowledged,
         "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in query.all()
    ]


@router.post("/api/v1/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="alert not found")
    alert.acknowledged = True
    db.commit()
    return {"id": alert.id, "acknowledged": True}
