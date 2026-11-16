"""Settings for the Crocbot X backend.

Secrets come only from the server-side environment / .env file (never
committed, never logged, never sent to the browser). ``.env`` is parsed
here with the stdlib only, so no extra dependency is needed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


_load_dotenv()


def _csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("CROCBOT_ENV", "dev"))
    host: str = field(default_factory=lambda: os.environ.get("CROCBOT_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("CROCBOT_PORT", "8000")))

    # Model provider (xAI Responses API). Empty key = stub engine, $0.
    xai_api_key: str = field(default_factory=lambda: os.environ.get("XAI_API_KEY", ""))
    xai_base_url: str = field(
        default_factory=lambda: os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
    )
    xai_model: str = field(default_factory=lambda: os.environ.get("XAI_MODEL", "grok-4.7"))
    xai_cheap_model: str = field(
        default_factory=lambda: os.environ.get("XAI_CHEAP_MODEL", "grok-4.3")
    )
    xai_code_model: str = field(
        default_factory=lambda: os.environ.get("XAI_CODE_MODEL", "grok-build-0.1")
    )

    # Database: SQLite file by default (local dev). Postgres on the VPS
    # via DATABASE_URL=postgresql+psycopg://user:pass@postgres:5432/crocbot
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "DATABASE_URL", "sqlite:///./data/crocbot.db"
        )
    )
    redis_url: str = field(
        default_factory=lambda: os.environ.get("REDIS_URL", "")
    )  # optional, VPS only

    # Engine selection: muse (default, in-process), codex, pi, stub, xai.
    default_engine: str = field(
        default_factory=lambda: os.environ.get("CROCBOT_ENGINE", "muse")
    )
    codex_bin: str = field(default_factory=lambda: os.environ.get("CODEX_BIN", "codex"))
    pi_bin: str = field(default_factory=lambda: os.environ.get("PI_BIN", "pi"))
    pi_session_dir: str = field(
        default_factory=lambda: os.environ.get(
            "PI_SESSION_DIR", os.path.expanduser("~/.pi/agent/sessions")
        )
    )
    engine_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("CROCBOT_ENGINE_TIMEOUT_S", "300"))
    )
    max_steps: int = field(
        default_factory=lambda: int(os.environ.get("CROCBOT_MAX_STEPS", "10"))
    )

    # Budgets / limits. $5/user/day default, hard stop (research report §5).
    daily_budget_usd: float = field(
        default_factory=lambda: float(os.environ.get("DAILY_USER_BUDGET_USD", "5"))
    )
    rate_limit_per_min: int = field(
        default_factory=lambda: int(os.environ.get("CROCBOT_RATE_LIMIT_PER_MIN", "60"))
    )
    approval_timeout_s: int = field(
        default_factory=lambda: int(os.environ.get("CROCBOT_APPROVAL_TIMEOUT_S", "900"))
    )

    # Fetch tool: default-deny. Only these hostnames may be fetched.
    fetch_allowlist: list[str] = field(
        default_factory=lambda: _csv("CROCBOT_FETCH_ALLOWLIST", "")
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: _csv(
            "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173"
        )
    )
    # Working directory the CLI engines and file tools may touch.
    workdir: str = field(
        default_factory=lambda: os.environ.get("CROCBOT_WORKDIR", ".")
    )
    data_dir: str = field(
        default_factory=lambda: os.environ.get("CROCBOT_DATA_DIR", "./data")
    )

    # Shell tool is off unless the deployer explicitly enables it.
    allow_shell: bool = field(
        default_factory=lambda: os.environ.get("CROCBOT_ALLOW_SHELL", "false").lower()
        == "true"
    )


settings = Settings()
