"""Transactional storage and model tools for the reward and penalty ledger."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_DB_PATH = "/data/relay.db"
DIRECTIONS = ("Vale_to_Lin", "Lin_to_Vale")
KINDS = ("reward", "penalty")
LEVELS = ("light", "medium", "heavy")
STATUSES = ("open", "pending_confirm", "settled", "voided")
SOURCES = ("manual", "command", "daily_task")
AUTHORS = ("model", "user")
_ID_RE = re.compile(r"^att-[A-Za-z0-9_-]{4,80}$")
_UPLOAD_NAME_RE = re.compile(r"^(att-[A-Za-z0-9_-]{4,80})\.([A-Za-z0-9]{1,8})$")


class LedgerToolError(ValueError):
    """A safe, user-facing ledger validation or storage error."""


def ensure_ledger_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ledger_entries (
            id           TEXT PRIMARY KEY,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            direction    TEXT NOT NULL CHECK (direction IN ('Vale_to_Lin', 'Lin_to_Vale')),
            kind         TEXT NOT NULL CHECK (kind IN ('reward', 'penalty')),
            level        TEXT NOT NULL CHECK (level IN ('light', 'medium', 'heavy')),
            reason       TEXT NOT NULL,
            action       TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL CHECK (status IN ('open', 'pending_confirm', 'settled', 'voided')),
            settled_at   TEXT,
            settle_note  TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT,
            voided_at    TEXT,
            void_note    TEXT NOT NULL DEFAULT '',
            evidence_json TEXT,
            author       TEXT NOT NULL CHECK (author IN ('model', 'user')),
            source       TEXT NOT NULL CHECK (source IN ('manual', 'command', 'daily_task')),
            source_ref   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_entries_created ON ledger_entries(created_at DESC, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_entries_status ON ledger_entries(status, created_at DESC)"
    )


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _next_updated_at(current: str | None, now: datetime) -> str:
    stamp = now
    if current:
        try:
            prior = datetime.fromisoformat(current.replace("Z", "+00:00"))
            if prior.tzinfo is None:
                prior = prior.replace(tzinfo=timezone.utc)
            prior = prior.astimezone(timezone.utc)
            if stamp <= prior:
                stamp = prior + timedelta(microseconds=1)
        except ValueError:
            pass
    return _utc_text(stamp)


def _text(value: Any, field: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise LedgerToolError(f"{field} must be a string")
    cleaned = " ".join("".join(ch if ch >= " " else " " for ch in value).split())
    if (not cleaned and not allow_empty) or len(cleaned) > limit or "\x00" in value:
        requirement = f"at most {limit} characters" if allow_empty else f"non-empty and at most {limit} characters"
        raise LedgerToolError(f"{field} must be {requirement}")
    return cleaned


def _choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise LedgerToolError(f"invalid {field}")
    return value


def _clean_fields(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise LedgerToolError("ledger entry must be an object")
    return {
        "direction": _choice(payload.get("direction"), "direction", DIRECTIONS),
        "kind": _choice(payload.get("kind"), "kind", KINDS),
        "level": _choice(payload.get("level"), "level", LEVELS),
        "reason": _text(payload.get("reason"), "reason", 500),
        "action": _text(payload.get("action", ""), "action", 300, allow_empty=True),
    }


def clean_evidence(
    value: Any,
    *,
    upload_dir: str | Path,
    public_prefix: str = "",
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LedgerToolError("evidence must be an uploaded image")
    image_id = value.get("id")
    raw_url = value.get("url")
    mime = value.get("mime")
    if not isinstance(image_id, str) or not _ID_RE.fullmatch(image_id):
        raise LedgerToolError("evidence id is invalid")
    if not isinstance(raw_url, str) or not isinstance(mime, str) or not mime.lower().startswith("image/"):
        raise LedgerToolError("evidence must be an uploaded image")
    parsed = urlsplit(raw_url)
    filename = Path(parsed.path).name
    match = _UPLOAD_NAME_RE.fullmatch(filename)
    if parsed.path != f"/uploads/{filename}" or not match or match.group(1) != image_id:
        raise LedgerToolError("evidence URL is invalid")
    root = Path(upload_dir).resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise LedgerToolError("evidence upload was not found")
    prefix = public_prefix.rstrip("/")
    url = f"{prefix}/uploads/{filename}" if prefix else f"/uploads/{filename}"
    name = value.get("name")
    safe_name = _text(name if isinstance(name, str) else filename, "evidence name", 180)
    return {"id": image_id, "url": url, "name": safe_name, "mime": mime.lower(), "kind": "image"}


def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    raw_evidence = item.pop("evidence_json", None)
    try:
        item["evidence"] = json.loads(raw_evidence) if raw_evidence else None
    except (TypeError, json.JSONDecodeError):
        item["evidence"] = None
    return item


def get_entry(conn: sqlite3.Connection, entry_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
    return _record(row)


def list_entries(
    conn: sqlite3.Connection, status: str | None = None
) -> list[dict[str, Any]]:
    if status is not None:
        _choice(status, "status", STATUSES)
        rows = conn.execute(
            "SELECT * FROM ledger_entries WHERE status = ? ORDER BY created_at DESC, id",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM ledger_entries ORDER BY created_at DESC, id").fetchall()
    return [entry for row in rows if (entry := _record(row)) is not None]


def add_entry(
    conn: sqlite3.Connection,
    payload: Any,
    *,
    author: str,
    source: str = "manual",
    source_ref: str | None = None,
    status: str = "open",
    evidence: Any = None,
    upload_dir: str | Path = ".",
    public_prefix: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    fields = _clean_fields(payload)
    author = _choice(author, "author", AUTHORS)
    source = _choice(source, "source", SOURCES)
    status = _choice(status, "status", ("open", "pending_confirm"))
    if status == "pending_confirm" and fields["kind"] != "penalty":
        raise LedgerToolError("only penalties can await confirmation")
    if source_ref is not None:
        source_ref = _text(source_ref, "source_ref", 180)
    clean_attachment = clean_evidence(
        evidence, upload_dir=upload_dir, public_prefix=public_prefix
    )
    stamp = _utc_text(_now(now))
    entry_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO ledger_entries (
            id, created_at, updated_at, direction, kind, level, reason, action, status,
            evidence_json, author, source, source_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry_id, stamp, stamp, fields["direction"], fields["kind"], fields["level"],
            fields["reason"], fields["action"], status,
            json.dumps(clean_attachment, ensure_ascii=False, separators=(",", ":"))
            if clean_attachment else None,
            author, source, source_ref,
        ),
    )
    return get_entry(conn, entry_id)  # type: ignore[return-value]


def update_user_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    payload: Any,
    *,
    expected_updated_at: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    fields = _clean_fields(payload)
    entry = get_entry(conn, entry_id)
    if entry is None:
        raise LedgerToolError("ledger entry not found")
    if entry["author"] != "user":
        raise LedgerToolError("only user-authored entries can be edited")
    if entry["status"] == "voided":
        raise LedgerToolError("voided ledger entries cannot be edited")
    if not isinstance(expected_updated_at, str) or expected_updated_at != entry["updated_at"]:
        raise LedgerToolError("ledger entry changed; refresh before editing")
    updated_at = _next_updated_at(entry["updated_at"], _now(now))
    cursor = conn.execute(
        """UPDATE ledger_entries SET direction = ?, kind = ?, level = ?, reason = ?,
           action = ?, updated_at = ? WHERE id = ? AND author = 'user' AND updated_at = ?""",
        (
            fields["direction"], fields["kind"], fields["level"], fields["reason"],
            fields["action"], updated_at, entry_id, expected_updated_at,
        ),
    )
    if cursor.rowcount != 1:
        raise LedgerToolError("ledger entry changed; refresh before editing")
    changed = get_entry(conn, entry_id)
    if changed is None:
        raise LedgerToolError("ledger entry not found")
    if changed["updated_at"] != updated_at:
        raise LedgerToolError("ledger entry changed; refresh before editing")
    return changed


def settle_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    note: Any = "",
    *,
    evidence: Any = None,
    replace_evidence: bool = False,
    upload_dir: str | Path = ".",
    public_prefix: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    entry = get_entry(conn, entry_id)
    if entry is None:
        raise LedgerToolError("ledger entry not found")
    if entry["status"] != "open":
        raise LedgerToolError("only open ledger entries can be settled")
    clean_note = _text(note, "note", 500, allow_empty=True)
    current_evidence = entry.get("evidence")
    if replace_evidence:
        current_evidence = clean_evidence(
            evidence, upload_dir=upload_dir, public_prefix=public_prefix
        )
    stamp = _utc_text(_now(now))
    updated_at = _next_updated_at(entry["updated_at"], _now(now))
    cursor = conn.execute(
        """UPDATE ledger_entries SET status = 'settled', settled_at = ?, settle_note = ?,
           evidence_json = ?, updated_at = ? WHERE id = ? AND status = 'open' AND updated_at = ?""",
        (
            stamp, clean_note,
            json.dumps(current_evidence, ensure_ascii=False, separators=(",", ":"))
            if current_evidence else None,
            updated_at, entry_id, entry["updated_at"],
        ),
    )
    if cursor.rowcount != 1:
        raise LedgerToolError("ledger entry changed; refresh before settling")
    return get_entry(conn, entry_id)  # type: ignore[return-value]


def confirm_entry(
    conn: sqlite3.Connection, entry_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    entry = get_entry(conn, entry_id)
    if entry is None:
        raise LedgerToolError("ledger entry not found")
    if entry["status"] != "pending_confirm":
        raise LedgerToolError("only pending ledger entries can be confirmed")
    stamp = _utc_text(_now(now))
    cursor = conn.execute(
        """UPDATE ledger_entries SET status = 'open', confirmed_at = ?, updated_at = ?
           WHERE id = ? AND status = 'pending_confirm' AND updated_at = ?""",
        (stamp, _next_updated_at(entry["updated_at"], _now(now)), entry_id, entry["updated_at"]),
    )
    if cursor.rowcount != 1:
        raise LedgerToolError("ledger entry changed before confirmation")
    return get_entry(conn, entry_id)  # type: ignore[return-value]


def void_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    note: Any = "",
    *,
    allowed_authors: tuple[str, ...] = ("model",),
    allow_pending: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    entry = get_entry(conn, entry_id)
    if entry is None:
        raise LedgerToolError("ledger entry not found")
    if entry["author"] not in allowed_authors:
        raise LedgerToolError("ledger entry author is protected")
    allowed_statuses = ("open", "pending_confirm") if allow_pending else ("open",)
    if entry["status"] not in allowed_statuses:
        raise LedgerToolError("ledger entry cannot be voided in its current status")
    clean_note = _text(note, "note", 500, allow_empty=True)
    stamp = _utc_text(_now(now))
    cursor = conn.execute(
        """UPDATE ledger_entries SET status = 'voided', voided_at = ?, void_note = ?,
           updated_at = ? WHERE id = ? AND status = ? AND updated_at = ?""",
        (stamp, clean_note, _next_updated_at(entry["updated_at"], _now(now)), entry_id,
         entry["status"], entry["updated_at"]),
    )
    if cursor.rowcount != 1:
        raise LedgerToolError("ledger entry changed before voiding")
    return get_entry(conn, entry_id)  # type: ignore[return-value]


def ledger_tool_call(
    db_path: str,
    operation: str,
    arguments: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise LedgerToolError("arguments must be an object")
    allowed = {
        "ledger_add": {"direction", "kind", "level", "reason", "action"},
        "ledger_list": {"status"},
        "ledger_settle": {"id", "note"},
        "ledger_void": {"id", "note"},
    }
    if operation not in allowed:
        raise LedgerToolError("unknown ledger operation")
    if set(arguments) - allowed[operation]:
        raise LedgerToolError("unexpected ledger arguments")
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        ensure_ledger_schema(conn)
        with conn:
            if operation == "ledger_add":
                result = add_entry(conn, arguments, author="model", source="manual", now=now)
                return {"entry": result}
            if operation == "ledger_list":
                status = arguments.get("status")
                if status is not None:
                    _choice(status, "status", STATUSES)
                return {"entries": list_entries(conn, status)}
            entry_id = _text(arguments.get("id"), "id", 80)
            note = arguments.get("note", "")
            if operation == "ledger_settle":
                result = settle_entry(conn, entry_id, note, now=now)
            else:
                result = void_entry(conn, entry_id, note, allowed_authors=("model",), now=now)
            return {"entry": result}
    finally:
        conn.close()


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise LedgerToolError("request must be an object")
        result = ledger_tool_call(
            os.environ.get("RELAY_DB_PATH", DEFAULT_DB_PATH),
            request.get("operation"), request.get("arguments"),
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except (LedgerToolError, sqlite3.Error, OSError) as exc:
        message = str(exc) if isinstance(exc, LedgerToolError) else "ledger storage unavailable"
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
