"""Tool handlers for the Zambo receipts plugin. Always return JSON strings."""

import json

try:
    from .client import RUN_URL, ZamboError
except ImportError:  # free-module loading (test harnesses)
    from client import RUN_URL, ZamboError


def _session_key(kwargs):
    return kwargs.get("session_id") or kwargs.get("task_id") or "default"


def make_tools(journal):
    client = journal.client

    def verify_receipt(args, **kwargs):
        raw = (args or {}).get("receipt_id", "").strip()
        receipt_id = raw.rstrip("/").split("/")[-1] if raw else ""
        if not receipt_id:
            return json.dumps({"error": "No receipt id provided"})
        try:
            check = client.check_receipt(receipt_id)
        except ZamboError as e:
            return json.dumps({"receipt_id": receipt_id, "error": str(e)})
        return json.dumps(
            {
                "receipt_id": receipt_id,
                "record_found": check["ok"],
                "url": check["url"],
                "note": (
                    "The public record resolves: the saved work record for this "
                    "receipt exists and matches what was committed. A receipt "
                    "proves the record was not changed after the fact, not "
                    "that the work was done well."
                    if check["ok"]
                    else "No public record at that id. Check the id and try again."
                ),
            }
        )

    def wrap_session(args, **kwargs):
        key = _session_key(kwargs)
        args = args or {}
        try:
            receipt_id = journal.wrap_session(
                key, summary=args.get("summary"), goal=args.get("goal")
            )
        except ZamboError as e:
            return json.dumps({"error": f"Wrap failed: {e}"})
        rec = journal.session(key)
        return json.dumps(
            {
                "receipt_id": receipt_id,
                "url": RUN_URL.format(receipt_id=receipt_id),
                "calls": len(rec.calls),
                "journaled_live": rec.journaled,
                "log_sha256": rec.digest(),
            }
        )

    def session_status(args, **kwargs):
        key = _session_key(kwargs)
        rec = journal.sessions.get(key)
        if rec is None:
            return json.dumps(
                {
                    "session": key,
                    "calls": 0,
                    "note": "No tool calls recorded for this session yet.",
                    "outbox_pending": journal.outbox_size(),
                }
            )
        return json.dumps(
            {
                "session": key,
                "calls": len(rec.calls),
                "journaled_live": rec.journaled,
                "outbox_pending": journal.outbox_size(),
                "wrapped_receipt": rec.wrapped_receipt,
                "receipt_url": (
                    RUN_URL.format(receipt_id=rec.wrapped_receipt)
                    if rec.wrapped_receipt
                    else None
                ),
                "log_sha256": rec.digest(),
            }
        )

    return verify_receipt, wrap_session, session_status


def make_receipt_command(journal):
    """The /receipt slash command: '/receipt <id>' verifies, bare shows status."""

    def handle(raw_args):
        raw = (raw_args or "").strip()
        if not raw:
            last = None
            for rec in journal.sessions.values():
                if rec.wrapped_receipt:
                    last = rec.wrapped_receipt
            if last:
                return f"Latest session receipt: {RUN_URL.format(receipt_id=last)}"
            return (
                "No wrapped session yet. Sessions wrap automatically when they "
                "end, or ask the agent to wrap this session for a receipt link."
            )
        receipt_id = raw.rstrip("/").split("/")[-1]
        try:
            check = journal.client.check_receipt(receipt_id)
        except ZamboError as e:
            return f"Could not check receipt {receipt_id}: {e}"
        if check["ok"]:
            return f"Receipt {receipt_id}: public record found at {check['url']}"
        return f"Receipt {receipt_id}: no public record found."

    return handle
