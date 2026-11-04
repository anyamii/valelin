"""Conversation predicates retained from the formal backend; no prompt injection."""
def is_desktop_session(session_id: str) -> bool:
    return bool(session_id) and session_id.startswith("api-")
def desktop_line_clause() -> str:
    return "(json_extract(meta, '$.call_session_id') IS NULL OR json_extract(meta, '$.call_session_id') = '')"
