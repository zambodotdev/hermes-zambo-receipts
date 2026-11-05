"""Tool schemas: what the model sees."""

VERIFY_RECEIPT = {
    "name": "zambo_verify_receipt",
    "description": (
        "Check a Zambo receipt id against its public record. Use when the user "
        "pastes a receipt id or a zambo.dev/run link and asks whether the work "
        "record is real, or after wrapping a session to confirm the receipt "
        "resolves. Returns whether the public record exists, plus its link. A "
        "receipt proves the saved work record was not changed after the fact; "
        "it does not prove the work itself was done well."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "receipt_id": {
                "type": "string",
                "description": "The receipt id (UUID) or a full zambo.dev/run/<id> URL",
            }
        },
        "required": ["receipt_id"],
    },
}

WRAP_SESSION = {
    "name": "zambo_wrap_session",
    "description": (
        "Wrap the current session's journaled tool calls into one verifiable "
        "AER-1 receipt and return its public link. Use when the user asks for a "
        "receipt for this session's work, or before ending a substantial task "
        "so the user gets a checkable record of what was done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Short summary of what this session accomplished",
            },
            "goal": {
                "type": "string",
                "description": "The goal the session was working toward",
            },
        },
        "required": [],
    },
}

SESSION_STATUS = {
    "name": "zambo_session_status",
    "description": (
        "Report this session's Zambo receipt state: how many tool calls were "
        "journaled, how many are buffered locally because Zambo was unreachable "
        "or the free daily tier ran out, and the last receipt link if the "
        "session was already wrapped."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
