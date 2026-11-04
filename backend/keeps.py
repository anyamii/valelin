"""Unified WORDS / ALBUM storage and legacy import helpers.

This module deliberately owns only additive relay-side storage. Existing gateway
favorites and relay voice snapshots remain read-only migration sources.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from backend.voice_audio_tags import strip_voice_audio_tags


_TAG_RE = re.compile(r"^[^\s#]{1,32}$")
_SAFE_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def plain_text_preview(value: object) -> str:
    """Mirror the WORDS keepPlainText summary cleanup for model-bound shares."""
    text = str(value or "")
    text = re.sub(
        r"^\[voice(?:\s*·\s*(?:\d+:\d+))?(?:\s*·\s*[a-z]+)?(?:\|[^\]]*)?\]\s*",
        "",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    text = re.sub(r"</?voice>", "", text)
    text = re.sub(r"\[br\]", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"^\[[a-z]{2}\]\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[(?:zh|en|ja)\]\s*", "", text)
    text = re.sub(r"(?:^|\n)\s*\[audio\][^\n]*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"`([^`\n]*)`", r"\1", text)
    text = re.sub(r"\[([^\]\n]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*([\s\S]*?)\*\*", r"\1", text)
    text = re.sub(r"__([\s\S]*?)__", r"\1", text)
    text = re.sub(r"~~([^~\n]+)~~", r"\1", text)
    text = re.sub(r"\*([^*\n]+)\*", r"\1", text)
    text = re.sub(r"_([^_\n]+)_", r"\1", text)
    text = re.sub(
        r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]+|[-*+][ \t]+|\d+\.[ \t]+)",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(r"[\*_~`]", "", text)
    text = re.sub(r":[a-z0-9_]{1,32}:", " ", text, flags=re.IGNORECASE)
    return _norm(text)


def _preview_segments(item: dict) -> list[dict]:
    media = item.get("media") if isinstance(item.get("media"), dict) else {}
    primary = media.get("segments") if isinstance(media.get("segments"), list) else []
    legacy_voice = media.get("legacy_voice") if isinstance(media.get("legacy_voice"), dict) else {}
    legacy = legacy_voice.get("segments") if isinstance(legacy_voice.get("segments"), list) else []
    raw_segments = [segment for segment in [*primary, *legacy] if isinstance(segment, dict)]
    has_seq = True
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for raw in raw_segments:
        seg = dict(raw)
        kind = str(seg.get("kind") or "text")
        seq = None
        if seg.get("seq") is not None:
            try:
                number = float(seg.get("seq"))
                if math.isfinite(number):
                    seq = int(number) if number.is_integer() else number
            except (TypeError, ValueError):
                pass
        if seq is None:
            has_seq = False
        text = re.sub(r"\s+", " ", str(seg.get("text") or "")).strip()
        url = re.split(r"[?#]", str(seg.get("url") or ""))[0]
        basename = url.rsplit("/", 1)[-1]
        url_key = "" if kind == "voice" else basename
        key = f"{kind}|{seq}" if seq is not None else f"{kind}|{text}|{url_key}"
        old = seen.get(key)
        if old is not None:
            for field in ("url", "text", "zh", "duration"):
                if not old.get(field) and seg.get(field):
                    old[field] = seg[field]
            continue
        seg["kind"] = kind
        seg["seq"] = seq
        seen[key] = seg
        out.append(seg)
    if has_seq:
        out.sort(key=lambda seg: float(seg["seq"]))
    return out


def preview_text(item: dict) -> str:
    """Return the same cleaned, de-duplicated text WORDS shows in its summary."""
    segments = _preview_segments(item)
    if segments:
        parts = [
            plain_text_preview(seg.get("text"))
            for seg in segments
            if seg.get("kind") not in {"sticker", "file"}
        ]
        return _norm(" ".join(part for part in parts if part))
    return plain_text_preview(item.get("text"))


def display_preview_text(item: dict) -> str:
    """Build keep-card copy while hiding audio tags only in voice content."""
    segments = _preview_segments(item)
    if segments:
        parts = []
        for seg in segments:
            if seg.get("kind") in {"sticker", "file"}:
                continue
            text = str(seg.get("text") or "")
            if seg.get("kind") == "voice":
                text = strip_voice_audio_tags(text)
            clean = plain_text_preview(text)
            if clean:
                parts.append(clean)
        return _norm(" ".join(parts))

    text = str(item.get("text") or "")
    # Legacy voice blocks lack segment metadata. Scope cleanup to explicit wrappers.
    text = re.sub(
        r"(?is)(<voice>)(.*?)(</voice>)",
        lambda match: match.group(1) + strip_voice_audio_tags(match.group(2)) + match.group(3),
        text,
    )
    blocks = re.split(r"(\r?\n[ \t]*\r?\n)", text)
    voice_header = re.compile(
        r"^[ \t]*\[voice(?:\s*·\s*(?:\d+:\d+))?(?:\s*·\s*[a-z]+)?(?:\|[^\]\r\n]*)?\]",
        re.IGNORECASE,
    )
    for index in range(0, len(blocks), 2):
        match = voice_header.match(blocks[index])
        if match:
            blocks[index] = blocks[index][:match.end()] + strip_voice_audio_tags(blocks[index][match.end():])
    return plain_text_preview("".join(blocks))


def share_context_text(item: dict, text: str) -> str:
    """Describe a WORDS share in the clean, human-readable form shown in WORDS."""
    zone = timezone(timedelta(hours=8))
    day_label = ""
    try:
        message_at = datetime.fromisoformat(str(item.get("message_at") or "").replace("Z", "+00:00"))
        if message_at.tzinfo is None:
            message_at = message_at.replace(tzinfo=zone)
        else:
            message_at = message_at.astimezone(zone)
        day_label = f"{message_at.month}月{message_at.day}日"
    except (TypeError, ValueError):
        pass
    saver = {"her": "她", "Vale": "我"}.get(str(item.get("saved_by") or ""), "她")
    label = (
        f"她从 WORDS 分享了一条{saver}收藏的、{day_label}的话"
        if day_label
        else f"她从 WORDS 分享了一条{saver}收藏的话"
    )
    return f"{label}：\n{text}"


def _json_load(raw: object, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the unified model without changing any existing table."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS keep_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key      TEXT NOT NULL UNIQUE,
            source_kind     TEXT NOT NULL DEFAULT 'chat',
            saved_by        TEXT NOT NULL,
            speaker         TEXT NOT NULL DEFAULT '',
            message_id      INTEGER,
            turn_id         INTEGER,
            message_at      TEXT NOT NULL DEFAULT '',
            saved_at        TEXT NOT NULL,
            text_snapshot   TEXT NOT NULL DEFAULT '',
            quote_snapshot  TEXT NOT NULL DEFAULT '',
            user_text       TEXT NOT NULL DEFAULT '',
            assistant_text  TEXT NOT NULL DEFAULT '',
            media_json      TEXT NOT NULL DEFAULT '{}',
            source_json     TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_keep_items_saved_at ON keep_items(saved_at DESC);
        CREATE INDEX IF NOT EXISTS idx_keep_items_owner ON keep_items(saved_by, saved_at DESC);
        CREATE INDEX IF NOT EXISTS idx_keep_items_message ON keep_items(message_id);
        CREATE TABLE IF NOT EXISTS keep_tags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS keep_item_tags (
            keep_id INTEGER NOT NULL REFERENCES keep_items(id) ON DELETE CASCADE,
            tag_id  INTEGER NOT NULL REFERENCES keep_tags(id) ON DELETE CASCADE,
            PRIMARY KEY (keep_id, tag_id)
        );
        CREATE TABLE IF NOT EXISTS keep_notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            keep_id    INTEGER NOT NULL REFERENCES keep_items(id) ON DELETE CASCADE,
            author     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            content    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_keep_notes_item ON keep_notes(keep_id, created_at, id);
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(keep_items)")}
    if "seg" not in columns:
        conn.execute("ALTER TABLE keep_items ADD COLUMN seg INTEGER")
    conn.commit()


def _copy_known_media(
    *,
    source: Path,
    name: str,
    media_kind: str,
    keep_id_key: str,
    media_dir: Path,
    public_path: str,
    extra: dict | None = None,
) -> dict | None:
    """Copy a known relay-owned file into the non-expiring keep directory."""
    try:
        source = source.resolve(strict=True)
        if not source.is_file():
            return None
        ext = source.suffix.lower()
        if not _SAFE_EXT_RE.fullmatch(ext):
            ext = ".bin"
        digest = hashlib.sha256((keep_id_key + "\0" + name).encode("utf-8")).hexdigest()[:20]
        filename = f"keep-{digest}{ext}"
        media_dir.mkdir(parents=True, exist_ok=True)
        target = media_dir / filename
        if not target.exists():
            shutil.copyfile(source, target)
        out = {
            "kind": media_kind,
            "name": Path(name).name or filename,
            "url": f"{public_path}/{filename}",
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        }
        if extra:
            out.update(extra)
        return out
    except (OSError, RuntimeError, ValueError):
        return None


def _message_media(
    *,
    message_id: int,
    meta: dict,
    source_key: str,
    upload_dir: Path,
    voice_dir: Path,
    sticker_dir: Path,
    keep_media_dir: Path,
    media_public_path: str,
    only_image_id: str | None = None,
) -> dict:
    items: list[dict] = []
    copied_ids: set[str] = set()
    for attachment in meta.get("attachments", []) if isinstance(meta.get("attachments"), list) else []:
        if not isinstance(attachment, dict) or attachment.get("kind") != "image":
            continue
        url = str(attachment.get("url") or attachment.get("local_path") or "")
        image_id = str(attachment.get("image_id") or attachment.get("id") or Path(urlparse(url).path).name)
        basename = Path(urlparse(url).path).name
        if only_image_id and only_image_id not in {image_id, basename, Path(basename).stem}:
            continue
        if not basename or basename in {".", ".."}:
            continue
        saved = _copy_known_media(
            source=upload_dir / basename,
            name=str(attachment.get("name") or basename),
            media_kind="image",
            keep_id_key=source_key,
            media_dir=keep_media_dir,
            public_path=media_public_path,
            extra={"image_id": image_id, "message_id": message_id},
        )
        if saved:
            items.append(saved)
            copied_ids.add(image_id)
            copied_ids.add(basename)
            copied_ids.add(Path(basename).stem)

    segments = meta.get("segments") if isinstance(meta.get("segments"), list) else []
    safe_segments: list[dict] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        out = dict(segment)
        kind = str(segment.get("kind") or "")
        if kind == "sticker":
            sticker_id = str(segment.get("id") or segment.get("sticker_id") or "")
            if sticker_id and re.fullmatch(r"[a-z0-9_]{1,32}", sticker_id):
                saved = _copy_known_media(
                    source=sticker_dir / f"{sticker_id}.webp",
                    name=f"{sticker_id}.webp",
                    media_kind="sticker",
                    keep_id_key=source_key,
                    media_dir=keep_media_dir,
                    public_path=media_public_path,
                    extra={"sticker_id": sticker_id},
                )
                if saved:
                    items.append(saved)
                    out["url"] = saved["url"]
                    out["id"] = sticker_id
        elif kind == "voice":
            url = str(segment.get("url") or "")
            basename = Path(urlparse(url).path).name
            if basename and basename not in {".", ".."}:
                candidate = voice_dir / basename
                if not candidate.is_file():
                    candidate = voice_dir / "kept" / basename
                if not candidate.is_file():
                    candidate = upload_dir / basename
                saved = _copy_known_media(
                    source=candidate,
                    name=str(segment.get("name") or basename),
                    media_kind="voice",
                    keep_id_key=f"{source_key}:segment:{index}",
                    media_dir=keep_media_dir,
                    public_path=media_public_path,
                )
                if saved:
                    items.append(saved)
                    out["url"] = saved["url"]
                else:
                    out["url"] = None
        safe_segments.append(out)

    if only_image_id and not any(x.get("kind") == "image" for x in items):
        raise ValueError("image_id is not attached to this message or its original is unavailable")
    return {"items": items, "segments": safe_segments}


def message_segments(message: sqlite3.Row, meta: dict) -> list[dict]:
    """Expose the stored reply seq or the visible inbound bubble order."""
    stored = meta.get("segments") if isinstance(meta.get("segments"), list) else []
    if stored:
        return [s for s in stored if isinstance(s, dict) and isinstance(s.get("seq"), int)]
    text = str(message["text"] or "")
    if message["direction"] != "in":
        return [{"seq": 0, "kind": "text", "text": text}] if text.strip() else []
    attachments = meta.get("attachments") if isinstance(meta.get("attachments"), list) else []
    if message["kind"] == "voice":
        voice = next((a for a in attachments if isinstance(a, dict) and
                      (a.get("kind") == "audio" or str(a.get("mime") or "").startswith("audio/"))), None)
        if voice:
            visible_text = text.split("\n[audio]", 1)[0]
            return [{"seq": 0, "kind": "voice", "text": re.sub(r"^🎤\s*", "", visible_text).strip(),
                     "url": voice.get("url") or voice.get("local_path"),
                     "duration": voice.get("duration") or 0}]
    # The inbound split UI divides at blank lines; fenced code remains one bubble.
    fenced: list[str] = []
    def protect(match: re.Match) -> str:
        fenced.append(match.group(0))
        return f"\x00F{len(fenced) - 1}\x00"
    protected = re.sub(r"```[\s\S]*?```", protect, text)
    parts = [re.sub(r"\x00F(\d+)\x00", lambda m: fenced[int(m.group(1))], p.strip())
             for p in re.split(r"\n{2,}", protected) if p.strip()]
    segments = [{"seq": i, "kind": "sticker" if re.fullmatch(r":[a-z0-9_]{1,32}:", p) else "text", "text": p}
                for i, p in enumerate(parts)]
    if len(parts) > 1:
        segments.append({"seq": len(parts), "kind": "text", "text": text, "whole_bubble": True})
    return segments


def match_inbound_text(conn: sqlite3.Connection, text: str) -> tuple[int | None, int | None, list[dict]]:
    needle = str(text or "")
    if not needle or len(needle) > 500:
        raise ValueError("match_text must contain 1 to 500 characters")
    rows = conn.execute("SELECT id, ts, direction, kind, text, meta FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 200").fetchall()
    matches = []
    for row in rows:
        full_text = str(row["text"] or "")
        if needle not in full_text:
            continue
        meta = _json_load(row["meta"], {})
        matched_seg = None
        for segment in message_segments(row, meta if isinstance(meta, dict) else {}):
            if segment.get("whole_bubble"):
                continue
            body = str(segment.get("text") or "")
            if needle in body:
                matched_seg = int(segment["seq"])
                break
        matches.append({"id": int(row["id"]), "seg": matched_seg,
                        "time": row["ts"], "preview": full_text[:30]})
    if len(matches) == 1:
        return matches[0]["id"], matches[0]["seg"], []
    return None, None, matches


def _selected_context(conn: sqlite3.Connection, message: sqlite3.Row, meta: dict,
                      context: str, context_message_id: int | None, context_match_text: str) -> sqlite3.Row | None:
    """Resolve an explicit quote within the same conversation, before the saved row."""
    if not isinstance(context, str) or context not in {"auto", "none", "message"}:
        raise ValueError("context must be auto, none, or message")
    if not isinstance(context_match_text, str):
        raise ValueError("context_match_text must be a string")
    if context != "message":
        if context_message_id is not None or context_match_text:
            raise ValueError("context selectors require context=message")
        if context == "none":
            return None
        return conn.execute(
            "SELECT id, direction, text FROM messages WHERE id < ? AND direction != ? "
            "AND kind NOT IN ('thinking', 'call') AND trim(text) != '' ORDER BY id DESC LIMIT 1",
            (message["id"], message["direction"]),
        ).fetchone()
    if (context_message_id is not None) == bool(context_match_text):
        raise ValueError("provide exactly one context_message_id or context_match_text")
    if context_message_id is not None and (
        isinstance(context_message_id, bool) or not isinstance(context_message_id, int) or context_message_id <= 0
    ):
        raise ValueError("context_message_id must be a positive integer")
    if context_match_text and (not context_match_text.strip() or len(context_match_text) > 500):
        raise ValueError("context_match_text must contain 1 to 500 characters")
    # Import lazily: near_context itself uses this module for keep previews.
    from near_context import desktop_line_clause, is_desktop_session

    call_id = str(meta.get("call_session_id") or "")
    session_id = str(meta.get("api_session") or "")
    args: list[object] = [message["id"]]
    if call_id:
        scope = "json_extract(meta, '$.call_session_id') = ?"
        args.append(call_id)
    elif is_desktop_session(session_id):
        scope = desktop_line_clause()
    else:
        scope = desktop_line_clause() + " AND COALESCE(json_extract(meta, '$.api_session'), '') = ?"
        args.append(session_id)
    selector = "id = ?" if context_message_id is not None else "instr(text, ?) > 0"
    args.append(context_message_id if context_message_id is not None else context_match_text)
    rows = conn.execute(
        f"SELECT id, direction, text FROM messages WHERE id < ? AND {scope} AND {selector} "
        "AND direction IN ('in', 'out') AND kind NOT IN ('thinking', 'call', 'keep_activity') "
        "AND trim(text) != '' ORDER BY id DESC LIMIT 2", args,
    ).fetchall()
    if not rows:
        raise ValueError("context message not found before this message in the same conversation")
    if len(rows) != 1:
        raise ValueError("ambiguous context_match_text; use context_message_id")
    return rows[0]


def save_message(
    conn: sqlite3.Connection,
    *,
    message_id: int,
    saved_by: str,
    tags: list[str],
    upload_dir: Path,
    voice_dir: Path,
    sticker_dir: Path,
    keep_media_dir: Path,
    media_public_path: str,
    image_id: str | None = None,
    seg: int | None = None,
    require_inbound: bool = False,
    context: str = "auto",
    context_message_id: int | None = None,
    context_match_text: str = "",
) -> int:
    if saved_by not in {"her", "Vale"}:
        raise ValueError("saved_by must be her or Vale")
    message = conn.execute(
        "SELECT id, ts, direction, kind, text, meta FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not message:
        raise LookupError("message not found")
    if require_inbound and message["direction"] != "in":
        raise PermissionError("Vale can only save her messages")
    if seg is not None and (isinstance(seg, bool) or not isinstance(seg, int) or seg < 0):
        raise ValueError("seg must be a non-negative integer")
    if seg is not None and image_id:
        raise ValueError("seg and image_id cannot be combined")
    meta = _json_load(message["meta"], {})
    if not isinstance(meta, dict):
        meta = {}
    previous = _selected_context(conn, message, meta, context, context_message_id, context_match_text)
    source_key = f"chat:{saved_by}:{message_id}:{image_id or '*'}" + (f":seg:{seg}" if seg is not None else "")
    existing = conn.execute("SELECT id FROM keep_items WHERE source_key = ?", (source_key,)).fetchone()
    if existing:
        _replace_item_tags(conn, int(existing["id"]), tags)
        return int(existing["id"])
    selected = None
    if seg is not None:
        selected = next((s for s in message_segments(message, meta) if s["seq"] == seg), None)
        if selected is None:
            raise ValueError("seg is not a bubble in this message")
        meta = {"segments": [selected]}
    media = _message_media(
        message_id=int(message["id"]), meta=meta, source_key=source_key,
        upload_dir=upload_dir, voice_dir=voice_dir, sticker_dir=sticker_dir,
        keep_media_dir=keep_media_dir, media_public_path=media_public_path,
        only_image_id=image_id,
    )
    if selected and selected.get("kind") == "voice" and not any(
        item.get("kind") == "voice" for item in media["items"]
    ):
        raise ValueError("voice segment audio is unavailable")
    quote = str(previous["text"] or "") if previous else ""
    source = {"message_kind": message["kind"], "image_id": image_id}
    if context == "message":
        source.update(context_message_id=int(previous["id"]),
                      context_speaker="Vale" if previous["direction"] == "out" else "her")
    saved_at = _now_iso()
    cursor = conn.execute(
        "INSERT INTO keep_items (source_key, source_kind, saved_by, speaker, message_id, message_at, saved_at, text_snapshot, quote_snapshot, media_json, source_json, seg) "
        "VALUES (?, 'chat', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_key, saved_by, "Vale" if message["direction"] == "out" else "her",
         message_id, str(message["ts"] or ""), saved_at, str((selected.get("text") if selected else message["text"]) or ""), quote,
         json.dumps(media, ensure_ascii=False), json.dumps(source, ensure_ascii=False), seg),
    )
    keep_id = int(cursor.lastrowid)
    _replace_item_tags(conn, keep_id, tags)
    conn.commit()
    return keep_id


def _normalize_tags(tags: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in tags or []:
        tag = str(raw or "").strip().lstrip("#")
        if not tag or not _TAG_RE.fullmatch(tag):
            continue
        if tag not in out:
            out.append(tag)
    return out[:20]


def _replace_item_tags(conn: sqlite3.Connection, keep_id: int, tags: list[str] | None) -> list[str]:
    names = _normalize_tags(tags)
    conn.execute("DELETE FROM keep_item_tags WHERE keep_id = ?", (keep_id,))
    for name in names:
        conn.execute("INSERT OR IGNORE INTO keep_tags (name, created_at) VALUES (?, ?)", (name, _now_iso()))
        tag_id = conn.execute("SELECT id FROM keep_tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO keep_item_tags (keep_id, tag_id) VALUES (?, ?)", (keep_id, tag_id))
    return names


def set_item_tags(conn: sqlite3.Connection, keep_id: int, tags: list[str] | None) -> list[str]:
    if not conn.execute("SELECT 1 FROM keep_items WHERE id = ?", (keep_id,)).fetchone():
        raise LookupError("keep not found")
    names = _replace_item_tags(conn, keep_id, tags)
    conn.commit()
    return names


def clear_item_context(conn: sqlite3.Connection, keep_id: int) -> None:
    """Clear only the quote; both participants can correct a shared keep."""
    cursor = conn.execute("UPDATE keep_items SET quote_snapshot = '' WHERE id = ?", (keep_id,))
    if not cursor.rowcount:
        raise LookupError("keep not found")
    conn.commit()


def item_payload(conn: sqlite3.Connection, keep_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM keep_items WHERE id = ?", (keep_id,)).fetchone()
    if not row:
        return None
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM keep_tags t JOIN keep_item_tags it ON it.tag_id=t.id "
        "WHERE it.keep_id=? ORDER BY t.name COLLATE NOCASE", (keep_id,)
    ).fetchall()]
    notes = [dict(r) for r in conn.execute(
        "SELECT id, author, created_at, content FROM keep_notes WHERE keep_id=? ORDER BY created_at, id",
        (keep_id,),
    ).fetchall()]
    media = _json_load(row["media_json"], {})
    if not isinstance(media, dict):
        media = {"items": [], "segments": []}
    return {
        "id": int(row["id"]), "saved_by": row["saved_by"], "speaker": row["speaker"],
        "message_id": row["message_id"], "turn_id": row["turn_id"],
        "seg": row["seg"], "granularity": "segment" if row["seg"] is not None else "message",
        "image_id": _json_load(row["source_json"], {}).get("image_id"),
        "message_at": row["message_at"], "saved_at": row["saved_at"],
        "text": row["text_snapshot"], "quote": row["quote_snapshot"],
        "quote_speaker": _json_load(row["source_json"], {}).get("context_speaker"),
        "user_text": row["user_text"], "assistant_text": row["assistant_text"],
        "media": media, "tags": tags, "notes": notes, "note_count": len(notes),
    }


def list_items(
    conn: sqlite3.Connection, *, view: str, source: str, tag: str,
    after: str, until: str = "", limit: int = 500
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if source in {"her", "Vale"}:
        clauses.append("k.saved_by = ?")
        params.append(source)
    if view == "words":
        clauses.append("trim(k.text_snapshot) != ''")
    if view == "album":
        clauses.append("k.media_json LIKE '%\"kind\": \"image\"%'")
    if after:
        clauses.append("k.message_at >= ?")
        params.append(after)
    if until:
        clauses.append("k.message_at < ?")
        params.append(until)
    if tag:
        clauses.append("EXISTS (SELECT 1 FROM keep_item_tags it JOIN keep_tags t ON t.id=it.tag_id WHERE it.keep_id=k.id AND t.name=?)")
        params.append(tag.lstrip("#"))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT k.id FROM keep_items k" + where + " ORDER BY k.message_at DESC, k.id DESC LIMIT ?",
        (*params, max(1, min(int(limit or 500), 1000))),
    ).fetchall()
    return [payload for row in rows if (payload := item_payload(conn, int(row["id"]))) is not None]


def list_tags(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT t.name FROM keep_tags t JOIN keep_item_tags it ON it.tag_id=t.id "
        "ORDER BY t.name COLLATE NOCASE"
    ).fetchall()
    return [r["name"] for r in rows]


def add_note(conn: sqlite3.Connection, keep_id: int, author: str, content: str) -> int:
    text = str(content or "").strip()
    if not text or len(text) > 4000:
        raise ValueError("note must contain 1 to 4000 characters")
    if not conn.execute("SELECT 1 FROM keep_items WHERE id = ?", (keep_id,)).fetchone():
        raise LookupError("keep not found")
    cursor = conn.execute(
        "INSERT INTO keep_notes (keep_id, author, created_at, content) VALUES (?, ?, ?, ?)",
        (keep_id, author, _now_iso(), text),
    )
    conn.commit()
    return int(cursor.lastrowid)


def delete_item(conn: sqlite3.Connection, keep_id: int, owner: str) -> bool:
    row = conn.execute("SELECT saved_by FROM keep_items WHERE id = ?", (keep_id,)).fetchone()
    if not row:
        return False
    if row["saved_by"] != owner:
        raise PermissionError("cannot delete another person's keep")
    conn.execute("DELETE FROM keep_item_tags WHERE keep_id = ?", (keep_id,))
    conn.execute("DELETE FROM keep_notes WHERE keep_id = ?", (keep_id,))
    conn.execute("DELETE FROM keep_items WHERE id = ?", (keep_id,))
    conn.commit()
    return True


def delete_note(conn: sqlite3.Connection, note_id: int, author: str) -> bool:
    row = conn.execute("SELECT author FROM keep_notes WHERE id = ?", (note_id,)).fetchone()
    if not row:
        return False
    if row["author"] != author:
        raise PermissionError("cannot delete another person's note")
    conn.execute("DELETE FROM keep_notes WHERE id = ?", (note_id,))
    conn.commit()
    return True


def migrate_legacy_favorites(
    conn: sqlite3.Connection,
    *,
    gateway_db: Path,
    upload_dir: Path,
    voice_dir: Path,
    sticker_dir: Path,
    keep_media_dir: Path,
    media_public_path: str,
) -> tuple[int, int]:
    """Idempotently import gateway snapshots and the one relay legacy row."""
    gateway_count = 0
    legacy_count = 0
    messages = conn.execute(
        "SELECT id, ts, direction, kind, text, meta FROM messages ORDER BY id"
    ).fetchall()
    if gateway_db.exists():
        source = sqlite3.connect(f"file:{gateway_db}?mode=ro", uri=True, timeout=2)
        source.row_factory = sqlite3.Row
        try:
            favorites = source.execute(
                "SELECT id, turn_id, kind, saved_by, speaker, user_text, assistant_text, turn_created_at, created_at, note "
                "FROM favorites ORDER BY id"
            ).fetchall()
        finally:
            source.close()
        for favorite in favorites:
            source_key = f"gateway-favorite:{favorite['id']}"
            if conn.execute("SELECT 1 FROM keep_items WHERE source_key=?", (source_key,)).fetchone():
                continue
            speaker = str(favorite["speaker"] or "Vale")
            saved_by = str(favorite["saved_by"] or "her")
            user_text = str(favorite["user_text"] or "")
            assistant_text = str(favorite["assistant_text"] or "")
            main_text = assistant_text if speaker == "Vale" else user_text
            quote = user_text if speaker == "Vale" else assistant_text
            turn_id = int(favorite["turn_id"]) if favorite["turn_id"] is not None else None
            msg_id = _match_legacy_message(messages, speaker, main_text, assistant_text, favorite["turn_created_at"])
            media = {"items": [], "segments": []}
            if msg_id is not None:
                msg = next((m for m in messages if int(m["id"]) == msg_id), None)
                if msg:
                    meta = _json_load(msg["meta"], {})
                    if isinstance(meta, dict):
                        try:
                            media = _message_media(
                                message_id=msg_id, meta=meta, source_key=source_key,
                                upload_dir=upload_dir, voice_dir=voice_dir, sticker_dir=sticker_dir,
                                keep_media_dir=keep_media_dir, media_public_path=media_public_path,
                            )
                        except Exception:
                            media = {"items": [], "segments": []}
            voice = conn.execute(
                "SELECT msg_id, segments FROM favorites_voice WHERE turn_id = ?", (turn_id,)
            ).fetchone() if turn_id is not None else None
            if voice:
                old_segments = _json_load(voice["segments"], [])
                if isinstance(old_segments, list) and old_segments:
                    media["legacy_voice"] = {"msg_id": int(voice["msg_id"]), "segments": old_segments}
                    existing_urls = {str(x.get("url")) for x in media.get("items", []) if isinstance(x, dict)}
                    for index, segment in enumerate(old_segments):
                        if not isinstance(segment, dict) or segment.get("kind") != "voice":
                            continue
                        url = str(segment.get("url") or "")
                        basename = Path(urlparse(url).path).name
                        if not basename:
                            continue
                        saved = _copy_known_media(
                            source=voice_dir / "kept" / basename, name=basename, media_kind="voice",
                            keep_id_key=f"{source_key}:legacy-voice:{index}", media_dir=keep_media_dir,
                            public_path=media_public_path,
                        )
                        if saved:
                            media.setdefault("items", []).append(saved)
                            segment["url"] = saved["url"]
                        elif url and url not in existing_urls:
                            pass  # Keep the historic authenticated voice URL as a fallback.
            cursor = conn.execute(
                "INSERT OR IGNORE INTO keep_items (source_key, source_kind, saved_by, speaker, message_id, turn_id, message_at, saved_at, text_snapshot, quote_snapshot, user_text, assistant_text, media_json, source_json) "
                "VALUES (?, 'gateway-favorite', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_key, saved_by, speaker, msg_id, turn_id,
                 str(favorite["turn_created_at"] or ""), str(favorite["created_at"] or _now_iso()),
                 main_text, quote, user_text, assistant_text, json.dumps(media, ensure_ascii=False),
                 json.dumps({"gateway_id": int(favorite["id"]), "kind": favorite["kind"], "note": favorite["note"]}, ensure_ascii=False)),
            )
            if cursor.rowcount:
                gateway_count += 1

    legacy_rows = conn.execute("SELECT id, msg_id, session_id, role, text, created_at FROM favorites ORDER BY id").fetchall()
    for old in legacy_rows:
        source_key = f"relay-favorite:{old['id']}"
        if conn.execute("SELECT 1 FROM keep_items WHERE source_key=?", (source_key,)).fetchone():
            continue
        msg_id = int(old["msg_id"])
        message = next((m for m in messages if int(m["id"]) == msg_id), None)
        media = {"items": [], "segments": []}
        if message:
            meta = _json_load(message["meta"], {})
            if isinstance(meta, dict):
                try:
                    media = _message_media(
                        message_id=msg_id, meta=meta, source_key=source_key,
                        upload_dir=upload_dir, voice_dir=voice_dir, sticker_dir=sticker_dir,
                        keep_media_dir=keep_media_dir, media_public_path=media_public_path,
                    )
                except Exception:
                    media = {"items": [], "segments": []}
        conn.execute(
            "INSERT OR IGNORE INTO keep_items (source_key, source_kind, saved_by, speaker, message_id, message_at, saved_at, text_snapshot, media_json, source_json) "
            "VALUES (?, 'relay-legacy', 'her', ?, ?, ?, ?, ?, ?, ?)",
            (source_key, "Vale" if old["role"] == "assistant" else "her", msg_id,
             str(message["ts"] or "") if message else "", str(old["created_at"] or _now_iso()),
             str(old["text"] or ""), json.dumps(media, ensure_ascii=False),
             json.dumps({"legacy_id": int(old["id"]), "session_id": str(old["session_id"] or "")}, ensure_ascii=False)),
        )
        legacy_count += 1
    conn.commit()
    return gateway_count, legacy_count


def _match_legacy_message(messages, speaker: str, main_text: str, assistant_text: str, turn_created_at: str) -> int | None:
    """Best-effort anchor to the exact relay message, never a reason to drop text."""
    target = assistant_text if speaker == "Vale" else main_text
    direction = "out" if speaker == "Vale" else "in"
    target_norm = _norm(target)
    if not target_norm:
        return None
    candidates = [m for m in messages if m["direction"] == direction and _norm(m["text"]) == target_norm]
    if not candidates:
        return None
    try:
        turn_at = datetime.fromisoformat(str(turn_created_at).replace("Z", "+00:00"))
        if turn_at.tzinfo is None:
            turn_at = turn_at.replace(tzinfo=timezone.utc)
        def score(row):
            at = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return abs((turn_at - at).total_seconds())
        candidates.sort(key=score)
    except (TypeError, ValueError, OverflowError):
        candidates.sort(key=lambda row: int(row["id"]))
    return int(candidates[0]["id"])
