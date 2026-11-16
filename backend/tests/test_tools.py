import os

import pytest

from app.agent.loop import judge_text
from app.agent.tools import TOOLS, execute_tool

WORKDIR = os.environ["CROCBOT_WORKDIR"]


def test_judge_classifies():
    assert judge_text("hello, can you explain this?")[0] == "APPROVE"
    assert judge_text("")[0] == "PASS"
    assert judge_text("  ")[0] == "PASS"
    assert judge_text("FREE MONEY click here to win")[0] == "SPAM"


def test_pay_tool_is_refused_even_when_approved():
    with pytest.raises(Exception, match="pay is disabled"):
        execute_tool("pay", {"amount_usd": 5, "recipient": "x"},
                     workdir=WORKDIR, allowlist=[], allow_shell=False, approved=True)


def test_write_tool_requires_approval():
    with pytest.raises(ValueError, match="requires approval"):
        execute_tool("write_file", {"path": "x.txt", "content": "hi"},
                     workdir=WORKDIR, allowlist=[], allow_shell=False, approved=False)


def test_write_tool_runs_when_approved(tmp_path):
    result = execute_tool("write_file", {"path": "note.txt", "content": "hello"},
                          workdir=str(tmp_path), allowlist=[], allow_shell=False, approved=True)
    assert result["written"] is True
    assert (tmp_path / "note.txt").read_text() == "hello"


def test_read_file_cannot_escape_workdir(tmp_path):
    with pytest.raises(ValueError, match="escapes the working directory"):
        execute_tool("read_file", {"path": "../secret.txt"},
                     workdir=str(tmp_path), allowlist=[], allow_shell=False)


def test_fetch_default_deny_with_empty_allowlist():
    with pytest.raises(ValueError, match="is empty"):
        execute_tool("fetch_url", {"url": "https://example.com"},
                     workdir=WORKDIR, allowlist=[], allow_shell=False)


def test_fetch_blocks_private_ip_even_when_listed():
    # 127.0.0.1 on the allow-list must still be refused (SSRF).
    with pytest.raises(ValueError, match="blocked IP"):
        execute_tool("fetch_url", {"url": "http://127.0.0.1/admin"},
                     workdir=WORKDIR, allowlist=["127.0.0.1"], allow_shell=False)


def test_all_tools_declare_side_effect():
    for name, tool in TOOLS.items():
        assert tool.side_effect in ("read", "write"), name
        if tool.side_effect == "write":
            assert tool.requires_approval, name
