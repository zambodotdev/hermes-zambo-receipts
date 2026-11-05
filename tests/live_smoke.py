"""Live smoke test: drives the real hooks against production Zambo.

Run:  python3 tests/live_smoke.py
Prints the wrapped session's public receipt link. Uses a throwaway caller
id, so it consumes a few of that caller's free daily calls and nothing else.
"""

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import ZamboClient
from hooks import Journal, make_hooks


def main():
    caller = "hermes-smoke-" + uuid.uuid4().hex[:6]
    journal = Journal(ZamboClient(caller=caller))
    start, post, end = make_hooks(journal)
    sid = "live-smoke-session"
    start(session_id=sid, model="smoke", platform="cli")
    post(tool_name="terminal", args={"command": "ls"}, result="a.txt b.txt",
         task_id="t", duration_ms=12, session_id=sid)
    post(tool_name="web_search", args={"q": "zambo"}, result="1 result",
         task_id="t", duration_ms=95, session_id=sid)
    end(session_id=sid, completed=True, interrupted=False)
    rec = journal.sessions[sid]
    assert rec.journaled == len(rec.calls) == 2, "every call must journal live"
    assert rec.wrapped_receipt, "session must wrap into a receipt"
    check = journal.client.check_receipt(rec.wrapped_receipt)
    assert check["ok"], "the receipt must verify publicly"
    print(json.dumps({
        "caller": caller,
        "journaled": rec.journaled,
        "receipt": rec.wrapped_receipt,
        "url": check["url"],
        "verified": True,
    }, indent=2))


if __name__ == "__main__":
    main()
