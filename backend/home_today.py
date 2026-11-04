"""Read-only aggregation for the authenticated HOME Today card."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


_TZ = ZoneInfo("Asia/Shanghai")
_FILE_MARK_RE = re.compile(r"\[file:\s*([^\]\r\n]+)\]", re.IGNORECASE)
_COMMAND_OUTCOMES = {"completed": "你做到了", "timed_out": "没等到你", "canceled": "这张划掉了"}


def _local_datetime(value: Any, *, milliseconds: bool = False) -> datetime | None:
    if milliseconds and isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value) / 1000, timezone.utc).astimezone(_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (OverflowError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(_TZ)


def _one_line(value: Any, limit: int = 112) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _inside_today(value: Any, today: date) -> datetime | None:
    local = _local_datetime(value)
    return local if local and local.date() == today else None


def _event(items: list[dict[str, Any]], kind: str, when: datetime | None, summary: Any) -> None:
    text = _one_line(summary)
    if not when or not text:
        return
    items.append({
        "type": kind,
        "ts": when.isoformat(timespec="milliseconds"),
        "time": when.strftime("%H:%M"),
        "summary": text,
        "_sort": when.timestamp(),
    })


def _document_data(raw: Any, fallback: Any) -> Any:
    try:
        data = json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback
    return data


def _file_name(marker_body: str) -> str:
    source, separator, display_name = marker_body.partition("|")
    name = display_name.strip() if separator and display_name.strip() else source.strip()
    # File markers contain server paths. HOME needs only a safe display name.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    return _one_line(name, 80)


def home_today_payload(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    """Return today's Vale-authored HOME events, newest first, without writes."""
    current = now or datetime.now(_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_TZ)
    local_now = current.astimezone(_TZ)
    today = local_now.date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=_TZ)
    next_day = day_start + timedelta(days=1)
    start_utc = day_start.astimezone(timezone.utc).isoformat(timespec="microseconds")
    end_utc = next_day.astimezone(timezone.utc).isoformat(timespec="microseconds")
    items: list[dict[str, Any]] = []

    command_rows = conn.execute(
        "SELECT text, started_at, finished_at, status FROM command_cards "
        "WHERE (julianday(started_at) >= julianday(?) AND julianday(started_at) < julianday(?)) "
        "OR (julianday(finished_at) >= julianday(?) AND julianday(finished_at) < julianday(?)) "
        "ORDER BY started_at DESC LIMIT 200",
        (start_utc, end_utc, start_utc, end_utc),
    ).fetchall()
    for row in command_rows:
        text = _one_line(row["text"], 88)
        started = _inside_today(row["started_at"], today)
        if started:
            _event(items, "command", started, f"给你下了张卡：{text}")
        finished = _inside_today(row["finished_at"], today)
        outcome = _COMMAND_OUTCOMES.get(str(row["status"] or ""))
        if finished and outcome:
            _event(items, "command_result", finished, f"{outcome}：{text}")

    ledger_rows = conn.execute(
        "SELECT settled_at, settle_note, action, reason, kind FROM ledger_entries "
        "WHERE status = 'settled' AND julianday(settled_at) >= julianday(?) "
        "AND julianday(settled_at) < julianday(?) ORDER BY settled_at DESC LIMIT 100",
        (start_utc, end_utc),
    ).fetchall()
    for row in ledger_rows:
        settled = _inside_today(row["settled_at"], today)
        detail = row["settle_note"] or row["action"] or row["reason"]
        label = "还你一笔" if row["kind"] == "reward" else "罚过了"
        _event(items, "ledger", settled, f"{label}：{detail}")

    daily_rows = conn.execute(
        "SELECT doc_key, data_json FROM daily_documents "
        "WHERE doc_key IN ('valelin_notes', 'valelin_anniv')"
    ).fetchall()
    for row in daily_rows:
        data = _document_data(row["data_json"], {} if row["doc_key"] == "valelin_notes" else [])
        if row["doc_key"] == "valelin_notes" and isinstance(data, dict):
            for entries in data.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    text = entry.get("t", "")
                    if entry.get("author") == "model":
                        created = _local_datetime(entry.get("ts"), milliseconds=True)
                        _event(items, "calendar", created if created and created.date() == today else None,
                               f"替你记下：{text}")
                    if entry.get("done_by") == "model":
                        done_at = _inside_today(entry.get("done_at"), today)
                        _event(items, "calendar", done_at, f"划掉一件：{text}")
        elif row["doc_key"] == "valelin_anniv" and isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict) or entry.get("author") != "model":
                    continue
                created = _local_datetime(entry.get("ts"), milliseconds=True)
                when = created if created and created.date() == today else None
                _event(items, "calendar", when, f"记住这天：{entry.get('name', '')}")

    message_rows = conn.execute(
        "SELECT ts, text FROM messages WHERE direction = 'out' AND kind = 'reply' "
        "AND julianday(ts) >= julianday(?) AND julianday(ts) < julianday(?) "
        "AND instr(text, '[file:') > 0 ORDER BY ts DESC, id DESC LIMIT 200",
        (start_utc, end_utc),
    ).fetchall()
    for row in message_rows:
        when = _inside_today(row["ts"], today)
        if not when:
            continue
        for match in _FILE_MARK_RE.finditer(str(row["text"] or "")):
            name = _file_name(match.group(1))
            if name:
                _event(items, "file", when, f"递给你：{name}")

    keep_rows = conn.execute(
        "SELECT saved_at, text_snapshot, media_json FROM keep_items WHERE saved_by = 'Vale' "
        "AND julianday(saved_at) >= julianday(?) AND julianday(saved_at) < julianday(?) "
        "ORDER BY saved_at DESC, id DESC LIMIT 100",
        (start_utc, end_utc),
    ).fetchall()
    for row in keep_rows:
        when = _inside_today(row["saved_at"], today)
        if not when:
            continue
        text = _one_line(row["text_snapshot"], 88)
        media = _document_data(row["media_json"], {})
        media_items = media.get("items", []) if isinstance(media, dict) else []
        image_count = sum(1 for item in media_items if isinstance(item, dict) and item.get("kind") == "image")
        if text and image_count:
            summary = f"收下了：{text}"
        elif text:
            summary = f"收下这句：{text}"
        elif image_count:
            summary = f"收下了：{image_count} 张照片"
        else:
            summary = "收下了：一条"
        _event(items, "keep", when, summary)

    items.sort(key=lambda item: float(item["_sort"]), reverse=True)
    for item in items:
        item.pop("_sort", None)
    return {"date": today.isoformat(), "timezone": "Asia/Shanghai", "items": items[:250]}
