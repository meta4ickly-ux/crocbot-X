"""Pytest setup: temp SQLite DB + temp data dir, set BEFORE app import."""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="crocbot-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["CROCBOT_DATA_DIR"] = _TMP
os.environ["CROCBOT_WORKDIR"] = _TMP
os.environ["XAI_API_KEY"] = ""
os.environ["CROCBOT_ENGINE"] = "muse"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
