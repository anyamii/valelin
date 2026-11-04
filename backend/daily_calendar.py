"""Transactional model tools for the two writable daily-calendar documents."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


DAILY_NOTES = "valelin_notes"
DAILY_ANNIVERSARIES = "valelin_anniv"
DAILY_KEYS = (DAILY_NOTES, DAILY_ANNIVERSARIES)
DEFAULT_DB_PATH = "/data/relay.db"
MODEL_WRITE_LIMIT = 10
_TIME_ZONE = ZoneInfo("Asia/Shanghai")
_MAX_DOCUMENT_BYTES = 64 * 1024


class CalendarToolError(ValueError):
    """A safe, user-facing calendar-tool validation or storage error."""


def legacy_note_id(day: str, text: str) -> str:
    """Return a stable id for a legacy string until its first mutation."""
    value = f"{day}\0{text}".encode("utf-16-le", "surrogatepass")
    hashed = 0x811C9DC5
    for index in range(0, len(value), 2):
        code_unit = value[index] | (value[index + 1] << 8)
        hashed = ((hashed ^ code_unit) * 0x01000193) & 0xFFFFFFFF
    return f"legacy-{hashed:08x}"


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _valid_timestamp(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def valid_daily_notes(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for day, entries in value.items():
        if not _valid_date(day) or not isinstance(entries, list):
            return False
        for entry in entries:
            if isinstance(entry, str):
                continue
            if not isinstance(entry, dict):
                return False
            if not isinstance(entry.get("id"), str) or not entry["id"].strip():
                return False
            if not isinstance(entry.get("t"), str):
                return False
            if "legacy_text" in entry and not isinstance(entry.get("legacy_text"), str):
                return False
            if entry.get("author", "user") not in ("user", "model"):
                return False
            if not _valid_timestamp(entry.get("ts")):
                return False
            if "done" in entry and not isinstance(entry["done"], bool):
                return False
            if entry.get("done_by") not in (None, "user", "model"):
                return False
            if entry.get("done_at") is not None and not isinstance(entry.get("done_at"), str):
                return False
    return True


def valid_daily_anniversaries(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for entry in value:
        if not isinstance(entry, dict):
            return False
        if not _valid_date(entry.get("date")) or not isinstance(entry.get("name"), str):
            return False
        if "yearly" in entry and not isinstance(entry["yearly"], bool):
            return False
        if entry.get("author", "user") not in ("user", "model"):
            return False
        if "id" in entry and (not isinstance(entry["id"], str) or not entry["id"].strip()):
            return False
        if not _valid_timestamp(entry.get("ts")):
            return False
    return True


def valid_daily_document(doc_key: str, value: Any) -> bool:
    if doc_key == DAILY_NOTES:
        return valid_daily_notes(value)
    if doc_key == DAILY_ANNIVERSARIES:
        return valid_daily_anniversaries(value)
    return False


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


def _arg_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise CalendarToolError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > limit or "\x00" in text:
        raise CalendarToolError(f"{field} must be non-empty and at most {limit} characters")
    return text


def _doc_data(conn: sqlite3.Connection, key: str, default: Any) -> tuple[Any, str | None]:
    row = conn.execute(
        "SELECT data_json, updated_at FROM daily_documents WHERE doc_key = ?", (key,)
    ).fetchone()
    if row is None:
        return default, None
    try:
        data = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise CalendarToolError(f"stored {key} document is invalid") from exc
    if not valid_daily_document(key, data):
        raise CalendarToolError(f"stored {key} document has an unsupported shape")
    return data, str(row[1])


def _encode_document(key: str, data: Any) -> str:
    if not valid_daily_document(key, data):
        raise CalendarToolError(f"updated {key} document is invalid")
    try:
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CalendarToolError(f"updated {key} document cannot be serialized") from exc
    if len(serialized.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
        raise CalendarToolError("daily document is too large")
    return serialized


def _save_document(
    conn: sqlite3.Connection,
    key: str,
    data: Any,
    current_updated_at: str | None,
    now: datetime,
) -> str:
    updated_at = _next_updated_at(current_updated_at, now)
    conn.execute(
        "INSERT INTO daily_documents (doc_key, data_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(doc_key) DO UPDATE SET data_json=excluded.data_json, "
        "updated_at=excluded.updated_at",
        (key, _encode_document(key, data), updated_at),
    )
    return updated_at


def _new_id(conn: sqlite3.Connection) -> str:
    for _ in range(5):
        candidate = uuid.uuid4().hex
        if not _find_entries(conn, candidate):
            return candidate
    raise CalendarToolError("could not allocate a unique calendar id")


def _entry_view(entry_id: str, day: str, text: str, author: str, **extra: Any) -> dict[str, Any]:
    return {"id": entry_id, "date": day, "text": text, "author": author, **extra}


def _legacy_anniversary_id(row: dict[str, Any]) -> str:
    value = row["date"] + chr(0) + row["name"]
    return f"legacy-anniv-{uuid.uuid5(uuid.NAMESPACE_URL, value).hex[:16]}"


def _find_entries(conn: sqlite3.Connection, entry_id: str) -> list[dict[str, Any]]:
    notes, _ = _doc_data(conn, DAILY_NOTES, {})
    anniversaries, _ = _doc_data(conn, DAILY_ANNIVERSARIES, [])
    found: list[dict[str, Any]] = []
    for day, rows in notes.items():
        for index, row in enumerate(rows):
            if isinstance(row, str):
                candidate_id = legacy_note_id(day, row)
                if candidate_id == entry_id:
                    found.append({
                        **_entry_view(candidate_id, day, row, "user", done=False),
                        "doc_key": DAILY_NOTES,
                        "index": index,
                        "legacy": True,
                    })
                continue
            if row.get("id") == entry_id:
                found.append({
                    **_entry_view(
                        entry_id, day, row.get("t", ""), row.get("author", "user"),
                        ts=row.get("ts"), done=bool(row.get("done", False)),
                        done_by=row.get("done_by"), done_at=row.get("done_at"),
                    ),
                    "doc_key": DAILY_NOTES,
                    "index": index,
                    "legacy": False,
                })
    for index, row in enumerate(anniversaries):
        if isinstance(row, dict) and row.get("id", _legacy_anniversary_id(row)) == entry_id:
            found.append({
                **_entry_view(
                    entry_id, row.get("date", ""), row.get("name", ""), row.get("author", "user"),
                    yearly=bool(row.get("yearly", False)), ts=row.get("ts"),
                ),
                "doc_key": DAILY_ANNIVERSARIES,
                "index": index,
                "legacy": not bool(row.get("id")),
            })
    return found


def _reserve_model_write(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_calendar_model_write_usage ("
        "local_day TEXT PRIMARY KEY, writes INTEGER NOT NULL CHECK (writes >= 0))"
    )
    local_day = now.astimezone(_TIME_ZONE).date().isoformat()
    row = conn.execute(
        "SELECT writes FROM daily_calendar_model_write_usage WHERE local_day = ?",
        (local_day,),
    ).fetchone()
    writes = int(row[0]) if row else 0
    if writes >= MODEL_WRITE_LIMIT:
        raise CalendarToolError("daily model calendar write limit reached (10 per Asia/Shanghai day)")
    conn.execute(
        "INSERT INTO daily_calendar_model_write_usage (local_day, writes) VALUES (?, 1) "
        "ON CONFLICT(local_day) DO UPDATE SET writes = writes + 1",
        (local_day,),
    )


def _connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    path = os.fspath(db_path)
    if not os.path.isfile(path):
        raise CalendarToolError("daily calendar database is unavailable")
    try:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
    except sqlite3.Error as exc:
        raise CalendarToolError("daily calendar database is unavailable") from exc


def _handle_add_note(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    day = args.get("date")
    if not _valid_date(day):
        raise CalendarToolError("date must be a real YYYY-MM-DD date")
    text = _arg_text(args.get("text"), "text", 500)
    data, updated_at = _doc_data(conn, DAILY_NOTES, {})
    entry_id = _new_id(conn)
    ts = int(now.timestamp() * 1000)
    stored = {"id": entry_id, "t": text, "author": "model", "ts": ts, "done": False}
    data.setdefault(day, []).append(stored)
    _reserve_model_write(conn, now)
    _save_document(conn, DAILY_NOTES, data, updated_at, now)
    persisted, _ = _doc_data(conn, DAILY_NOTES, {})
    matches = [item for item in persisted.get(day, []) if isinstance(item, dict) and item.get("id") == entry_id]
    if len(matches) != 1 or matches[0] != stored:
        raise CalendarToolError("calendar add_note read-back did not match; transaction rolled back")
    return _entry_view(entry_id, day, matches[0]["t"], matches[0]["author"], ts=matches[0]["ts"], done=matches[0]["done"])


def _handle_add_anniversary(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    day = args.get("date")
    if not _valid_date(day):
        raise CalendarToolError("date must be a real YYYY-MM-DD date")
    name = _arg_text(args.get("name"), "name", 120)
    yearly = args.get("yearly")
    if not isinstance(yearly, bool):
        raise CalendarToolError("yearly must be a boolean")
    data, updated_at = _doc_data(conn, DAILY_ANNIVERSARIES, [])
    entry_id = _new_id(conn)
    stored = {
        "date": day, "name": name, "yearly": yearly,
        "id": entry_id, "author": "model", "ts": int(now.timestamp() * 1000),
    }
    data.append(stored)
    _reserve_model_write(conn, now)
    _save_document(conn, DAILY_ANNIVERSARIES, data, updated_at, now)
    persisted, _ = _doc_data(conn, DAILY_ANNIVERSARIES, [])
    matches = [item for item in persisted if isinstance(item, dict) and item.get("id") == entry_id]
    if len(matches) != 1 or matches[0] != stored:
        raise CalendarToolError("calendar add_anniversary read-back did not match; transaction rolled back")
    return _entry_view(entry_id, day, matches[0]["name"], matches[0]["author"], yearly=matches[0]["yearly"], ts=matches[0]["ts"])


def _handle_remove(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    entry_id = _arg_text(args.get("id"), "id", 128)
    found = _find_entries(conn, entry_id)
    if not found:
        raise CalendarToolError("calendar entry not found")
    if len(found) != 1:
        raise CalendarToolError("calendar id is ambiguous; nothing was removed")
    entry = found[0]
    if entry["author"] != "model":
        raise CalendarToolError("calendar_remove can remove only model-authored entries")
    key = entry["doc_key"]
    default = {} if key == DAILY_NOTES else []
    data, updated_at = _doc_data(conn, key, default)
    data[entry["date"]].pop(entry["index"]) if key == DAILY_NOTES else data.pop(entry["index"])
    if key == DAILY_NOTES and not data.get(entry["date"]):
        data.pop(entry["date"], None)
    _save_document(conn, key, data, updated_at, now)
    remaining = _find_entries(conn, entry_id)
    if remaining:
        raise CalendarToolError("calendar_remove read-back found the entry; transaction rolled back")
    result = {key: value for key, value in entry.items() if key not in ("doc_key", "index", "legacy")}
    result["removed"] = True
    return result


def _handle_set_done(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    entry_id = _arg_text(args.get("id"), "id", 128)
    done = args.get("done")
    if not isinstance(done, bool):
        raise CalendarToolError("done must be a boolean")
    found = _find_entries(conn, entry_id)
    if not found:
        raise CalendarToolError("calendar entry not found")
    if len(found) != 1:
        raise CalendarToolError("calendar id is ambiguous; nothing was updated")
    entry = found[0]
    if entry["doc_key"] != DAILY_NOTES:
        raise CalendarToolError("calendar_set_done applies only to note items")
    data, updated_at = _doc_data(conn, DAILY_NOTES, {})
    old = data[entry["date"]][entry["index"]]
    if isinstance(old, str):
        stored = {
            "id": entry_id,
            "t": old,
            "legacy_text": old,
            "author": "user",
            "ts": int(now.timestamp() * 1000),
        }
    else:
        stored = dict(old)
    stored["done"] = done
    stored["done_by"] = "model"
    stored["done_at"] = _utc_text(now)
    data[entry["date"]][entry["index"]] = stored
    _save_document(conn, DAILY_NOTES, data, updated_at, now)
    persisted, _ = _doc_data(conn, DAILY_NOTES, {})
    matches = [item for item in persisted.get(entry["date"], []) if isinstance(item, dict) and item.get("id") == entry_id]
    if len(matches) != 1 or matches[0] != stored:
        raise CalendarToolError("calendar_set_done read-back did not match; transaction rolled back")
    return _entry_view(
        entry_id, entry["date"], matches[0]["t"], matches[0].get("author", "user"),
        ts=matches[0].get("ts"), done=matches[0]["done"],
        done_by=matches[0]["done_by"], done_at=matches[0]["done_at"],
    )


def _handle_list(conn: sqlite3.Connection, args: dict[str, Any], now: datetime) -> dict[str, Any]:
    today = now.astimezone(_TIME_ZONE).date()
    start_value = args.get("from", today.isoformat())
    end_value = args.get("to", (today + timedelta(days=7)).isoformat())
    if not _valid_date(start_value) or not _valid_date(end_value):
        raise CalendarToolError("from and to must be real YYYY-MM-DD dates")
    start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
    if end < start:
        raise CalendarToolError("to must be on or after from")
    if (end - start).days > 366:
        raise CalendarToolError("calendar_list range cannot exceed 367 days")
    notes, _ = _doc_data(conn, DAILY_NOTES, {})
    anniversaries, _ = _doc_data(conn, DAILY_ANNIVERSARIES, [])
    items: list[dict[str, Any]] = []
    for day, rows in notes.items():
        if not start_value <= day <= end_value:
            continue
        for row in rows:
            if isinstance(row, str):
                items.append(_entry_view(legacy_note_id(day, row), day, row, "user", done=False))
            else:
                items.append(_entry_view(
                    row["id"], day, row["t"], row.get("author", "user"),
                    ts=row.get("ts"), done=bool(row.get("done", False)),
                ))
    for row in anniversaries:
        if start_value <= row["date"] <= end_value:
            items.append(_entry_view(
                row.get("id") or _legacy_anniversary_id(row),
                row["date"], row["name"], row.get("author", "user"),
                yearly=bool(row.get("yearly", False)), ts=row.get("ts"),
            ))
    items.sort(key=lambda item: (item["date"], item["id"]))
    return {"from": start_value, "to": end_value, "items": items}


def calendar_tool_call(
    db_path: str | os.PathLike[str],
    operation: str,
    arguments: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run a model calendar operation; mutations and read-back share one transaction."""
    if not isinstance(arguments, dict):
        raise CalendarToolError("tool arguments must be an object")
    if operation not in {
        "calendar_list", "calendar_add_note", "calendar_add_anniversary",
        "calendar_remove", "calendar_set_done",
    }:
        raise CalendarToolError("unsupported calendar operation")
    current = _now(now)
    conn = _connect(db_path)
    try:
        if operation == "calendar_list":
            conn.execute("BEGIN")
            result = _handle_list(conn, arguments, current)
            conn.rollback()
            return result
        conn.execute("BEGIN IMMEDIATE")
        if operation == "calendar_add_note":
            result = _handle_add_note(conn, arguments, current)
        elif operation == "calendar_add_anniversary":
            result = _handle_add_anniversary(conn, arguments, current)
        elif operation == "calendar_remove":
            result = _handle_remove(conn, arguments, current)
        else:
            result = _handle_set_done(conn, arguments, current)
        conn.commit()
        return result
    except CalendarToolError:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise CalendarToolError(f"calendar storage error ({type(exc).__name__})") from exc
    finally:
        conn.close()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = calendar_tool_call(
            os.environ.get("RELAY_DB_PATH", DEFAULT_DB_PATH),
            payload.get("operation") if isinstance(payload, dict) else "",
            payload.get("arguments") if isinstance(payload, dict) else None,
        )
        response = {"ok": True, "result": result}
    except (CalendarToolError, json.JSONDecodeError) as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception as exc:  # Keep internal paths and document contents out of tool errors.
        response = {"ok": False, "error": f"calendar operation failed ({type(exc).__name__})"}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
