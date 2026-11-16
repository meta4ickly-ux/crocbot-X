"""Tool registry.

Every tool declares a side-effect class. Read tools run automatically.
Write tools pause for an in-chat approval (see routers/approvals.py)
and fail closed on timeout. The ``pay`` tool exists only so the
PayGuard can refuse it in one place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..security import check_fetch_url
from .payguard import attempt_pay

# Imported lazily in execute_tool to keep httpx optional at import time.


@dataclass
class ToolDef:
    name: str
    description: str
    side_effect: str  # "read" | "write"
    requires_approval: bool
    schema: dict = field(default_factory=dict)


TOOLS: dict[str, ToolDef] = {
    "web_search": ToolDef(
        "web_search",
        "Search the web (stub in v1: returns a labelled stub result, no network).",
        "read", False,
        {"query": "string"},
    ),
    "fetch_url": ToolDef(
        "fetch_url",
        "Fetch a URL from the allow-list only (SSRF-checked).",
        "read", False,
        {"url": "string"},
    ),
    "read_file": ToolDef(
        "read_file",
        "Read a file under the working directory.",
        "read", False,
        {"path": "string"},
    ),
    "write_file": ToolDef(
        "write_file",
        "Write a file under the working directory.",
        "write", True,
        {"path": "string", "content": "string"},
    ),
    "delete_file": ToolDef(
        "delete_file",
        "Delete a file under the working directory. Destructive — always needs founder review.",
        "write", True,
        {"path": "string"},
    ),
    "run_shell": ToolDef(
        "run_shell",
        "Run a shell command. Off unless CROCBOT_ALLOW_SHELL=true, and still needs approval.",
        "write", True,
        {"command": "string"},
    ),
    "send_email": ToolDef(
        "send_email",
        "Send an email. Not configured in v1 — always returns not-configured.",
        "write", True,
        {"to": "string", "subject": "string", "body": "string"},
    ),
    "post_message": ToolDef(
        "post_message",
        "Post a message to a channel. Not configured in v1 — always returns not-configured.",
        "write", True,
        {"channel": "string", "text": "string"},
    ),
    "pay": ToolDef(
        "pay",
        "Move money. ALWAYS refused by the PayGuard ($0 moved).",
        "write", True,
        {"amount_usd": "number", "recipient": "string"},
    ),
}


def _safe_path(workdir: str, path: str) -> str:
    base = os.path.realpath(workdir)
    target = os.path.realpath(os.path.join(base, path))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"path {path!r} escapes the working directory")
    return target


def execute_tool(name: str, args: dict, *, workdir: str, allowlist: list[str],
                 allow_shell: bool, approved: bool = False) -> dict:
    """Execute a tool. Write tools require ``approved=True``.

    Returns a result dict. Raises ValueError / PayBlockedError on refusal.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"unknown tool {name!r}")
    if name == "pay":
        # The guard is the only path for money, and it always refuses.
        attempt_pay(float(args.get("amount_usd", 0) or 0), str(args.get("recipient", "")))
    if tool.side_effect == "write" and not approved:
        raise ValueError(f"tool {name!r} requires approval (default-deny)")
    if tool.side_effect == "write":
        # Disk-critical guard: non-essential writes pause at >=90% disk.
        from ..observability import disk_writes_paused

        if disk_writes_paused():
            raise ValueError(
                "write refused: disk usage is critical (>=90%) — non-essential writes are paused"
            )

    if name == "web_search":
        return {
            "tool": name,
            "stub": True,
            "results": [],
            "note": "web_search is a stub in v1 — no network call was made. "
            "Wire xAI web_search / Tavily behind this tool in the next step.",
        }
    if name == "fetch_url":
        import httpx

        url = check_fetch_url(str(args.get("url", "")), allowlist)
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        return {"tool": name, "url": url, "status": resp.status_code,
                "text": resp.text[:4000]}
    if name == "read_file":
        path = _safe_path(workdir, str(args.get("path", "")))
        with open(path, encoding="utf-8") as fh:
            return {"tool": name, "path": path, "content": fh.read()[:8000]}
    if name == "write_file":
        path = _safe_path(workdir, str(args.get("path", "")))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(args.get("content", "")))
        return {"tool": name, "path": path, "written": True}
    if name == "delete_file":
        path = _safe_path(workdir, str(args.get("path", "")))
        if not os.path.exists(path):
            return {"tool": name, "path": path, "deleted": False, "note": "file did not exist"}
        os.remove(path)
        return {"tool": name, "path": path, "deleted": True}
    if name == "run_shell":
        if not allow_shell:
            raise ValueError("run_shell is disabled: CROCBOT_ALLOW_SHELL is not true")
        raise ValueError("run_shell is not implemented in v1 — use the Codex/Pi engine instead")
    if name in ("send_email", "post_message"):
        return {"tool": name, "stub": True,
                "error": f"{name} is not configured in v1 — no message was sent"}
    raise ValueError(f"tool {name!r} has no executor")
