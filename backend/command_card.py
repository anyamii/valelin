"""Transactional storage and tools for the single active command card."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from backend.ledger import add_entry, ensure_ledger_schema
except ModuleNotFoundError:
    from ledger import add_entry, ensure_ledger_schema


DEFAULT_DB_PATH = "/data/relay.db"
MAX_COMMAND_CHARS = 480
MAX_COMMAND_MINUTES = 24 * 60
OUTCOMES = ("completed", "timed_out", "canceled")
DEFAULT_COMMAND_CARD_LIST_LIMIT = 100
MAX_COMMAND_CARD_LIST_LIMIT = 200
_ID_RE = re.compile(r"^[A-Fa-f0-9-]{16,80}$")
_TIME_ZONE = ZoneInfo("Asia/Shanghai")


class CommandCardError(ValueError):
    """A safe command-card validation or state error."""


def ensure_command_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_cards (
            id          TEXT PRIMARY KEY,
            text        TEXT NOT NULL,
            minutes     INTEGER NOT NULL CHECK (minutes BETWEEN 1 AND 1440),
            started_at  TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('active', 'completed', 'timed_out', 'canceled')),
            finished_at TEXT,
            message_id  INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_command_cards_started ON command_cards(started_at DESC, id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_command_cards_one_active "
        "ON command_cards(status) WHERE status = 'active'"
    )


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        raise CommandCardError("text must be a string")
    cleaned = " ".join("".join(ch if ch >= " " else " " for ch in value).split())
    if not cleaned or len(cleaned) > MAX_COMMAND_CHARS or "\x00" in value:
        raise CommandCardError(f"text must be non-empty and at most {MAX_COMMAND_CHARS} characters")
    return cleaned


def _minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_COMMAND_MINUTES:
        raise CommandCardError(f"minutes must be an integer from 1 to {MAX_COMMAND_MINUTES}")
    return value


def _record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _write_command_message_meta(
    conn: sqlite3.Connection,
    *,
    command_id: str,
    message_id: int,
    status: str,
    finished_at: str | None,
) -> bool:
    row = conn.execute("SELECT meta FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return False
    try:
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict):
        return False
    if (
        meta.get("command_card_id") == command_id
        and meta.get("command_card_status") == status
        and meta.get("command_card_finished_at") == finished_at
    ):
        return False
    meta.update({
        "command_card_id": command_id,
        "command_card_status": status,
        "command_card_finished_at": finished_at,
    })
    conn.execute(
        "UPDATE messages SET meta = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False, separators=(",", ":")), message_id),
    )
    return True


def backfill_command_message_meta(conn: sqlite3.Connection) -> int:
    """Backfill each command row's current status onto its linked chat message."""
    rows = conn.execute(
        "SELECT id, message_id, status, finished_at FROM command_cards ORDER BY started_at, id"
    ).fetchall()
    return sum(
        _write_command_message_meta(
            conn,
            command_id=row["id"],
            message_id=row["message_id"],
            status=row["status"],
            finished_at=row["finished_at"],
        )
        for row in rows
    )


def _add_timeout_penalty(
    conn: sqlite3.Connection,
    *,
    command_id: str,
    text: str,
    now: datetime,
) -> None:
    add_entry(
        conn,
        {
            "direction": "Vale_to_Lin",
            "kind": "penalty",
            "level": "light",
            "reason": f"指令超时：{text}",
            "action": "",
        },
        author="model",
        source="command",
        source_ref=command_id,
        status="pending_confirm",
        now=now,
    )


