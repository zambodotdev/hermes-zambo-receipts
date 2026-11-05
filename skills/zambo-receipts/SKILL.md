---
name: zambo-receipts
description: Work with Zambo verifiable receipts in this session: wrap finished work into a checkable receipt, verify receipts the user shares, and report the session's journal state.
---

# Zambo receipts

This Hermes install runs the Zambo Receipts plugin. Every tool call in the
session is journaled to Zambo as it happens, and the session can be wrapped
into one verifiable AER-1 receipt: a public record at zambo.dev/run/<id>
that commits to the session's tool calls and result hashes.

## When to wrap

- The user asks for a receipt, a record, or proof of the work.
- You finish a substantial multi-step task and the user would benefit from a
  checkable record of what was done.

Call `zambo_wrap_session` with a one or two sentence summary of what was
accomplished and the session's goal. Share the returned URL with the user.

## When to verify

- The user pastes a receipt id or a zambo.dev/run link.

Call `zambo_verify_receipt` with the id. Explain the boundary plainly: a
receipt proves the saved work record was not changed after the fact. It
does not prove the work was done well.

## Status

Call `zambo_session_status` when the user asks what has been journaled, or
when a wrap seems to have missed calls. Calls that could not reach Zambo
(network down, free daily tier used up) wait in a local outbox and flush
automatically; the wrap binds their hashes through the session log digest
either way.
