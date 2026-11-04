"""Compact activity metadata and safe agent-event maintenance helpers.

The raw Agent SDK envelopes remain authoritative in ``agent_events``.  This
module only derives a bounded list of action labels for message history, so a
history render does not need to fetch every run just to decide whether a
message has a thought drawer.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable


AGENT_ACTIVITY_MAX = 20
AGENT_RETENTION_DAYS = 30
DEFAULT_BACKFILL_START = "2026-09-08T00:00:00+00:00"


def run_id_for_stream(stream_id: str) -> str:
    value = str(stream_id or "").strip()
    return value if value.startswith("sdk:") else f"sdk:{value}"


def stream_id_for_run(run_id: str) -> str:
    return str(run_id or "").strip()[4:] if str(run_id or "").startswith("sdk:") else str(run_id or "").strip()


def normalize_tool_name(name: object) -> str:
    value = str(name or "").strip()
    if "__" in value:
        value = value.rsplit("__", 1)[-1].strip()
    return value or "tool"


def _event_label(event: dict) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "block_start":
        kind = str(event.get("kind") or "")
        if kind == "thinking":
            return "thinking"
        if kind == "tool":
            tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
            return normalize_tool_name(tool.get("name") or event.get("name"))
    return ""


def activity_from_events(events: Iterable[dict]) -> list[str]:
    """Return the same compact, consecutive-deduped labels used by live UI."""
    labels: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        label = _event_label(event)
        if not label or (labels and labels[-1] == label):
            continue
        labels.append(label)
        if len(labels) >= AGENT_ACTIVITY_MAX:
            break
    return labels


def _payload_events(rows: Iterable[sqlite3.Row | tuple]) -> list[dict]:
    events: list[dict] = []
    for row in rows:
        raw = row[0] if not isinstance(row, sqlite3.Row) else row["payload"]
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def activity_for_run(conn: sqlite3.Connection, run_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT payload FROM agent_events WHERE run_id = ? ORDER BY sequence ASC",
        (run_id,),
    ).fetchall()
    return activity_from_events(_payload_events(rows))


def _message_stream_rows(conn: sqlite3.Connection, stream_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, meta FROM messages "
        "WHERE direction = 'out' AND json_extract(meta, '$.stream_id') = ? "
        "ORDER BY id ASC",
        (stream_id,),
    ).fetchall()


def _meta(raw: object) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def persist_activity_for_stream(
    conn: sqlite3.Connection, stream_id: str, activity: list[str]
) -> int:
    """Attach activity to all matching AI rows; never deletes or rewrites content."""
    if not activity:
        return 0
    updated = 0
    for row in _message_stream_rows(conn, stream_id):
        meta = _meta(row["meta"])
        if meta.get("activity") == activity:
            continue
        meta["activity"] = list(activity)
        conn.execute(
            "UPDATE messages SET meta = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False, separators=(",", ":")), row["id"]),
        )
        updated += 1
    return updated


def backfill_activity(
    conn: sqlite3.Connection, start: str = DEFAULT_BACKFILL_START, apply: bool = False
) -> dict:
    rows = conn.execute(
        "SELECT id, ts, meta FROM messages "
        "WHERE direction = 'out' AND ts >= ? "
        "AND json_extract(meta, '$.stream_id') IS NOT NULL "
        "ORDER BY id ASC",
        (start,),
    ).fetchall()
    by_stream: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        stream_id = str(_meta(row["meta"]).get("stream_id") or "").strip()
        if stream_id:
            by_stream.setdefault(stream_id, []).append(row)

    report = {
        "start": start,
        "mode": "apply" if apply else "dry-run",
        "streams_scanned": len(by_stream),
        "runs_with_events": 0,
        "runs_without_events": 0,
        "messages_scanned": len(rows),
        "messages_missing_activity": 0,
        "messages_updated": 0,
    }
    for stream_id, message_rows in by_stream.items():
        activity = activity_for_run(conn, run_id_for_stream(stream_id))
        if not activity:
            report["runs_without_events"] += 1
            continue
        report["runs_with_events"] += 1
        missing = sum(1 for row in message_rows if not isinstance(_meta(row["meta"]).get("activity"), list))
        report["messages_missing_activity"] += missing
        if apply and missing:
            report["messages_updated"] += persist_activity_for_stream(conn, stream_id, activity)
    if apply:
        conn.commit()
    return report


def _retention_candidates(conn: sqlite3.Connection, cutoff: str) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, COUNT(*) AS row_count, MIN(created_at) AS oldest, MAX(created_at) AS newest "
        "FROM agent_events WHERE created_at < ? GROUP BY run_id ORDER BY run_id",
        (cutoff,),
    ).fetchall()
    candidates: list[dict] = []
    for row in rows:
        run_id = str(row["run_id"])
        stream_id = stream_id_for_run(run_id)
        messages = _message_stream_rows(conn, stream_id)
        activity = activity_for_run(conn, run_id)
        complete = bool(messages) and bool(activity) and all(
            _meta(message["meta"]).get("activity") == activity for message in messages
        )
        candidates.append({
            "run_id": run_id,
            "stream_id": stream_id,
            "rows": int(row["row_count"]),
            "oldest": row["oldest"],
            "newest": row["newest"],
            "messages": len(messages),
            "activity": activity,
            "safe": complete,
            "reason": "ready" if complete else ("no_message" if not messages else "activity_missing"),
        })
    return candidates


def retention_report(
    conn: sqlite3.Connection, cutoff: str | None = None, apply: bool = False
) -> dict:
    if cutoff is None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=AGENT_RETENTION_DAYS)).isoformat()
    candidates = _retention_candidates(conn, cutoff)
    safe = [item for item in candidates if item["safe"]]
    blocked = [item for item in candidates if not item["safe"]]
    deleted = 0
    if apply and safe:
        run_ids = [item["run_id"] for item in safe]
        marks = ",".join("?" for _ in run_ids)
        cur = conn.execute(
            f"DELETE FROM agent_events WHERE created_at < ? AND run_id IN ({marks})",
            (cutoff, *run_ids),
        )
        deleted = int(cur.rowcount or 0)
        conn.commit()
    return {
        "cutoff": cutoff,
        "retention_days": AGENT_RETENTION_DAYS,
        "mode": "apply" if apply else "dry-run",
        "candidate_runs": len(candidates),
        "candidate_rows": sum(item["rows"] for item in candidates),
        "safe_runs": len(safe),
        "safe_rows": sum(item["rows"] for item in safe),
        "blocked_runs": len(blocked),
        "blocked_rows": sum(item["rows"] for item in blocked),
        "deleted_rows": deleted,
        "blocked_reasons": {
            reason: sum(1 for item in blocked if item["reason"] == reason)
            for reason in sorted({item["reason"] for item in blocked})
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Valelin agent_events backfill and retention maintenance")
    parser.add_argument("--db", default=os.environ.get("RELAY_DB_PATH", "/data/relay.db"))
    parser.add_argument("--backfill", action="store_true", help="report or apply message activity backfill")
    parser.add_argument("--backfill-start", default=DEFAULT_BACKFILL_START)
    parser.add_argument("--apply-backfill", action="store_true")
    parser.add_argument("--retention", action="store_true", help="report or apply 30-day event retention")
    parser.add_argument("--apply-retention", action="store_true", help="delete only safe old agent_events rows")
    parser.add_argument("--cutoff", default=None)
    args = parser.parse_args()
    if args.apply_backfill:
        args.backfill = True
    if args.apply_retention:
        args.retention = True
    if not args.backfill and not args.retention:
        args.backfill = args.retention = True

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        result = {}
        if args.backfill:
            result["backfill"] = backfill_activity(conn, args.backfill_start, args.apply_backfill)
        if args.retention:
            result["retention"] = retention_report(conn, args.cutoff, args.apply_retention)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
