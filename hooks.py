"""Session journaling for the Zambo receipts plugin.

Every Hermes tool call is recorded locally the moment it returns. When
auto-journal is on, the call is also journaled to Zambo right away; if Zambo
is unreachable (or the caller's free daily tier is used up), the entry waits
in a durable outbox on disk and is flushed at wrap time or session end.

Privacy: by default only the tool name, duration, result size, and the
SHA-256 of the result are journaled. Result contents are sent only when the
user sets journal_results to "full". Receipt pages are public: anyone with
the link can open them, and the summary and goal text passed to a wrap is
published on the receipt page.

Wrapping happens in on_session_finalize (fired on /new, quit, and gateway
reset), not in on_session_end, which fires at the end of every turn and now
only flushes the outbox. A session re-wraps only when new calls have
arrived since its last receipt.
"""

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

try:
    from .client import ZamboClient, ZamboError, ZamboQuotaError
except ImportError:  # free-module loading (test harnesses)
    from client import ZamboClient, ZamboError, ZamboQuotaError

logger = logging.getLogger(__name__)

OWN_TOOLS = ("zambo_verify_receipt", "zambo_wrap_session", "zambo_session_status")
MAX_RESULT_CHARS = 4000
# Outbox bounds: a dead network or a spent free tier must not turn the
# outbox into an unbounded retry queue.
OUTBOX_MAX_ENTRIES = 500
OUTBOX_MAX_AGE_S = 7 * 24 * 3600


def _next_day_ts(now=None):
    """Start of the next UTC day: when a daily quota can reset."""
    now = time.time() if now is None else now
    return now - (now % 86400) + 86400


def _hermes_home():
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        return Path(home)


def data_dir():
    path = _hermes_home() / "plugin-data" / "zambo-receipts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_caller_id():
    """One stable caller identity per install, so each user's Zambo free
    daily tier is their own, never shared with other installs."""
    path = data_dir() / "caller.txt"
    try:
        value = path.read_text().strip()
        if value:
            return value
    except Exception:
        pass
    value = "hermes-" + uuid.uuid4().hex[:10]
    try:
        path.write_text(value)
    except Exception:
        pass
    return value