def expire_overdue_commands(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Close overdue cards once and create their pending-confirm penalty atomically."""
    current = _now(now)
    stamp = _utc_text(current)
    rows = conn.execute(
        "SELECT id, text, message_id FROM command_cards WHERE status = 'active' AND deadline_at <= ?",
        (stamp,),
    ).fetchall()
    expired: list[dict[str, Any]] = []
    for row in rows:
        cursor = conn.execute(
            "UPDATE command_cards SET status = 'timed_out', finished_at = ? "
            "WHERE id = ? AND status = 'active' AND deadline_at <= ?",
            (stamp, row["id"], stamp),
        )
        if cursor.rowcount != 1:
            continue
        _write_command_message_meta(
            conn,
            command_id=row["id"],
            message_id=row["message_id"],
            status="timed_out",
            finished_at=stamp,
        )
        # Pomodoro outcomes only; no reward or penalty ledger.
        updated = conn.execute("SELECT * FROM command_cards WHERE id = ?", (row["id"],)).fetchone()
        if updated is not None:
            expired.append(dict(updated))
    return expired


def push_command(
    conn: sqlite3.Connection,
    text: Any,
    minutes: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    cleaned = _clean_text(text)
    duration = _minutes(minutes)
    current = _now(now)
    started_at = _utc_text(current)
    deadline_at = _utc_text(current + timedelta(minutes=duration))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone() is None:
        raise CommandCardError("message storage unavailable")

    expire_overdue_commands(conn, now=current)
    active = conn.execute(
        "SELECT id FROM command_cards WHERE status = 'active' LIMIT 1"
    ).fetchone()
    if active is not None:
        raise CommandCardError("another command is still active")

    command_id = uuid.uuid4().hex
    line = f"[指令 · {cleaned} · {duration} min → 已推给她]"
    meta = json.dumps({
        "command_card_id": command_id,
        "command_card_status": "active",
        "command_card_finished_at": None,
    }, ensure_ascii=False, separators=(",", ":"))
    message = conn.execute(
        "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?, 'out', 'reply', ?, ?)",
        (started_at, line, meta),
    )
    conn.execute(
        "INSERT INTO command_cards (id, text, minutes, started_at, deadline_at, status, message_id) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (command_id, cleaned, duration, started_at, deadline_at, message.lastrowid),
    )
    row = conn.execute("SELECT * FROM command_cards WHERE id = ?", (command_id,)).fetchone()
    return _record(row) or {}


def finish_command(
    conn: sqlite3.Connection,
    command_id: Any,
    outcome: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(command_id, str) or not _ID_RE.fullmatch(command_id):
        raise CommandCardError("command id is invalid")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise CommandCardError("invalid command outcome")
    current = _now(now)
    expire_overdue_commands(conn, now=current)
    row = conn.execute("SELECT * FROM command_cards WHERE id = ?", (command_id,)).fetchone()
    if row is None:
        raise CommandCardError("command not found")
    if row["status"] != "active":
        if row["status"] == outcome:
            return dict(row)
        raise CommandCardError("command already ended")
    if outcome == "timed_out" and current < datetime.fromisoformat(row["deadline_at"]):
        raise CommandCardError("command deadline has not passed")

    status = outcome
    stamp = _utc_text(current)
    cursor = conn.execute(
        "UPDATE command_cards SET status = ?, finished_at = ? WHERE id = ? AND status = 'active'",
        (status, stamp, command_id),
    )
    if cursor.rowcount != 1:
        raise CommandCardError("command already ended")
    _write_command_message_meta(
        conn,
        command_id=command_id,
        message_id=row["message_id"],
        status=status,
        finished_at=stamp,
    )
    updated = conn.execute("SELECT * FROM command_cards WHERE id = ?", (command_id,)).fetchone()
    return _record(updated) or {}


def command_state_from_db(db_path: str | Path) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path), timeout=5.0, uri=str(db_path).startswith("file:"))
    conn.row_factory = sqlite3.Row
    try:
        ensure_command_schema(conn)
        active_row = conn.execute(
            "SELECT * FROM command_cards WHERE status = 'active' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        latest_row = conn.execute(
            "SELECT * FROM command_cards ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return {"active": _record(active_row), "latest": _record(latest_row)}
    finally:
        conn.close()


def recent_command_cards_from_db(
    db_path: str | Path,
    *,
    limit: int = DEFAULT_COMMAND_CARD_LIST_LIMIT,
) -> list[dict[str, Any]]:
    """Return a bounded newest-first status list for refreshing cached chat rows."""
    bounded_limit = max(1, min(int(limit), MAX_COMMAND_CARD_LIST_LIMIT))
    conn = sqlite3.connect(str(db_path), timeout=5.0, uri=str(db_path).startswith("file:"))
    conn.row_factory = sqlite3.Row
    try:
        ensure_command_schema(conn)
        rows = conn.execute(
            "SELECT id, status, finished_at FROM command_cards "
            "ORDER BY started_at DESC, id DESC LIMIT ?",
            (bounded_limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _local_time(raw: str | None) -> str:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_TIME_ZONE).strftime("%m-%d %H:%M")
    except (OverflowError, TypeError, ValueError):
        return "时间未知"


def command_context_from_db(
    db_path: str | Path, *, now: datetime | None = None, max_chars: int = 420
) -> str:
    """Expire overdue commands, then render only compact command-card facts."""
    conn = sqlite3.connect(str(db_path), timeout=5.0, uri=str(db_path).startswith("file:"))
    conn.row_factory = sqlite3.Row
    try:
        ensure_ledger_schema(conn)
        ensure_command_schema(conn)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            expire_overdue_commands(conn, now=now)
            active_row = conn.execute(
                "SELECT * FROM command_cards WHERE status = 'active' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            recent = conn.execute(
                "SELECT * FROM command_cards WHERE status != 'active' ORDER BY started_at DESC LIMIT 3"
            ).fetchall()
        if active_row is None and not recent:
            return ""
        lines = ["指令卡"]
        if active_row is not None:
            lines.append(
                f"进行中：{str(active_row['text'])[:100]}；开始 {_local_time(active_row['started_at'])}，"
                f"截止 {_local_time(active_row['deadline_at'])}。"
            )
        status_label = {"completed": "完成", "timed_out": "超时", "canceled": "取消"}
        for row in recent:
            lines.append(
                f"最近结果：{status_label.get(row['status'], '结束')}「{str(row['text'])[:64]}」"
                f"；{_local_time(row['finished_at'])}。"
            )
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            rendered = rendered[: max(0, max_chars - 1)].rstrip() + "…"
        return rendered
    finally:
        conn.close()


def command_tool_call(
    db_path: str,
    operation: str,
    arguments: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if operation != "command_push":
        raise CommandCardError("unknown command operation")
    if not isinstance(arguments, dict):
        raise CommandCardError("arguments must be an object")
    if set(arguments) != {"text", "minutes"}:
        raise CommandCardError("command_push requires text and minutes")
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        ensure_ledger_schema(conn)
        ensure_command_schema(conn)
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            command = push_command(conn, arguments["text"], arguments["minutes"], now=now)
        return {"command": command}
    finally:
        conn.close()


def main() -> int:
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise CommandCardError("request must be an object")
        result = command_tool_call(
            os.environ.get("RELAY_DB_PATH", DEFAULT_DB_PATH),
            request.get("operation"),
            request.get("arguments"),
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except (CommandCardError, sqlite3.Error, OSError) as exc:
        message = str(exc) if isinstance(exc, CommandCardError) else "command storage unavailable"
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
