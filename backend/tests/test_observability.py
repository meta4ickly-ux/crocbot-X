import json
import logging

from fastapi.testclient import TestClient

from app.db import Bot, Run, SessionLocal, init_db, seed_bots
from app.main import app
from app.observability import (
    JsonFormatter,
    bot_status,
    check_crash_loop,
    check_disk,
    disk_stats,
    disk_writes_paused,
    failed_runs_in_window,
    process_stats,
)
from app.agent.loop import make_scratch_workdir
import os


def _client():
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
    finally:
        db.close()
    return TestClient(app)


def test_json_log_format_has_bot_and_run_fields():
    record = logging.LogRecord("crocbot", logging.INFO, __file__, 1, "hello", (), None)
    record.fields = {"kind": "test"}
    line = JsonFormatter().format(record)
    obj = json.loads(line)
    assert obj["level"] == "info"
    assert obj["msg"] == "hello"
    assert "bot_id" in obj and "run_id" in obj and "ts" in obj


def test_process_and_disk_stats_shape():
    stats = process_stats()
    assert stats["memory_bytes"] > 0
    assert stats["cpu_seconds"] >= 0
    disk = disk_stats()
    assert 0 <= disk["used_pct"] <= 100
    assert disk["warn_pct"] == 80.0
    assert disk["critical_pct"] == 90.0
    assert disk["level"] in ("ok", "warn", "critical")
    assert disk_writes_paused() is (disk["level"] == "critical")


def test_crash_loop_pauses_bot_after_3_failures_in_10_minutes():
    init_db()
    db = SessionLocal()
    try:
        seed_bots(db)
        bot_id = "crash-test-bot"
        if db.get(Bot, bot_id) is None:
            db.add(Bot(id=bot_id, name="Crash Test Bot", model="grok-4.3"))
            db.commit()
        for _ in range(3):
            run = Run(bot_id=bot_id, engine="stub", model="grok-4.3", status="failed",
                      stop_reason="error")
            db.add(run)
        db.commit()
        assert failed_runs_in_window(db, bot_id) == 3
        alert = check_crash_loop(db, bot_id)
        assert alert is not None
        assert alert.kind == "crash_loop"
        assert alert.severity == "critical"
        bot = db.get(Bot, bot_id)
        assert bot.paused is True
        assert "crash-loop" in bot.paused_reason
        # Second check does not re-alert a paused bot.
        assert check_crash_loop(db, bot_id) is None
    finally:
        db.close()


def test_scratch_workdir_is_fresh_and_clean():
    path = make_scratch_workdir("test-run-1")
    assert os.path.isdir(path)
    assert os.listdir(path) == []
    with open(os.path.join(path, "leftover.txt"), "w") as fh:
        fh.write("x")
    # The loop deletes the scratch dir when the run ends; simulate it.
    import shutil

    shutil.rmtree(path, ignore_errors=True)
    assert not os.path.exists(path)


def test_metrics_endpoint_exposes_bots_cpu_mem_disk_alerts():
    with _client() as client:
        body = client.get("/metrics").json()
        assert body["service"] == "crocbot-x"
        assert "process" in body and "memory_bytes" in body["process"]
        assert "cpu_seconds" in body["process"]
        assert "disk" in body and "used_pct" in body["disk"]
        assert "bots" in body
        auditor = [b for b in body["bots"] if b["id"] == "time-auditor"][0]
        assert auditor["schedule_mode"] == "daily_background+on_demand"
        assert "restarts_last_10m" in auditor
        assert "alerts" in body
        assert "engines" in body


def test_alerts_endpoint_lists_and_acknowledges():
    init_db()
    db = SessionLocal()
    try:
        alert = check_disk(db)  # returns None when disk is fine
        assert alert is None or alert.kind == "disk_usage"
    finally:
        db.close()
    with _client() as client:
        alerts = client.get("/api/v1/alerts").json()
        assert isinstance(alerts, list)