class SessionRecord:
    def __init__(self, session_key):
        self.session_key = session_key
        self.job_id = str(uuid.uuid4())
        self.zambo_session = "hermes-" + "".join(
            ch if ch.isalnum() or ch in "-_" else "-" for ch in str(session_key)
        )[:80]
        self.started = time.time()
        self.calls = []          # every observed tool call (local truth)
        self.journaled = 0       # calls confirmed journaled to Zambo
        self.wrapped_receipt = None
        self.wrapped_call_count = 0  # len(calls) at the last successful wrap

    def digest(self):
        canon = json.dumps(
            [
                {
                    "tool": c["tool"],
                    "sha256": c["sha256"],
                    "bytes": c["bytes"],
                    "duration_ms": c["duration_ms"],
                }
                for c in self.calls
            ],
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def tool_summary(self):
        counts = {}
        for c in self.calls:
            counts[c["tool"]] = counts.get(c["tool"], 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
        return counts, top


class Journal:
    """Owns the Zambo client, session records, and the durable outbox."""

    def __init__(self, client, *, auto_journal=True, auto_wrap=True,
                 journal_results="hash"):
        self.client = client
        self.auto_journal = auto_journal
        self.auto_wrap = auto_wrap
        self.journal_results = journal_results
        self.sessions = {}
        self.outbox_path = data_dir() / "outbox.jsonl"

    # -- session lifecycle -------------------------------------------------
    def session(self, key):
        if key not in self.sessions:
            self.sessions[key] = SessionRecord(key)
        return self.sessions[key]

    # -- outbox -------------------------------------------------------------
    @staticmethod
    def _outbox_prune(entries, now=None):
        """Drop entries past the age cap; stamp legacy entries that predate
        ts tracking so they get a full age window from first sight."""
        now = time.time() if now is None else now
        kept = []
        for entry in entries:
            entry.setdefault("ts", now)
            if now - entry["ts"] <= OUTBOX_MAX_AGE_S:
                kept.append(entry)
        return kept

    def _outbox_add(self, entry):
        entry.setdefault("ts", time.time())
        try:
            entries = self._outbox_prune(self._outbox_read())
            entries.append(entry)
            if len(entries) > OUTBOX_MAX_ENTRIES:
                entries = entries[-OUTBOX_MAX_ENTRIES:]
            self._outbox_write_all(entries)
        except Exception:
            logger.debug("zambo-receipts: outbox write failed", exc_info=True)

    def _outbox_read(self):
        try:
            lines = self.outbox_path.read_text(encoding="utf-8").splitlines()
            return [json.loads(l) for l in lines if l.strip()]
        except Exception:
            return []

    def _outbox_write_all(self, entries):
        try:
            tmp = self.outbox_path.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
            )
            tmp.replace(self.outbox_path)
        except Exception:
            logger.debug("zambo-receipts: outbox rewrite failed", exc_info=True)

    def outbox_size(self):
        return len(self._outbox_read())

    def flush_outbox(self, limit=50):
        """Best-effort: journal buffered entries, keep the ones that fail.

        Entries past the age cap are dropped. An entry Zambo rejected for
        quota is parked until the next day (retry_after) and skipped, not
        retried, by later flushes: a spent free tier must not turn every
        flush into a network call that cannot succeed.
        """
        now = time.time()
        entries = self._outbox_prune(self._outbox_read(), now=now)
        if not entries:
            self._outbox_write_all(entries)
            return 0
        sent, keep = 0, []
        for entry in entries:
            if sent >= limit:
                keep.append(entry)
                continue
            if entry.get("retry_after", 0) > now:
                keep.append(entry)
                continue
            try:
                self.client.journal(
                    session_id=entry["zambo_session"],
                    job_id=entry["job_id"],
                    tool_name=entry["tool"],
                    metadata=entry["metadata"],
                )
                sent += 1
                rec = self.sessions.get(entry["session_key"])
                if rec is not None:
                    rec.journaled += 1
            except ZamboQuotaError:
                entry["retry_after"] = _next_day_ts(now)
                keep.append(entry)
            except ZamboError:
                keep.append(entry)
        self._outbox_write_all(keep)
        return sent

    # -- recording -----------------------------------------------------------
    def record_call(self, session_key, tool_name, result, duration_ms):
        rec = self.session(session_key)
        result_text = result if isinstance(result, str) else json.dumps(
            result, default=str
        )
        entry = {
            "tool": tool_name,
            "sha256": hashlib.sha256(result_text.encode("utf-8")).hexdigest(),
            "bytes": len(result_text.encode("utf-8")),
            "duration_ms": duration_ms,
            "ts": time.time(),
        }
        rec.calls.append(entry)
        if not self.auto_journal:
            return
        metadata = {
            "hermes_tool": tool_name,
            "result_sha256": entry["sha256"],
            "result_bytes": entry["bytes"],
            "duration_ms": duration_ms,
        }
        if self.journal_results == "full":
            metadata["result"] = result_text[:MAX_RESULT_CHARS]
        try:
            self.client.journal(
                session_id=rec.zambo_session,
                job_id=rec.job_id,
                tool_name=tool_name,
                metadata=metadata,
            )
            entry["journaled"] = True
            rec.journaled += 1
        except ZamboError:
            entry["journaled"] = False
            self._outbox_add(
                {
                    "session_key": session_key,
                    "zambo_session": rec.zambo_session,
                    "job_id": rec.job_id,
                    "tool": tool_name,
                    "metadata": metadata,
                }
            )

    # -- wrapping -------------------------------------------------------------
    def wrap_session(self, session_key, *, summary=None, goal=None):
        rec = self.session(session_key)
        self.flush_outbox()
        counts, top = rec.tool_summary()
        digest = rec.digest()
        # Bind the full local log's digest into the job before wrapping, so
        # the receipt commits to calls Zambo never saw (offline, over quota).
        try:
            self.client.journal(
                session_id=rec.zambo_session,
                job_id=rec.job_id,
                tool_name="session_log_digest",
                metadata={
                    "log_sha256": digest,
                    "calls": len(rec.calls),
                    "journaled": rec.journaled,
                    "tools": counts,
                },
            )
        except ZamboError:
            pass
        if not summary:
            tools_txt = ", ".join(f"{name} x{n}" for name, n in top) or "no tools"
            summary = (
                f"Hermes Agent session {str(session_key)[:8]}: "
                f"{len(rec.calls)} tool calls ({tools_txt}). "
                f"Session log SHA-256 {digest[:16]}..., "
                f"{rec.journaled} of {len(rec.calls)} calls journaled live."
            )
        receipt_id = self.client.wrap(
            session_id=rec.zambo_session,
            summary=summary,
            goal=goal or f"Hermes Agent session {session_key}",
            recipient="the session's user",
            next_steps=(
                "Open the receipt link to review the session's work record. "
                f"Full local log digest: sha256:{digest}"
            ),
        )
        rec.wrapped_receipt = receipt_id
        rec.wrapped_call_count = len(rec.calls)
        return receipt_id


def make_hooks(journal):
    """Build the lifecycle callbacks bound to one Journal."""

    def _key(kwargs, fallback=None):
        return kwargs.get("session_id") or kwargs.get("task_id") or fallback or "default"

    def on_session_start(session_id=None, **kwargs):
        try:
            journal.session(_key(kwargs, session_id))
        except Exception:
            logger.debug("zambo-receipts: session start failed", exc_info=True)

    def post_tool_call(tool_name=None, args=None, result=None, task_id=None,
                       duration_ms=None, **kwargs):
        try:
            if tool_name in OWN_TOOLS:
                return
            journal.record_call(_key(kwargs, task_id), tool_name, result, duration_ms)
        except Exception:
            logger.debug("zambo-receipts: journal hook failed", exc_info=True)

    def on_session_end(session_id=None, completed=None, interrupted=None, **kwargs):
        # Fires at the end of every turn: flush only, never wrap here.
        try:
            journal.flush_outbox()
        except Exception:
            logger.debug("zambo-receipts: session end flush failed", exc_info=True)

    def on_session_finalize(session_id=None, **kwargs):
        # Fires once per session boundary (/new, quit, gateway reset):
        # flush, then wrap, but only when new calls arrived since the
        # session's last receipt.
        try:
            key = _key(kwargs, session_id)
            journal.flush_outbox()
            rec = journal.sessions.get(key)
            if (
                journal.auto_wrap
                and rec is not None
                and len(rec.calls) > rec.wrapped_call_count
            ):
                receipt_id = journal.wrap_session(key)
                logger.info(
                    "zambo-receipts: session %s wrapped: https://zambo.dev/run/%s",
                    key, receipt_id,
                )
        except Exception:
            logger.warning("zambo-receipts: session finalize wrap failed", exc_info=True)

    return on_session_start, post_tool_call, on_session_end, on_session_finalize
