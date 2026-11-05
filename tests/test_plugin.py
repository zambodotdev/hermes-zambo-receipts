import json

import pytest
import yaml

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import __init__ as plugin_init

register = plugin_init.register
from client import ZamboClient, ZamboError
from hooks import Journal


class FakeCtx:
    def __init__(self, config=None):
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.skills = {}
        self.config = config or {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_hook(self, name, callback, **kw):
        self.hooks.setdefault(name, []).append(callback)

    def register_command(self, name, handler, description=""):
        self.commands[name] = handler

    def register_skill(self, name, text):
        self.skills[name] = text


class FakeClient(ZamboClient):
    """Records calls; can be switched into failure mode."""

    def __init__(self, fail=False):
        super().__init__(caller="hermes-test")
        self.fail = fail
        self.calls_made = []

    def call(self, tool, arguments, session_id, **kw):
        if self.fail:
            raise ZamboError("simulated outage")
        self.calls_made.append((tool, arguments, session_id))
        if tool == "handover":
            assert isinstance(arguments["next_steps"], list)
            return {"workflow_receipt_url": "https://zambo.dev/run/wrap-receipt-123"}
        if tool == "journal_log":
            assert isinstance(arguments["external_executor"], str)
            return {"status": "logged", "entry": {"entry_id": "entry-1"}}
        return {}

    def check_receipt(self, receipt_id):
        return {"ok": receipt_id == "good-id", "status": 200, "url": f"https://zambo.dev/run/{receipt_id}"}


@pytest.fixture(autouse=True)
def _tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_manifest_matches_registration():
    ctx = FakeCtx()
    register(ctx)
    manifest = yaml.safe_load(open("plugin.yaml"))
    assert set(manifest["provides_tools"]) == set(ctx.tools)
    assert set(manifest["provides_hooks"]) == set(ctx.hooks)
    assert "receipt" in ctx.commands
    assert "zambo-receipts" in ctx.skills


def test_journal_hook_hashes_and_skips_own_tools():
    client = FakeClient()
    journal = Journal(client)
    from hooks import make_hooks

    start, post, end = make_hooks(journal)
    start(session_id="s1")
    post(tool_name="terminal", args={}, result="file.txt", task_id="t", duration_ms=12, session_id="s1")
    post(tool_name="zambo_wrap_session", args={}, result="{}", task_id="t", session_id="s1")
    rec = journal.sessions["s1"]
    assert len(rec.calls) == 1
    assert rec.journaled == 1
    tool, args, _ = client.calls_made[-1]
    assert tool == "journal_log"
    meta = args["metadata"]
    assert len(meta["result_sha256"]) == 64
    assert "result" not in meta  # hash mode sends no contents


def test_outbox_buffers_on_failure_and_flushes():
    client = FakeClient(fail=True)
    journal = Journal(client)
    journal.record_call("sX", "read_file", "contents", 5)
    assert journal.outbox_size() == 1
    assert journal.sessions["sX"].journaled == 0
    client.fail = False
    assert journal.flush_outbox() == 1
    assert journal.outbox_size() == 0
    assert journal.sessions["sX"].journaled == 1


def test_session_end_auto_wraps():
    client = FakeClient()
    journal = Journal(client)
    from hooks import make_hooks

    start, post, end = make_hooks(journal)
    start(session_id="s9")
    post(tool_name="web_search", args={}, result="hits", task_id="t", session_id="s9")
    end(session_id="s9", completed=True, interrupted=False)
    rec = journal.sessions["s9"]
    assert rec.wrapped_receipt == "wrap-receipt-123"
    wrap = [c for c in client.calls_made if c[0] == "handover"]
    assert len(wrap) == 1
    args = wrap[0][1]
    assert args["summary"] and args["goal"] and args["recipient"] and args["next_steps"]


def test_wrap_tool_and_verify_tool():
    client = FakeClient()
    journal = Journal(client)
    from tools import make_tools

    verify, wrap, status = make_tools(journal)
    journal.record_call("sess", "terminal", "out", 3)
    out = json.loads(wrap({}, session_id="sess"))
    assert out["receipt_id"] == "wrap-receipt-123"
    assert out["url"].endswith("/run/wrap-receipt-123")
    ok = json.loads(verify({"receipt_id": "https://zambo.dev/run/good-id"}, session_id="sess"))
    assert ok["record_found"] is True
    st = json.loads(status({}, session_id="sess"))
    assert st["calls"] == 1 and st["wrapped_receipt"] == "wrap-receipt-123"


def test_full_mode_includes_truncated_result():
    client = FakeClient()
    journal = Journal(client, journal_results="full")
    journal.record_call("sF", "terminal", "x" * 9000, 1)
    meta = client.calls_made[-1][1]["metadata"]
    assert len(meta["result"]) == 4000


def test_rejection_is_an_error_not_a_success():
    import requests

    class RejectingClient(ZamboClient):
        def _post(self, payload, timeout=None):
            return {
                "result": {
                    "structuredContent": {
                        "rejection": True,
                        "executed": False,
                        "error": {"message": "bad args"},
                    }
                }
            }

    c = RejectingClient(caller="hermes-test")
    with pytest.raises(ZamboError):
        c.journal(session_id="s", job_id="j", tool_name="terminal", metadata={})
