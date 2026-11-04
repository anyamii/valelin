"""WORDS activity rows in the existing relay message timeline."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

from backend import keeps


MERGE_SECONDS = 180


def _short(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _item_label(item: dict) -> str:
    return _short(keeps.preview_text(item), 20) or "一条收藏"


def record(
    conn: sqlite3.Connection, *, action: str, actor: str, item: dict,
    note: str = "", replying: bool = False,
    turn_id: int | None = None, after_seg: int | None = None,
) -> dict:
    """Persist one inert event; consecutive saves by one actor share one row."""
    if action not in {"save", "note"} or actor not in {"her", "Vale"}:
        raise ValueError("invalid keep activity")
    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    keep_id = int(item["id"])
    name = "Lin" if actor == "her" else "Vale"
    label = _item_label(item)
    if action == "save":
        text = f"{name} 收了一段：{label}" if actor == "her" else f"Vale 收了你一条：{label}"
    else:
        head = "Vale 回了你的注" if actor == "Vale" and replying else f"{name} 在「{label}」下写了"
        text = f"{head}：{_short(note, 40)}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        last = conn.execute("SELECT id, ts, kind, meta FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        if action == "save" and last is not None and last["kind"] == "keep_activity":
            try:
                previous = json.loads(last["meta"] or "{}")
            except (TypeError, ValueError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            activity = previous.get("keep_activity") or {}
            try:
                age = (now - datetime.fromisoformat(activity.get("last_at", ""))).total_seconds()
            except (TypeError, ValueError):
                age = MERGE_SECONDS + 1
            same_position = actor == "her" or (
                turn_id is not None and activity.get("turn_id") == turn_id
                and activity.get("after_seg") == after_seg
            )
            if activity.get("action") == "save" and activity.get("actor") == actor and same_position and 0 <= age <= MERGE_SECONDS:
                ids = [*activity.get("keep_ids", []), keep_id]
                activity.update({"keep_id": keep_id, "keep_ids": ids, "count": len(ids), "last_at": ts})
                previous["keep_activity"] = activity
                text = f"{name} 收了 {len(ids)} 条" if actor == "her" else f"Vale 收了你 {len(ids)} 条"
                conn.execute("UPDATE messages SET ts=?, text=?, meta=? WHERE id=?",
                             (ts, text, json.dumps(previous, ensure_ascii=False), last["id"]))
                conn.commit()
                return {"id": last["id"], "ts": ts, "direction": "out", "kind": "keep_activity", "text": text, "meta": previous}
        activity = {"action": action, "actor": actor, "keep_id": keep_id,
                    "keep_ids": [keep_id], "count": 1, "last_at": ts}
        if turn_id is not None:
            activity["turn_id"] = turn_id
        if after_seg is not None:
            activity["after_seg"] = after_seg
        meta = {"keep_activity": activity}
        cursor = conn.execute(
            "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?, 'out', 'keep_activity', ?, ?)",
            (ts, text, json.dumps(meta, ensure_ascii=False)),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "ts": ts, "direction": "out", "kind": "keep_activity", "text": text, "meta": meta}
    except Exception:
        conn.rollback()
        raise
