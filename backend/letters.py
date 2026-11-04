"""Private scheduled letters. Delivery is acknowledged by the foreground PWA."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

SHANGHAI = timezone(timedelta(hours=8))
STATUSES = ("sealed", "delivered", "read", "cancelled")
MAX_TEXT = 4000


class LetterError(ValueError):
    """Safe validation/state error with no private letter content."""


def ensure_letters_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS letters (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 4000),
            title TEXT,
            created_at TEXT NOT NULL,
            deliver_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('sealed','delivered','read','cancelled')),
            delivered_at TEXT,
            read_at TEXT,
            author TEXT NOT NULL DEFAULT 'Vale' CHECK(author = 'Vale'),
            occasion TEXT CHECK(occasion IS NULL OR occasion IN ('anniversary','period'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_letters_due ON letters(status, deliver_at, id)")


def _now(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise LetterError(f"{field} must be non-empty and at most {limit} characters")
    return value


def _deliver_time(value: Any, current: datetime) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", value
    ):
        raise LetterError("deliver_at must be Asia/Shanghai local time YYYY-MM-DD HH:MM[:SS]")
    try:
        parsed = datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).astimezone(timezone.utc)
    except ValueError as exc:
        raise LetterError("deliver_at is invalid") from exc
    if parsed <= current:
        raise LetterError("deliver_at must be later than now")
    return _stamp(parsed)


def _connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    # A tool must never silently allocate a database at the wrong path.
    from pathlib import Path
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def letter_tool_call(path, operation: str, arguments: Any, *, now=None) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise LetterError("arguments must be an object")
    fields = {"letter_write": {"text", "deliver_at", "title", "occasion"},
              "letter_list": {"status"}, "letter_cancel": {"id"}}
    if operation not in fields or set(arguments) - fields[operation]:
        raise LetterError("unsupported letter operation or arguments")
    current = _now(now)
    conn = _connect(path)
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE" if operation != "letter_list" else "BEGIN")
            if operation == "letter_write":
                text = _text(arguments.get("text"), "text", MAX_TEXT)
                deliver_at = _deliver_time(arguments.get("deliver_at"), current)
                title = arguments.get("title")
                if title is not None:
                    title = _text(title, "title", 200)
                occasion = arguments.get("occasion") or None
                if occasion not in (None, "anniversary", "period"):
                    raise LetterError("occasion must be anniversary, period, or empty")
                entry_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO letters(id,text,title,created_at,deliver_at,status,author,occasion) "
                    "VALUES(?,?,?,?,?,'sealed','Vale',?)",
                    (entry_id, text, title, _stamp(current), deliver_at, occasion),
                )
            elif operation == "letter_list":
                status = arguments.get("status")
                if status is not None and status not in STATUSES:
                    raise LetterError("invalid letter status")
                rows = conn.execute(
                    "SELECT * FROM letters " + ("WHERE status=? " if status else "") +
                    "ORDER BY deliver_at, id", (status,) if status else (),
                ).fetchall()
                return {"letters": [dict(row) for row in rows]}
            else:
                entry_id = _text(arguments.get("id"), "id", 80)
                changed = conn.execute(
                    "UPDATE letters SET status='cancelled' WHERE id=? AND status='sealed'", (entry_id,),
                ).rowcount
                if not changed:
                    raise LetterError("only sealed letters can be cancelled")
            return dict(conn.execute("SELECT * FROM letters WHERE id=?", (entry_id,)).fetchone())
    except sqlite3.Error as exc:
        raise LetterError("letter storage unavailable") from exc
    finally:
        conn.close()


def due_letters(conn: sqlite3.Connection, *, now=None) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM letters WHERE (status='sealed' AND deliver_at<=?) "
        "OR status='delivered' ORDER BY deliver_at, id", (_stamp(_now(now)),),
    ).fetchall()]


def visible_letters(conn: sqlite3.Connection, *, occasion=None, now=None) -> list[dict[str, Any]]:
    # No future sealed row, metadata, or count reaches any PWA endpoint.
    if occasion is not None and occasion not in ("anniversary", "period"):
        raise LetterError("invalid occasion")
    rows = conn.execute(
        "SELECT * FROM letters WHERE status IN ('delivered','read') "
        + ("AND occasion=? " if occasion else "") + "ORDER BY deliver_at DESC, id",
        (occasion,) if occasion else (),
    ).fetchall()
    today = _now(now).astimezone(SHANGHAI).date()
    return [dict(row) for row in rows if not occasion or
            datetime.fromisoformat(row["delivered_at"]).astimezone(SHANGHAI).date() == today]


def mark_letter(conn: sqlite3.Connection, entry_id: str, state: str, *, now=None) -> dict[str, Any]:
    current = _stamp(_now(now))
    # Identical 404 for a missing id and a still-sealed/cancelled id; no oracle.
    if state == "delivered":
        conn.execute(
            "UPDATE letters SET status='delivered', delivered_at=? "
            "WHERE id=? AND status='sealed' AND deliver_at<=?", (current, entry_id, current),
        )
    elif state == "read":
        conn.execute(
            "UPDATE letters SET status='read', read_at=? WHERE id=? AND status='delivered'",
            (current, entry_id),
        )
    else:
        raise LetterError("invalid letter state")
    row = conn.execute(
        "SELECT * FROM letters WHERE id=? AND status IN ('delivered','read')", (entry_id,),
    ).fetchone()
    if row is None:
        raise LetterError("letter not found")
    return dict(row)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise LetterError("request must be an object")
        result = letter_tool_call(os.environ.get("RELAY_DB_PATH", "/data/relay.db"),
                                  payload.get("operation"), payload.get("arguments"))
        response = {"ok": True, "result": result}
    except (LetterError, json.JSONDecodeError) as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception:
        response = {"ok": False, "error": "letter storage unavailable"}
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
