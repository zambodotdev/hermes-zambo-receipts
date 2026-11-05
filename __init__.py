"""Zambo Receipts for Hermes Agent: registration.

Wires the journaling hooks, the three tools, the /receipt command, and a
bundled skill into Hermes. Everything degrades quietly: if Zambo is down or
the plugin misconfigures, Hermes keeps working and calls wait in the outbox.
"""

import logging
from pathlib import Path

try:
    from . import schemas, tools as tools_mod
    from .client import ZamboClient
    from .hooks import Journal, load_caller_id, make_hooks
except ImportError:  # loaded as a bare module: import as siblings instead
    import os as _os
    import sys as _sys

    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import schemas
    import tools as tools_mod
    from client import ZamboClient
    from hooks import Journal, load_caller_id, make_hooks

logger = logging.getLogger(__name__)


def _setting(ctx, key, default):
    try:
        value = ctx.get_config(key, default=default)
    except Exception:
        return default
    return default if value is None else value


def register(ctx):
    api_url = _setting(ctx, "api_url", "https://zambo.dev/mcp")
    auto_journal = bool(_setting(ctx, "auto_journal", True))
    auto_wrap = bool(_setting(ctx, "auto_wrap", True))
    journal_results = str(_setting(ctx, "journal_results", "hash"))

    client = ZamboClient(base_url=api_url, caller=load_caller_id())
    journal = Journal(
        client,
        auto_journal=auto_journal,
        auto_wrap=auto_wrap,
        journal_results=journal_results,
    )

    verify_receipt, wrap_session, session_status = tools_mod.make_tools(journal)
    ctx.register_tool(
        name="zambo_verify_receipt",
        toolset="zambo_receipts",
        schema=schemas.VERIFY_RECEIPT,
        handler=verify_receipt,
    )
    ctx.register_tool(
        name="zambo_wrap_session",
        toolset="zambo_receipts",
        schema=schemas.WRAP_SESSION,
        handler=wrap_session,
    )
    ctx.register_tool(
        name="zambo_session_status",
        toolset="zambo_receipts",
        schema=schemas.SESSION_STATUS,
        handler=session_status,
    )

    on_session_start, post_tool_call, on_session_end, on_session_finalize = make_hooks(journal)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)

    ctx.register_command(
        "receipt",
        tools_mod.make_receipt_command(journal),
        description="Check a Zambo receipt id, or show the latest session receipt",
    )

    try:
        ctx.register_skill(
            "zambo-receipts",
            Path(__file__).parent / "skills" / "zambo-receipts" / "SKILL.md",
            description=(
                "Work with Zambo verifiable receipts in this session: wrap "
                "finished work into a checkable receipt, verify receipts the "
                "user shares, and report the session's journal state."
            ),
        )
    except Exception:
        logger.debug("zambo-receipts: skill registration skipped", exc_info=True)

    logger.info(
        "zambo-receipts: journaling on=%s, auto-wrap on=%s, results=%s",
        auto_journal, auto_wrap, journal_results,
    )
