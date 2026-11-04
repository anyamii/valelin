#!/usr/bin/env python3
"""
companion relay backend — a private 1:1 message channel between a person and
their AI companion (an AI running locally as a Claude Code "channel" plugin).

Two ends, one shared secret:
  - AI side   (local CC channel plugin):  POST /channel/out  ·  SSE GET /channel/in
  - Human side (phone PWA):               POST /app/send     ·  SSE GET /app/stream  ·  GET /app/history

No framework magic: messages land in sqlite and fan out to SSE subscribers via
one asyncio.Queue per connection. A single shared Bearer secret guards every
endpoint (single user). The secret may travel in the Authorization header *or*
as a ?token= query param — because the browser's native EventSource cannot set
custom headers.

Everything personal — names, secrets, domain, paths — comes from environment
variables (see .env.example). Nothing identifying is hard-coded.
"""

import asyncio
import base64
import bisect
import mimetypes
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

log = logging.getLogger(__name__)

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError
from mcp.types import Implementation
from mcp.server.fastmcp import FastMCP

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.brain_providers import CallbackProvider, ProviderRouter
from backend.agent_activity import activity_for_run, persist_activity_for_stream, run_id_for_stream
from backend.daily_context import DAILY_DOCUMENT_KEYS
from backend.weather_context import (
    DEFAULT_CITY, WEATHER_CITY_KEY, cached_weather_line, normalize_weather_city,
)
from backend.daily_calendar import valid_daily_document as _valid_model_daily_document
from backend.letters import (
    LetterError, ensure_letters_schema, due_letters, visible_letters, mark_letter,
)
from backend.home_today import home_today_payload
from backend import notes as notes_store
from backend.ledger import (
    LedgerToolError,
    add_entry as add_ledger_entry,
    confirm_entry as confirm_ledger_entry,
    ensure_ledger_schema,
    get_entry as get_ledger_entry,
    list_entries as list_ledger_entries,
    settle_entry as settle_ledger_entry,
    update_user_entry as update_user_ledger_entry,
    void_entry as void_ledger_entry,
)
from backend.command_card import (
    CommandCardError,
    backfill_command_message_meta,
    command_state_from_db,
    ensure_command_schema,
    expire_overdue_commands,
    finish_command,
    recent_command_cards_from_db,
)
from backend import keeps as keeps_store
from backend import keeps_activity
from backend.voice_audio_tags import strip_voice_audio_tags
from backend.sdk_provider import ClaudeSDKProvider, SDKProviderConfig
from backend.local_outbox import OutboxConfig, LocalOutbox, raw_event
from backend.sdk_text import ToolTextJoiner
from near_context import desktop_line_clause, is_desktop_session

try:
    from pywebpush import webpush, WebPushException
except Exception:  # a missing lib must not stop the relay from starting
    webpush = None
    class WebPushException(Exception):
        pass


# --- identity (parameterized — set these to your own names) ----------------
AI_NAME = os.environ.get("RELAY_AI_NAME", "AI")          # AI companion's display name (push title, narration)
HUMAN_NAME = os.environ.get("RELAY_HUMAN_NAME", "对方")   # how the AI is told about you in voice/call narration

# --- core config / secrets (all from env) ----------------------------------
SECRET = os.environ.get("RELAY_SECRET", "")
DB_PATH = os.environ.get("RELAY_DB_PATH", "/data/relay.db")
_local_outbox = LocalOutbox(OutboxConfig.from_env())
# Railway's dynamic $PORT takes priority; RELAY_PORT is the local-dev fallback.
PORT = int(os.environ.get("PORT") or os.environ.get("RELAY_PORT", "3011"))
RELAY_HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
UPLOAD_DIR = Path(os.environ.get("RELAY_UPLOAD_DIR", str(Path(__file__).parent / "uploads")))
KEEP_MEDIA_DIR = UPLOAD_DIR / "keeps"
KEEP_MEDIA_PUBLIC_PATH = "/app/keeps/media"
KEEPS_MCP_ID = "Vale-keeps"
PUBLIC_PREFIX = os.environ.get("RELAY_PUBLIC_PREFIX", "").rstrip("/")
APP_PATH = os.environ.get("RELAY_APP_PATH", "/")  # where a push-notification tap opens the PWA
ALLOW_ORIGINS = [o.strip() for o in os.environ.get(
    "RELAY_ALLOW_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"
).split(",") if o.strip()]
MAX_UPLOAD_BYTES = int(os.environ.get("RELAY_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_ATTACHMENT_COMMAND_OUTPUT_BYTES = 512 * 1024
MAX_ATTACHMENT_TEXT_CHARS = 480_000
MAX_PDF_TEXT_PAGES = 100
VOICE_MAX_BYTES = int(os.environ.get("RELAY_VOICE_MAX_BYTES", str(8 * 1024 * 1024)))
VOICE_TRANSCRIBE_CMD = os.environ.get("RELAY_VOICE_TRANSCRIBE_CMD", "")

# --- MiniMax TTS (optional — leave keys blank to disable spoken replies) ----
MINIMAX_API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "speech-02-hd")
MINIMAX_VOICE_ZH = os.environ.get("MINIMAX_VOICE_ZH", "")
MINIMAX_TTS_TIMEOUT = float(os.environ.get("MINIMAX_TTS_TIMEOUT", "30"))
# MOSS/MOSI TTS（api.mosi.cn/v1/audio/speech，op 端 voice_bar 实测可用版）：Bearer 认证，
# body 严格 {"model","input","voice_id","response_format","delivery_method"}，无 version；
# 响应给音频 URL，下载得 mp3。旧的 studio.mosi.cn/v1/audio/tts 对一切请求 401，勿回退。
MOSS_API_BASE = os.environ.get("MOSS_API_BASE", "https://api.mosi.cn")
MOSS_API_KEY = os.environ.get("MOSS_API_KEY", "")
MOSS_VOICE_ID = os.environ.get("MOSS_VOICE_ID", "")

# --- TTS 上游（/app/tts 多提供商默认走 mimo；custom 用前端传的 URL）----
MIMO_TTS_URL = os.environ.get("MIMO_TTS_URL", "https://web-production-8d25b6.up.railway.app/api/tts")
TTS_UPSTREAM_TIMEOUT = float(os.environ.get("TTS_UPSTREAM_TIMEOUT", "60"))
# mimo v2.5 自填音色复刻：前端传 mi_api_key 时走 OpenAI 兼容 /v1/chat/completions（zero-shot 传样本）；env 仅兜底
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.environ.get("MIMO_MODEL", "mimo-v2.5-tts-voiceclone")

# --- 服务端语音合成（SSE 随流推音频）---------------------------------------
# segment_mode_on 时 relay 把 reply 流改成「整条级」：每个 block 首行 [voice] → 合成语音，
# done 时合成一次推单条 voice_msg；否则整条是文字，按显式规则切（[br] 分隔 + 双换行分气泡 +
# 400 字无换行兜底；句末标点自动分句已关，TASK-bubble-split-redo），
# 逐条推 text_msg。done 时把 segments 挂到 reply meta 一次性落库（content 保留
# [voice] 标记供上下文回送）。音频文件由 nginx /app/voice/ 静态服务（immutable 缓存）。
# 两个开关由前端 RHYTHM 卡 POST /app/voice_mode 同步：segment_mode_on 控制切分路径，
# voice_mode_on 控制 [voice] 合成（且须 segment_mode_on）；全关时原路径照旧推整段 reply_delta。
VOICE_DIR = Path(os.environ.get("RELAY_VOICE_DIR", str(Path(__file__).parent / "voice")))
VOICE_PUBLIC_PREFIX = os.environ.get("RELAY_VOICE_PUBLIC_PREFIX", "/app/voice").rstrip("/")
VOICE_DONE_TIMEOUT = float(os.environ.get("RELAY_VOICE_DONE_TIMEOUT", "90"))
# 语音块标记：每「消息」（\n\n 切的 block）首行 [voice] → 整块一条语音（可带 · 时长 · 情绪）。
# 管道参数（TASK-moss-sound-instruction）：[voice|instruction:…|sound_event:…] —— 值里可有
# 空格/逗号/英文标点，遇到 ] 才结束（值内不许换行）；分组 1 = 含括号整体（_voice_pipe_params 用）。
VOICE_MARK_RE = re.compile(r"^(\[voice(?:\s*·\s*(?:\d+:\d+))?(?:\s*·\s*[a-z]+)?(?:\|[^\]\n]*)?\])\s*")
# 管道参数只认这两个（进 MOSS 请求体同名字段）；未知参数名静默忽略（不报错、不透传）。
VOICE_KNOWN_PARAMS = ("instruction", "sound_event")
# 语音块超长保护（PATCH2 §B.3 修订）：[voice] 剥标记后超 VOICE_MAX_CHARS → 按句末标点
# 拆成多条语音段全部朗读（不丢片段、绝不拦腰切词），不再「截一段语音 + 尾巴转文字」。
# 80 太小（正常英文整句就 >80 会被拦腰截断丢英文），提到 200 同时仍防超长单条。
# 10-14 提到 600：200 会把一条 ~30 秒的英文语音切成两条（她点名要整条），600 ≈ 一分钟内一条。
VOICE_MAX_CHARS = 600
# 语音段切分边界：中英句末标点都算（中文。！？ + 英文 .!? + 省略号 …），
# 避免英文整句搜不到边界被 80 硬切中词丢片段。
VOICE_SPLIT_END_RE = re.compile(r"(?<=[。！？!?\.…])")
# 旧句子级 <voice> 标签：文字路径 / 段清洗时剥掉（兼容历史数据 + 模型偶尔回退）。
LEGACY_VOICE_TAG_RE = re.compile(r"<voice>[\s\S]*?</voice>", flags=re.I)
# 两个独立开关（前端 RHYTHM 卡拆分后 /app/voice_mode 双字段同步）：
# segment_mode_on → reply 按 \n\n 切段；voice_mode_on → [voice] 块合成（且须 segment 开）。
# 兜底语义不变：默认 0 → 升级后仍「不分段 + 不语音」，不会变成分段开着。
segment_mode_on: bool = os.environ.get("RELAY_SEGMENT_MODE", "0") == "1"
voice_mode_on: bool = os.environ.get("RELAY_VOICE_MODE", "0") == "1"
# 语音块 TTS 配置（前端 More 页 /app/voice_mode 同步；relay 重启回退默认 mimo）。
voice_tts_cfg: dict = {
    "provider": "", "custom_url": "", "custom_method": "GET",
    "el_api_key": "", "el_voice_id": "", "el_model": "",
    "mm_api_key": "", "mm_group_id": "", "mm_voice_id": "", "mm_model": "",
    "mi_api_key": "", "mi_voice_id": "",
    "mo_api_key": "", "mo_voice_id": "", "mo_model": "", "mo_version": "",
}
_tts_lock = asyncio.Lock()                          # 全局串行：mimo 并发会 429
voice_stream_locks: dict[str, asyncio.Lock] = {}    # stream_id -> 切分/推送临界区锁（保 seq 顺序）

# 语音配置持久化：内存态一重启就丢（2026-08-29 22:52 通话轮 [voice] 合成 url=null 的根因——
# 22:44 重启后配置回空，PWA 只在改开关时回传，[voice] 合成悄悄退化）。落 DB 同目录。
VOICE_STATE_PATH = Path(os.environ.get("RELAY_VOICE_STATE", str(Path(DB_PATH).parent / "voice_mode.json")))


def _save_voice_state() -> None:
    try:
        VOICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "segment_mode_on": segment_mode_on,
            "voice_mode_on": voice_mode_on,
            "sticker_mode_on": sticker_mode_on,
            "tts_cfg": voice_tts_cfg,
            "stt_cfg": voice_stt_cfg,
        }
        tmp = VOICE_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(VOICE_STATE_PATH)
        os.chmod(VOICE_STATE_PATH, 0o600)   # 内含 TTS key，权限收紧
    except Exception as exc:
        print(f"[voice-mode] 状态保存失败: {type(exc).__name__}: {exc}", flush=True)


def _load_voice_state() -> None:
    global segment_mode_on, voice_mode_on, sticker_mode_on
    try:
        data = json.loads(VOICE_STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"[voice-mode] 状态载入失败（用默认值）: {type(exc).__name__}: {exc}", flush=True)
        return
    segment_mode_on = bool(data.get("segment_mode_on"))
    voice_mode_on = bool(data.get("voice_mode_on"))
    sticker_mode_on = bool(data.get("sticker_mode_on"))
    saved_cfg = data.get("tts_cfg") or {}
    for k in voice_tts_cfg:
        if k in saved_cfg:
            voice_tts_cfg[k] = saved_cfg.get(k) or ""
    saved_stt = data.get("stt_cfg") or {}
    for k in voice_stt_cfg:
        if k in saved_stt:
            voice_stt_cfg[k] = str(saved_stt.get(k) or "")
    print(f"[voice-mode] 状态已载入 provider={voice_tts_cfg.get('provider') or '(空)'} "
          f"segment={segment_mode_on} voice={voice_mode_on} sticker={sticker_mode_on} "
          f"stt={voice_stt_cfg.get('stt_provider') or '(env)'}", flush=True)


# --- SDK 运行时配置（MORE 页 模型/思考程度/上下文 三卡；TASK-model-effort-full）---
# sdk_config.json 是唯一真相源（DB 同目录，voice_mode.json 同模式）。
# thinking 的开关、预算和显示方式以 bridge_ready 回传的 assembly 为准；effort 只存不发，
# 唯一下发点 = spawn 时 env 注入（call 会话在那里强制 low）；model 改动走
# setModel 暖会话直换（历史保留，代价 = 换模型必打穿一次缓存，用户主动付）。
SDK_CONFIG_PATH = Path(os.environ.get("RELAY_SDK_CONFIG", str(Path(DB_PATH).parent / "sdk_config.json")))
_sdk_config_lock = asyncio.Lock()
sdk_config: dict = {
    "model": os.environ.get("CLAUDE_SDK_MODEL", "sonnet").strip() or "sonnet",
    "effort": os.environ.get("CLAUDE_SDK_EFFORT", "high").strip() or "high",
    "auto_compact_pct": None,       # None=SDK 默认阈值；60/75/90=按窗口百分比
    "resolved_model": "",
    "updated_at": "",
}
# 别名（CLI 稳定支持）；[1m] 后缀 = 1M 长上下文变体。
SDK_MODEL_ALIASES = ("sonnet", "opus", "haiku", "sonnet[1m]", "opus[1m]", "fable", "best")
# 静态完整 ID 目录（完整版本清单；live supportedModels() 只提供别名，不能替代它）。
# 来源：Anthropic Model deprecations 的 Active 表（2026-09-23 快照）。这份列表需要人工维护，
# 因为 Claude Code /model 和 SDK supportedModels() 都没有公开「全部 full model ID」目录接口。
# Opus 5.5 两个 ID 依据 2026-09-23 升级报告中的 probe/ctx 原始 JSON。
# Sonnet 5.5 官方 API ID/alias 为 claude-sonnet-5-5，官方模型总览列出原生 1M 上下文。
# claude-{opus,sonnet}-4-6[1m] 的 1M 值来自 2026-09-06 独立 ctx 复测原始 JSON
# （projects/model-effort-full/probe-{opus,sonnet}-4-6-1m-2026-09-06.json）。
SDK_STATIC_MODEL_IDS = (
    "claude-sonnet-5", "claude-sonnet-5[1m]",
    "claude-sonnet-5-5", "claude-sonnet-5-5[1m]",
    "claude-opus-5-5", "claude-opus-5-5[1m]",
    "claude-opus-5", "claude-opus-5[1m]",
    "claude-haiku-4-5",
    "claude-opus-4-6", "claude-opus-4-6[1m]", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-4-5",
    "claude-sonnet-4-6", "claude-sonnet-4-6[1m]", "claude-sonnet-4-5",
    "claude-fable-5-1[1m]",
)
# effort 六档（applyFlagSettings 口径；ultracode 是独立 boolean 键，这里归一成档位名）。
SDK_EFFORT_TIERS = ("low", "medium", "high", "xhigh", "max", "ultracode")
SDK_AUTOCOMPACT_PRESETS = (60, 75, 90)


def _usage_credits_model(model: str) -> bool:
    """fable / best / claude-fable* 走 usage credits，SDK 模式无确认直接扣。"""
    return model in ("fable", "best") or model.startswith("claude-fable")


def _static_model_catalog() -> list:
    """静态完整版本目录。

    这是版本选择器的基线而不是仅在无活会话时的 fallback：SDK live 目录目前只返回
    default/sonnet/opus/haiku/fable 等可路由别名，会漏掉所有历史 full ID。
    4.6 的两个 [1m] 变体已由 2026-09-06 原始 ctx 探针 JSON 独立复测为 1M；
    fable 唯一目录 ID 是 claude-fable-5-1[1m]（横杠，官方描述自带 Requires usage credits），
    未做调用级实测。"""
    def entry(value: str, name: str, group: str, credits: bool, window: int) -> dict:
        return {"value": value, "name": name, "group": group,
                "usage_credits": credits, "context_window": window}
    return [
        entry("sonnet", "Sonnet（默认档）", "常用", False, 1_000_000),
        entry("opus", "Opus", "常用", False, 1_000_000),
        entry("haiku", "Haiku", "常用", False, 200_000),
        entry("sonnet[1m]", "Sonnet · 1M 长上下文", "长上下文", False, 1_000_000),
        entry("opus[1m]", "Opus · 1M 长上下文", "长上下文", False, 1_000_000),
        entry("claude-sonnet-5", "claude-sonnet-5", "指定版本", False, 1_000_000),
        entry("claude-sonnet-5[1m]", "claude-sonnet-5[1m]", "指定版本", False, 1_000_000),
        entry("claude-sonnet-5-5", "claude-sonnet-5-5", "指定版本", False, 1_000_000),
        entry("claude-sonnet-5-5[1m]", "claude-sonnet-5-5[1m]", "指定版本", False, 1_000_000),
        entry("claude-opus-5-5", "claude-opus-5-5", "指定版本", False, 1_000_000),
        entry("claude-opus-5-5[1m]", "claude-opus-5-5[1m]", "指定版本", False, 1_000_000),
        entry("claude-opus-5", "claude-opus-5", "指定版本", False, 1_000_000),
        entry("claude-opus-5[1m]", "claude-opus-5[1m]", "指定版本", False, 1_000_000),
        entry("claude-haiku-4-5", "claude-haiku-4-5", "指定版本", False, 200_000),
        entry("claude-opus-4-6", "claude-opus-4-6（Lin 常用）", "指定版本", False, 200_000),
        entry("claude-opus-4-6[1m]", "claude-opus-4-6[1m] · 1M（⚠️ usage credits）", "指定版本", True, 1_000_000),
        entry("claude-opus-4-7", "claude-opus-4-7", "指定版本", False, 1_000_000),
        entry("claude-opus-4-8", "claude-opus-4-8", "指定版本", False, 1_000_000),
        entry("claude-opus-4-5", "claude-opus-4-5", "指定版本", False, 200_000),
        entry("claude-sonnet-4-6", "claude-sonnet-4-6", "指定版本", False, 200_000),
        entry("claude-sonnet-4-6[1m]", "claude-sonnet-4-6[1m] · 1M（⚠️ usage credits）", "指定版本", True, 1_000_000),
        entry("claude-sonnet-4-5", "claude-sonnet-4-5", "指定版本", False, 200_000),
        entry("fable", "Fable（⚠️ usage credits）", "高级", True, 1_000_000),
        entry("best", "Best（⚠️ usage credits）", "高级", True, 1_000_000),
        entry("claude-fable-5-1[1m]", "claude-fable-5-1[1m]（⚠️ usage credits）", "高级", True, 1_000_000),
    ]


def _model_catalog_with_live(live_models: list | None) -> list:
    """静态完整目录为基线，live 仅补充/刷新当前 CLI 暴露的别名元数据。

    保留静态顺序和完整版本，避免稀疏的 supportedModels() 覆盖掉指定版本下拉。
    """
    catalog = _static_model_catalog()
    by_value = {
        item["value"]: index
        for index, item in enumerate(catalog)
        if isinstance(item, dict) and isinstance(item.get("value"), str)
    }
    for live in live_models or []:
        if not isinstance(live, dict) or not isinstance(live.get("value"), str):
            continue
        value = live["value"]
        if value in by_value:
            # 静态项保留 context_window / group；live 项覆盖 resolvedModel 等运行时事实。
            catalog[by_value[value]] = {**catalog[by_value[value]], **live}
        else:
            by_value[value] = len(catalog)
            catalog.append(live)
    return catalog


def _save_sdk_config() -> None:
    try:
        SDK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(sdk_config)
        tmp = SDK_CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SDK_CONFIG_PATH)
    except Exception as exc:
        print(f"[sdk:config] 状态保存失败: {type(exc).__name__}: {exc}", flush=True)


def _load_sdk_config() -> None:
    try:
        data = json.loads(SDK_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:
        print(f"[sdk:config] 状态载入失败（用默认值）: {type(exc).__name__}: {exc}", flush=True)
        return
    if isinstance(data.get("model"), str) and data["model"]:
        sdk_config["model"] = data["model"]
    if data.get("effort") in SDK_EFFORT_TIERS:
        sdk_config["effort"] = data["effort"]
    if data.get("auto_compact_pct") in SDK_AUTOCOMPACT_PRESETS or data.get("auto_compact_pct") is None:
        sdk_config["auto_compact_pct"] = data.get("auto_compact_pct")
    print(f"[sdk:config] 已载入 model={sdk_config['model']} effort={sdk_config['effort']} "
          f"auto_compact_pct={sdk_config['auto_compact_pct']}", flush=True)


_load_sdk_config()


# 句末标点 + 紧跟的收尾引号/括号算同一个切点：「……小猫。」的 」跟前一句走，不落成孤泡。
# 只吃紧跟在标点后的连续收尾串（成对右半 + 直/弯引号），句子开头的引号不受影响。
# （TASK-bubble-split-redo：只供 400 字无换行兜底使用，正常文本不再按标点分气泡。）
SENTENCE_END_RE = re.compile(r"""[。！？!?][」』"'）)】》〉”’]*""")
STREAM_PARAGRAPH_BREAK = "\n\n"
# ── 气泡分割（TASK-bubble-split-redo：标点驱动 → 显式驱动；规范 = workspace
#    projects/bubble-split/BUBBLE-SPLIT-RULES.md，relay 与前端同规则，共用用例集对齐）──
# 层 1：显式分隔标记 [br]（最高优先级，双层不许覆盖）。大小写不敏感；连续多个/首尾的
# 由「切段后滤空」消化，不产生空气泡；标记本身剥离不显示（四入口 + 通知 + 引用快照）。
BR_MARK_RE = re.compile(r"\[br\]", re.I)
# 层 1 流式兜底：done flush 时尾部残缺前缀（"[" / "[b" / "[br"）剥掉，半个标记不渲染。
BR_PARTIAL_TAIL_RE = re.compile(r"\[(?:b(?:r)?)?$", re.I)
# 层 2：双换行（中间可夹空白）= 分气泡；单换行不分。
BREAK_RE = re.compile(r"\n[ \t]*\n")
# 层 2 例外（整块不可切，现象 A 修复）：结构行判据——有序列表 1. 2. 1) （1）、无序列表
# - * •（标记后须空白）、表格行 |、引用 >、围栏 ```；缩进代码 ^ {4,}\S 按原始行判。
STRUCT_LINE_RE = re.compile(r"^(?:\s*(?:\d{1,3}[.、）)]|（\d+）|[-*•]\s|\||>|```)| {4,}\S)")
# 层 3 防御兜底：单个文字气泡超 400 字且无换行才按句末标点切多段（触发记 INFO 日志）。
TEXT_BUBBLE_MAX_CHARS = 400

# --- STT 语音转文字（语音通话用；/app/voice 音频分支）-----------------------
# 三路可选，RELAY_STT_MODE：api / cmd / whisper / auto / off（默认 auto 按序探测）。
#   api     = OpenAI 兼容 /audio/transcriptions（RELAY_STT_API_URL + RELAY_STT_API_KEY + RELAY_STT_MODEL），
#             Groq / OpenAI / 各类兼容站都能接，以后做成可选 provider 的口子留在这。
#   cmd     = 旧钩子 RELAY_VOICE_TRANSCRIBE_CMD（外部命令 <audio_path> <mime> → stdout）。
#   whisper = 本地 faster-whisper（CPU int8）。模型缓存目录必须在 systemd ReadWritePaths
#             里（ProtectHome=true 时 ~/.cache 不可写）：RELAY_STT_WHISPER_DIR，
#             默认 VOICE_DIR 同级 stt-models → 生产 ./data/valelin/stt-models。
# 转写跑线程（asyncio.to_thread），几十秒的 CPU 活不卡 SSE 事件循环。
STT_MODE = os.environ.get("RELAY_STT_MODE", "auto").strip().lower()
STT_API_URL = os.environ.get("RELAY_STT_API_URL", "").strip()
STT_API_KEY = os.environ.get("RELAY_STT_API_KEY", "").strip()
STT_API_MODEL = os.environ.get("RELAY_STT_MODEL", "whisper-large-v3-turbo").strip()
STT_LANG = os.environ.get("RELAY_STT_LANG", "zh").strip()          # 空 = 自动检测
STT_TIMEOUT = float(os.environ.get("RELAY_STT_TIMEOUT", "30"))
STT_MAX_CHARS = int(os.environ.get("RELAY_STT_MAX_CHARS", "500"))
STT_WHISPER_MODEL = os.environ.get("RELAY_STT_WHISPER_MODEL", "small").strip()
STT_WHISPER_DIR = Path(os.environ.get(
    "RELAY_STT_WHISPER_DIR", str(VOICE_DIR.parent / "stt-models")))
STT_WARMUP = os.environ.get("RELAY_STT_WARM", "0") == "1"           # 启动即后台预载模型
STT_WHISPER = {"model": None, "failed": False}                      # 进程内懒加载单例
STT_WHISPER_LOCK = threading.Lock()
# ElevenLabs Scribe 路（复盘实战同款）：keyterms 热词原生支持（只有 scribe_v2 认，v1 不认），
# 返回逐词 start/end/type。key 只在服务端 env，永不进浏览器（与 /app/tts 的 localStorage key 无关）。
STT_EL_URL = os.environ.get("RELAY_STT_EL_URL", "https://api.elevenlabs.io/v1/speech-to-text").strip()
STT_EL_KEY = os.environ.get("RELAY_STT_EL_KEY", "").strip()
STT_EL_MODEL = os.environ.get("RELAY_STT_EL_MODEL", "scribe_v2").strip()
# MORE 页「识别 · STT」卡同步来的运行时配置（keyterms 为换行/逗号分隔原始串，转写时才解析）。
# 卡上选了 provider 就优先于 env；key 是否在场只看 env（卡上没有 key 输入框，这是刻意的）。
voice_stt_cfg = {"stt_provider": "", "stt_model": "", "stt_lang": "", "stt_keyterms": ""}
_stt_mode_logged = {"mode": None}                                   # 只在解析结果变化时打一行日志

# ── voice_latency（通话七段计时结构化日志，cove 教程第 3 章那套）─────────────
# 纯观测：只 print 不改任何行为。一轮通话的锚点是用户语音消息 id（loop 回流带
# source_id 回来），各阶段日志带 since_stt_ms=距转写完成的累计毫秒，正好对上
# 「说完 → 第一声」的体感。客户端（endpoint/播放）另有半段经 /app/latency 回传。
VL_T0: dict[int, float] = {}          # user_msg_id → 转写完成时刻（monotonic）
VL_T0_KEEP = 64                       # 只留最近 64 轮


def _vl_note_t0(user_msg_id) -> None:
    if user_msg_id is None:
        return
    try:
        VL_T0[int(user_msg_id)] = time.monotonic()
    except (TypeError, ValueError):
        return
    while len(VL_T0) > VL_T0_KEEP:
        VL_T0.pop(next(iter(VL_T0)))


def _vl(stage: str, user_msg_id, force: bool = False, **fields) -> None:
    """[voice_latency] 结构化行。user_msg_id 不在 VL_T0（非通话轮）默认不打印。"""
    known = False
    if user_msg_id is not None:
        try:
            t0 = VL_T0.get(int(user_msg_id))
        except (TypeError, ValueError):
            t0 = None
        if t0 is not None:
            fields["since_stt_ms"] = round((time.monotonic() - t0) * 1000)
            known = True
    if not known and not force:
        return
    payload = {"event": "voice_latency", "stage": stage}
    payload.update(fields)
    try:
        print(f"[voice_latency] {json.dumps(payload, ensure_ascii=False)}", flush=True)
    except Exception:
        pass

# ══════════════ 贴纸（表情包）══════════════
# 双向贴纸：贴纸走「id + 语义描述」，图片本身永不进 API。配置 JSON 在 relay 手里
# （GET /api/stickers + 管理接口），loop 读同一文件做 prompt 注入。图片由 nginx
# /media/stickers/ 静态服务（immutable），relay 也兜一条 FileResponse 便于本地跑。
STICKER_CONFIG = Path(os.environ.get("RELAY_STICKER_CONFIG", str(Path(__file__).parent / "stickers.json")))
STICKER_DIR = Path(os.environ.get("RELAY_STICKER_DIR", str(Path(__file__).parent / "stickers")))
STICKER_MEDIA_PREFIX = os.environ.get("RELAY_STICKER_MEDIA_PREFIX", "/media/stickers").rstrip("/")
STICKER_MAX_BYTES = int(os.environ.get("RELAY_STICKER_MAX_BYTES", str(2 * 1024 * 1024)))
# 贴纸标记：独占一段，前后空行，格式 :id:。id 只允许小写字母数字下划线（防路径穿越）。
STICKER_RE = re.compile(r"^:([a-z0-9_]{1,32}):$")
STICKER_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
# 模型侧文件投递：独占一段，格式 [file: /absolute/path | optional display name]。
# 只允许模型已有写权限或既有上传目录里的文件；所有判断都在 realpath 后进行，防软链越界。
FILE_MARK_RE = re.compile(r"^\[file:\s*(.*?)\s*\]$", re.DOTALL)
FILE_DELIVERY_ROOTS = (Path("./data/Vale/notes"), Path("./data/Vale/tasks"), Path("./data/valelin/uploads"), KEEP_MEDIA_DIR)
FILE_DELIVERY_MAX_PER_REPLY = 3
# 贴纸开关（前端 RHYTHM 卡 /app/voice_mode 同步；默认开）。转发给 loop 做 prompt 注入的
# 门线 = sticker_mode_on and segment_mode_on（贴纸须独立成段）。
sticker_mode_on: bool = os.environ.get("RELAY_STICKER_MODE", "1") != "0"
segment_mode_on = voice_mode_on = sticker_mode_on = True
# _load_voice_state disabled in this offline demo. # 须在 segment/voice/sticker 三个开关都定义完之后（载入值不许被默认值覆盖）
try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
)


def _sticker_media_url(sid: str) -> str:
    return f"{STICKER_MEDIA_PREFIX}/{sid}.webp"


# ══════════════ 头像与名字（avatars + names）══════════════
# 配置 JSON 存头像、名字和聊天背景字段，
# 聊天顶栏、外观页读它。数据落盘走 /srv（生产 env 指到 RELAY_AVATAR_CONFIG / RELAY_AVATAR_DIR），
# 图片 URL 是 /static/avatars/…，nginx alias 直出、relay 兜一条 FileResponse 便于本地跑。
AVATAR_CONFIG = Path(os.environ.get("RELAY_AVATAR_CONFIG", str(Path(__file__).parent / "avatar_config.json")))
AVATAR_DIR = Path(os.environ.get("RELAY_AVATAR_DIR", str(Path(__file__).resolve().parent.parent / "static" / "avatars")))
AVATAR_MAX_BYTES = int(os.environ.get("RELAY_AVATAR_MAX_BYTES", str(5 * 1024 * 1024)))
AVATAR_KEYS = (
    "assistant_avatar", "user_avatar", "assistant_name", "user_name",
    "chat_background", "chat_background_opacity",
)
_DEFAULT_AVATAR_CONFIG = {
    "assistant_avatar": "",
    "user_avatar": "",
    "assistant_name": os.environ.get("AI", "Vale"),
    "user_name": os.environ.get("USER", "Lin"),
    "chat_background": "",
    "chat_background_opacity": 100,
}


def _avatar_media_url(name: str) -> str:
    return f"/static/avatars/{name}"


def _load_avatar_config() -> dict:
    """读头像/名字配置。坏文件不炸：回退默认四字段。"""
    try:
        if AVATAR_CONFIG.is_file():
            data = json.loads(AVATAR_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = dict(_DEFAULT_AVATAR_CONFIG)
                for k in AVATAR_KEYS:
                    if k not in data:
                        continue
                    if k == "chat_background_opacity":
                        try:
                            out[k] = max(0, min(100, int(data[k])))
                        except (TypeError, ValueError):
                            pass
                    elif isinstance(data[k], str):
                        out[k] = data[k]
                return out
    except Exception as exc:
        print(f"[avatar] config load failed: {type(exc).__name__}: {exc}")
    return dict(_DEFAULT_AVATAR_CONFIG)


def _save_avatar_config(data: dict) -> dict:
    AVATAR_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    AVATAR_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _derive_sticker_id(filename: str) -> str:
    """id 留空时从文件名生成：转小写、非 [a-z0-9_] 转下划线、截到 32。"""
    base = re.sub(r"[^a-z0-9_]", "_", (filename or "").rsplit("/", 1)[-1].split(".")[0].lower()).strip("_")
    return (base or "sticker")[:32]


def _unique_sticker_id(base: str, existing: set[str]) -> str:
    """已存在则加数字后缀去重（_2, _3, …）。"""
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _load_stickers() -> dict:
    """读配置 JSON。坏文件不炸：回退空表（version 1）。"""
    try:
        if STICKER_CONFIG.is_file():
            data = json.loads(STICKER_CONFIG.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("stickers"), list):
                return data
    except Exception as exc:
        print(f"[sticker] config load failed: {type(exc).__name__}: {exc}")
    return {"version": 1, "stickers": []}


def _sticker_table() -> dict[str, dict]:
    """id -> 配置项 的查表（出方向校验用；id 不存在 → 整段丢弃）。
    解析别名：aliases {旧id: 新id} 让历史消息里的旧 id 仍查中（改名不破坏历史）。"""
    data = _load_stickers()
    table = {s.get("id"): s for s in data.get("stickers", [])
             if isinstance(s, dict) and isinstance(s.get("id"), str)}
    for alias, canonical in (data.get("aliases") or {}).items():
        if isinstance(alias, str) and alias and canonical in table:
            table[alias] = table[canonical]
    return table


def _sticker_known(sid: str) -> bool:
    return sid in _sticker_table()


def _save_stickers(data: dict) -> dict:
    data["version"] = int(data.get("version") or 1) + 1
    STICKER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    STICKER_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _sticker_public(items) -> list[dict]:
    """GET /api/stickers：只下发 id/url/owner/enabled（desc/tags 是给模型和管理页的，聊天前端不要）。
    待补充（desc 空）的贴纸 enabled 视为 false —— 面板不展示、不可发送，补描述后才可用。"""
    out = []
    for s in items:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        out.append({
            "id": sid,
            "url": _sticker_media_url(sid),
            "owner": s.get("owner") or "both",
            "enabled": bool(s.get("enabled", True)) and bool((s.get("desc") or "").strip()),
        })
    return out


def _detect_image_mime(data: bytes) -> str | None:
    """魔数判图片类型（不看扩展名）。webp 须前 12 字节带 'WEBP' 才算真。"""
    for magic, ext in _IMAGE_MAGIC:
        if data.startswith(magic):
            if ext == "webp" and b"WEBP" not in data[8:16]:
                continue
            return ext
    return None


def _to_webp(data: bytes) -> bytes:
    """Pillow 重新编码为 WebP（保留 alpha；动图取首帧）。不透传原始字节。"""
    if _PILImage is None:
        raise RuntimeError("Pillow not installed; cannot convert sticker to WebP")
    import io as _io
    with _PILImage.open(_io.BytesIO(data)) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        buf = _io.BytesIO()
        im.save(buf, "WEBP", quality=85, method=6)
        out = buf.getvalue()
    if not out:
        raise RuntimeError("WebP conversion produced empty output")
    return out


def _to_chat_background_jpeg(data: bytes) -> tuple[bytes, int, int]:
    """Downsample a chat background to JPEG, keeping its long edge at 1600px."""
    if _PILImage is None:
        raise RuntimeError("Pillow not installed; cannot convert chat background")
    import io as _io
    with _PILImage.open(_io.BytesIO(data)) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im = im.convert("RGB")
        if max(im.size) > 1600:
            resampling = getattr(_PILImage, "Resampling", _PILImage)
            im.thumbnail((1600, 1600), resampling.LANCZOS)
        width, height = im.size
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
        out = buf.getvalue()
    if not out:
        raise RuntimeError("JPEG conversion produced empty output")
    return out, width, height


def _strip_invalid_sticker_blocks(text: str) -> str:
    """分段模式下移除「匹配贴纸格式但查表不存在」的独立段：模型编造 id 静默丢弃，
    不落库、不渲染（防破图 + 防上下文回灌模仿）。合法 id 保留原样（历史仍渲染）。"""
    if not text:
        return text
    known = _sticker_table()
    kept = []
    for b in str(text).split("\n\n"):
        s = b.strip()
        m = STICKER_RE.match(s)
        if m and m.group(1) not in known:
            continue
        kept.append(b)
    return "\n\n".join(kept)


# ── URL 导入：下载（SSRF 防护）→ 魔数验图 → Pillow 转 WebP ──────────────
STICKER_URL_MAX_BYTES = int(os.environ.get("RELAY_STICKER_URL_MAX_BYTES", str(5 * 1024 * 1024)))
STICKER_URL_TIMEOUT = 10.0
STICKER_URL_CONCURRENCY = int(os.environ.get("RELAY_STICKER_URL_CONCURRENCY", "4"))  # 批量并发下载上限

# 内网/本机/保留地址黑名单（防 SSRF）。含 IPv6（::1/128、fc00::/7 等）。
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "::1/128", "::ffff:0:0/96", "fc00::/7", "fe80::/10",
))


def _check_public_host(host: str) -> str:
    """解析主机并确保解析出的 IP 全是公网地址（防 SSRF）。
    任一解析结果命中内网/本机黑名单 → 400。返回第一个公网 IP 串。
    注：防的是「URL 直指内网」和「DNS 解析到内网」两种常见面；DNS rebinding
    （解析两次不一致）未做 IP 固定连接，自托管私有服务可接受。"""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(400, f"cannot resolve host '{host}'")
    addrs = []
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        addrs.append(ip)
        if any(ip in net for net in _PRIVATE_NETWORKS):
            raise HTTPException(400, f"internal/private address not allowed: {ip_str}")
    if not addrs:
        raise HTTPException(400, f"cannot resolve host '{host}'")
    return str(addrs[0])


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向也要过 SSRF 检查：每个跳转目标重新解析 + 验公网。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public_host(urllib.parse.urlparse(newurl).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_sticker_url(url: str) -> bytes:
    """下载图片字节。只收 http/https；公网校验；10s 超时；≤5MB。
    返回原始字节（魔数验图由调用方做，不看 Content-Type）。"""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https"):
        raise HTTPException(400, "only http/https URLs allowed")
    host = u.hostname or ""
    if not host:
        raise HTTPException(400, "missing host in URL")
    _check_public_host(host)
    opener = urllib.request.build_opener(_SafeRedirectHandler)
    req = urllib.request.Request(url, headers={
        "User-Agent": "valelin-sticker/1.0",
        "Accept": "image/*",
    })
    try:
        with opener.open(req, timeout=STICKER_URL_TIMEOUT) as resp:
            data = resp.read(STICKER_URL_MAX_BYTES + 1)
    except HTTPException:
        raise
    except urllib.error.HTTPError as exc:
        raise HTTPException(502, f"download failed (HTTP {exc.code})")
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
        raise HTTPException(502, f"download failed: {type(exc).__name__}")
    if len(data) > STICKER_URL_MAX_BYTES:
        raise HTTPException(413, f"image too large (> {STICKER_URL_MAX_BYTES // (1024 * 1024)}MB)")
    return data


def _url_stem(url: str) -> str:
    """URL 文件名（去 query/fragment、去扩展名）。"""
    return urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].split(".")[0]


def _url_stem_meaningful(stem: str) -> bool:
    """文件名「有意义」判定：随机 token（CZZY9UJ）/ 纯数字 → False；
    含分隔符（bun_shy）或像单词（happy）→ True。"""
    if len(stem) < 4:
        return True
    if not re.fullmatch(r"[a-zA-Z0-9]+", stem):
        return True                        # 含 _ / - 等分隔符 → 像人工命名
    if stem.isdigit():
        return False                       # 纯数字
    if not any(c.islower() for c in stem) and any(c.isdigit() for c in stem):
        return False                       # 大写字母+数字 → 随机 token
    return True


def _derive_sticker_id_from_url(url: str, desc: str) -> str:
    """URL 导入的 id 基名：文件名有意义 → 用它；无意义（随机串/纯数字）→
    desc 拼音化兜底（中文抽不出字母 → sticker，落库时再唯一化加序号）。"""
    stem = _url_stem(url)
    if _url_stem_meaningful(stem):
        return _derive_sticker_id(stem)
    return _derive_sticker_id(desc or "")


def _save_sticker_image(webp: bytes, sid: str, desc: str, owner: str, tags: str,
                        existing: set[str], data: dict) -> str:
    """落盘 WebP + 入配置（不 _save_stickers，批量最后统一存一次避免并发写文件）。
    sid 冲突时唯一化加后缀。desc 可空 → 待补充（填描述后才可用）。返回实际落库的 id。"""
    sid = _unique_sticker_id(sid, existing)
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    (STICKER_DIR / f"{sid}.webp").write_bytes(webp)
    data.setdefault("stickers", []).append({
        "id": sid, "desc": desc, "owner": owner,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "enabled": True,
    })
    existing.add(sid)
    return sid
    if not text:
        return text
    known = _sticker_table()
    kept = []
    for b in str(text).split("\n\n"):
        s = b.strip()
        m = STICKER_RE.match(s)
        if m and m.group(1) not in known:
            continue
        kept.append(b)
    return "\n\n".join(kept)

# --- Web Push (VAPID, optional) — push unread replies to the PWA lock screen
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_PEM = os.environ.get("VAPID_PRIVATE_PEM", "")   # PEM file path OR inline PEM text
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")
PUSH_PREVIEW_CHARS = int(os.environ.get("RELAY_PUSH_PREVIEW_CHARS", "120"))

# --- presence tuning (seconds) ---------------------------------------------
PRESENCE_ONLINE_SEC = int(os.environ.get("RELAY_PRESENCE_ONLINE_SEC", "180"))
PRESENCE_RECENT_SEC = int(os.environ.get("RELAY_PRESENCE_RECENT_SEC", "1800"))

# --- Optional server-side API loop -----------------------------------------
# "desktop" keeps the original Claude Code channel path. "loop" forwards new
# human messages to a local HTTP loop, which replies through /channel/out.
BRAIN_FILE = Path(os.environ.get("RELAY_BRAIN_FILE", str(Path(__file__).parent / "brain_target")))
LOOP_INGEST_URL = os.environ.get("RELAY_LOOP_INGEST_URL", "http://127.0.0.1:3020/loop/ingest")
SDK_RATE_LIMIT_FILE = Path(
    os.environ.get("RELAY_SDK_RATE_LIMIT_FILE", str(Path(DB_PATH).parent / "sdk_rate_limit.json"))
)
SDK_MODEL_FILE = Path(
    os.environ.get("RELAY_SDK_MODEL_FILE", str(Path(DB_PATH).parent / "sdk_model"))
)
SDK_MODELS = frozenset(("sonnet", "opus", "haiku"))


def _migrate_legacy_sdk_model() -> None:
    """一次性把旧 sdk_model 文件值迁进 sdk_config.json（唯一真相源）。
    仅当 sdk_config.json 尚未落盘时迁移，绝不覆盖用户在新卡里的选择。"""
    if SDK_CONFIG_PATH.exists():
        return
    try:
        legacy = SDK_MODEL_FILE.read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeError):
        return
    if legacy in SDK_MODELS:
        sdk_config["model"] = legacy
        _save_sdk_config()
        print(f"[sdk:config] 迁移旧 sdk_model={legacy} → sdk_config.json", flush=True)


_migrate_legacy_sdk_model()
STREAM_DRAFT_TTL = int(os.environ.get("RELAY_STREAM_DRAFT_TTL", "600"))
# 重 roll 多版本：同一个 user 消息产生的回复组最多保留这么多版本。到上限就明确报错，
# 不静默覆盖最早的版本。
MAX_REROLL_VARIANTS = int(os.environ.get("RELAY_MAX_REROLL_VARIANTS", "5"))
# 图片 24h 过期：超过 TTL 的本地图懒删 + 前端灰块 / 上下文占位降级 [图片已过期]。
IMAGE_TTL_MS = int(os.environ.get("RELAY_IMAGE_TTL_HOURS", "24")) * 3600 * 1000

# --- Lumen state proxy (Valelin Home 的 mini-state 数据源) -------------------
# OB 换域名只改这一个环境变量，前端不用动。
# 默认回环：relay 与 OB 同机，走公网域名会绕一圈还多过一次鉴权门；且 Railway 那
# 段公网曾无鉴权、已视为泄漏，绝不静默回退到公网。环境变量丢了就连本机、连不上报错。
OB_BASE_URL = os.environ.get("OB_BASE_URL", "http://127.0.0.1:8010").rstrip("/")
OB_LUMEN_TIMEOUT = float(os.environ.get("OB_LUMEN_TIMEOUT", "8"))
# Mind 记忆档 → OB Dashboard 代理。密码由 Lin 在 Railway 的 Valelin 服务里配，
# 读不到就返回 archive_not_configured，绝不用默认值兜底。
OB_DASH_TIMEOUT = 8.0
OB_DASHBOARD_PASSWORD = os.environ.get("ARCHIVE_DASHBOARD_PASSWORD", "")
# OB gateway 自己的鉴权 key（= gateway 的 ARCHIVE_GATEWAY_TOKEN）。api_loop 已在用
# GATEWAY_KEY/GATEWAY_URL 打 gateway；这里只有需要鉴权的只读代理（/api/murmur/blocked）用。
# 读不到就当没配：代理返回 error 让前端显示失败态，不静默兜底。
# rotate-token · 灰度期换新：OB_TOKEN（新）优先，GATEWAY_KEY（旧）兜底。
# 两把都是 gateway 的鉴权 key；下游切完、旧 token 作废后可删 GATEWAY_KEY。
GATEWAY_KEY = os.environ.get("OB_TOKEN") or os.environ.get("GATEWAY_KEY", "")

if not SECRET:
    raise SystemExit("RELAY_SECRET is required (set it in the systemd EnvironmentFile)")


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    from backend.demo_visits import connect
    return connect(DB_PATH)


def _check_db_persistence() -> None:
    """启动时确认 db 落在持久盘上。不阻断启动，但要在日志里显眼。"""
    p = DB_PATH
    # Railway 挂了 Volume 后会注入这个环境变量
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "")
    if os.environ.get("RAILWAY_ENVIRONMENT") and not vol:
        print(f"[!!] 没有 Volume，{p} 在临时盘上，重新部署会清零", flush=True)
    elif vol and not os.path.abspath(p).startswith(os.path.abspath(vol)):
        print(f"[!!] db 路径 {p} 不在 Volume {vol} 里，重新部署会清零", flush=True)
    else:
        print(f"[ok] db 持久化正常：{p}", flush=True)


def _ensure_station_columns(conn: sqlite3.Connection) -> None:
    """老库补列：CREATE TABLE IF NOT EXISTS 不会改已有表，所以单独 ALTER。

    stations 新增 protocol / active_model 两个字段；存量库启动时在这里补上。
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(stations)").fetchall()}
    if "protocol" not in existing:
        conn.execute("ALTER TABLE stations ADD COLUMN protocol TEXT NOT NULL DEFAULT 'openai'")
    if "active_model" not in existing:
        conn.execute("ALTER TABLE stations ADD COLUMN active_model TEXT NOT NULL DEFAULT ''")


def _ensure_mcp_columns(conn: sqlite3.Connection) -> None:
    """老库补列：MCP server 的 auto_approve 标记。"""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(mcp_servers)").fetchall()}
    if "auto_approve" not in existing:
        conn.execute("ALTER TABLE mcp_servers ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0")


def _iso_to_ms(ts: str) -> int | None:
    try:
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _ensure_image_columns(conn: sqlite3.Connection) -> None:
    """老库补列：images 表加 created_at（毫秒时间戳）/ expired。

    CREATE TABLE IF NOT EXISTS 不会改已有表，所以单独 ALTER；存量行 created_at
    从所属消息 ts（ISO）回填成毫秒，老登记补上时间戳后才能走 24h 过期判龄。
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
    if "created_at" not in existing:
        conn.execute("ALTER TABLE images ADD COLUMN created_at INTEGER")
    if "expired" not in existing:
        conn.execute("ALTER TABLE images ADD COLUMN expired INTEGER NOT NULL DEFAULT 0")
    if "created_at" not in existing or "expired" not in existing:
        conn.commit()
    rows = conn.execute(
        "SELECT i.image_id, m.ts FROM images i JOIN messages m ON m.id = i.message_id "
        "WHERE i.created_at IS NULL"
    ).fetchall()
    for r in rows:
        ms = _iso_to_ms(r["ts"])
        if ms:
            conn.execute("UPDATE images SET created_at = ? WHERE image_id = ?", (ms, r["image_id"]))
    if rows:
        conn.commit()


_KEEPS_MCP_TOOLS = [
    {
        "tool_name": "keep_save_message",
        "description": "Save one of her inbound chat messages in WORDS / ALBUM. Context: auto (default) keeps the previous opposite-direction message; none saves without a quote; message uses exactly one context_message_id or context_match_text to quote an earlier message in the same conversation. Ambiguous context text is rejected. Existing keeps retain their quote; use keep_clear_context to remove it.",
        "input_schema": {"type": "object", "properties": {
            "message_id": {"type": "integer", "description": "Relay message id."},
            "match_text": {"type": "string", "description": "Exact substring in one of her recent 200 messages; returns candidates if ambiguous."},
            "seg": {"type": "integer", "description": "Optional bubble segment seq."},
            "image_id": {"type": "string", "description": "Optional id of one image attached to the message."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional labels."},
            "context": {"type": "string", "enum": ["auto", "none", "message"], "default": "auto"},
            "context_message_id": {"type": "integer", "description": "Earlier relay message id, with context=message."},
            "context_match_text": {"type": "string", "description": "Unique exact substring of an earlier message in the same conversation, with context=message."},
        }, "anyOf": [{"required": ["message_id"]}, {"required": ["match_text"]}]},
    },
    {
        "tool_name": "keep_list",
        "description": "List shared WORDS / ALBUM items by collector, label, or message date.",
        "input_schema": {"type": "object", "properties": {
            "saved_by": {"type": "string", "enum": ["all", "her", "Vale"]},
            "tag": {"type": "string"}, "since": {"type": "string"}, "until": {"type": "string"},
            "view": {"type": "string", "enum": ["all", "words", "album"]},
        }},
    },
    {
        "tool_name": "keep_add_note",
        "description": "Add a note to one shared WORDS / ALBUM item as Vale.",
        "input_schema": {"type": "object", "properties": {
            "keep_id": {"type": "integer"}, "content": {"type": "string"},
        }, "required": ["keep_id", "content"]},
    },
    {
        "tool_name": "keep_list_notes",
        "description": "List notes attached to one shared WORDS / ALBUM item.",
        "input_schema": {"type": "object", "properties": {"keep_id": {"type": "integer"}}, "required": ["keep_id"]},
    },
    {
        "tool_name": "keep_delete",
        "description": "Delete a WORDS / ALBUM item saved by Vale.",
        "input_schema": {"type": "object", "properties": {"keep_id": {"type": "integer"}}, "required": ["keep_id"]},
    },
    {
        "tool_name": "keep_delete_note",
        "description": "Delete a note written by Vale.",
        "input_schema": {"type": "object", "properties": {"note_id": {"type": "integer"}}, "required": ["note_id"]},
    },
    {
        "tool_name": "keep_send_image",
        "description": "Return a delivery marker for one image in a saved WORDS / ALBUM item.",
        "input_schema": {"type": "object", "properties": {
            "keep_id": {"type": "integer"}, "image_index": {"type": "integer", "description": "Zero-based image index; defaults to the first image."},
        }, "required": ["keep_id"]},
    },
    {
        "tool_name": "keep_clear_context",
        "description": "Clear only the quote context of a shared WORDS / ALBUM item, saved by either participant. Text, media, labels, and notes stay unchanged.",
        "input_schema": {"type": "object", "properties": {"keep_id": {"type": "integer"}}, "required": ["keep_id"]},
    },
]


def _ensure_keeps_mcp(conn: sqlite3.Connection) -> None:
    now = now_iso()
    row = conn.execute("SELECT token FROM mcp_servers WHERE id = ?", (KEEPS_MCP_ID,)).fetchone()
    token = str(row["token"] or "") if row else ""
    if not token:
        token = secrets.token_urlsafe(32)
    internal_url = f"http://127.0.0.1:{PORT}/internal/keeps/mcp"
    conn.execute(
        "INSERT INTO mcp_servers (id, name, description, url, transport, token, headers, enabled, auto_approve, tools_count, tools_synced_at, last_error, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'streamable-http', ?, '{}', 1, 0, ?, ?, '', ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, url=excluded.url, "
        "transport=excluded.transport, enabled=1, tools_count=excluded.tools_count, tools_synced_at=excluded.tools_synced_at, updated_at=excluded.updated_at",
        (KEEPS_MCP_ID, "WORDS / ALBUM", "Relay-owned shared keeps", internal_url, token,
         len(_KEEPS_MCP_TOOLS), now, now, now),
    )
    for tool in _KEEPS_MCP_TOOLS:
        conn.execute(
            "INSERT INTO mcp_tools (server_id, tool_name, description, input_schema, enabled) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(server_id, tool_name) DO UPDATE SET description=excluded.description, input_schema=excluded.input_schema",
            (KEEPS_MCP_ID, tool["tool_name"], tool["description"],
             json.dumps(tool["input_schema"], ensure_ascii=False)),
        )


def init_db() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    KEEP_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                direction TEXT NOT NULL,   -- 'in' (human -> AI) | 'out' (AI -> human)
                kind      TEXT NOT NULL,   -- 'user' | 'reply' | 'thinking' | 'voice' | 'call' | ...
                text      TEXT NOT NULL,
                meta      TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_documents (
                doc_key    TEXT PRIMARY KEY,
                data_json  TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_calendar_model_write_usage (
                local_day TEXT PRIMARY KEY,
                writes    INTEGER NOT NULL CHECK (writes >= 0)
            )
            """
        )
        ensure_ledger_schema(conn)
        ensure_command_schema(conn)
        ensure_letters_schema(conn)
        command_meta_backfill_count = backfill_command_message_meta(conn)
        if command_meta_backfill_count:
            log.info(
                "[command-card] chat message meta backfilled count=%d",
                command_meta_backfill_count,
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                p256dh   TEXT NOT NULL,
                auth     TEXT NOT NULL,
                ua       TEXT,
                created  TEXT NOT NULL,
                last_ok  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stations (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                base_url      TEXT NOT NULL,
                api_key       TEXT NOT NULL DEFAULT '',
                models        TEXT NOT NULL DEFAULT '',
                default_model TEXT NOT NULL DEFAULT '',
                kind          TEXT NOT NULL DEFAULT 'chat',
                active        INTEGER NOT NULL DEFAULT 0,
                protocol      TEXT NOT NULL DEFAULT 'openai',
                active_model  TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
            """
        )
        _ensure_station_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_servers (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                url             TEXT NOT NULL,
                transport       TEXT NOT NULL DEFAULT 'streamable-http',
                token           TEXT NOT NULL DEFAULT '',
                headers         TEXT NOT NULL DEFAULT '{}',   -- JSON 字符串
                enabled         INTEGER NOT NULL DEFAULT 1,
                auto_approve    INTEGER NOT NULL DEFAULT 0,
                tools_count     INTEGER NOT NULL DEFAULT 0,
                tools_synced_at TEXT,                          -- NULL = 从未同步
                last_error      TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        _ensure_mcp_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_tools (
                server_id    TEXT NOT NULL,
                tool_name    TEXT NOT NULL,
                description  TEXT NOT NULL DEFAULT '',
                input_schema TEXT NOT NULL DEFAULT '{}',       -- JSON 字符串
                enabled      INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (server_id, tool_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id     INTEGER NOT NULL,        -- relay.db 里的消息 id
                session_id TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL,           -- user | assistant
                text       TEXT NOT NULL,           -- 存一份快照：消息可能被删或被重 roll 替换，收藏留住当时那句话
                created_at TEXT NOT NULL,
                UNIQUE(msg_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites_voice (
                turn_id    INTEGER PRIMARY KEY,     -- OB conversation_turns.id（收藏锚点，唯一；别拿 msg_id 当主键）
                msg_id     INTEGER NOT NULL,        -- relay messages.id（语音所在的回复行，拷文件/排查用）
                segments   TEXT NOT NULL,           -- JSON：meta.segments 全量按原顺序，voice.url 已改指 kept/（拷失败的 url 置 null 前端降级文字）
                saved_by   TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                image_id   TEXT PRIMARY KEY,        -- 图片标识 = 上传文件名（att-xxx.jpg）
                message_id INTEGER NOT NULL,        -- 所属消息 id
                local_path TEXT NOT NULL            -- /uploads/... 相对路径
            )
            """
        )
        # Normalized Agent SDK envelopes are an append-only timeline that is
        # intentionally separate from legacy chat messages. Never fold these
        # rows into messages: old reply/thinking/act delivery stays untouched.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                run_id     TEXT NOT NULL,
                sequence   INTEGER NOT NULL,
                event_id   TEXT NOT NULL,
                turn_id    TEXT,
                type       TEXT NOT NULL,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, sequence),
                UNIQUE(run_id, event_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_events_run_sequence "
            "ON agent_events(run_id, sequence)"
        )
        _ensure_image_columns(conn)
        keeps_store.ensure_schema(conn)
        _ensure_keeps_mcp(conn)
        conn.commit()


def _assign_image_ids(attachments: list) -> None:
    """给每个图片附件就地补 image_id（= 上传文件名）。随 meta 一起落库，
    loop 拿它给历史消息打 [IMAGE:<id>] 占位，不再每轮重传历史图。
    """
    for a in attachments or []:
        if not isinstance(a, dict) or a.get("kind") != "image":
            continue
        url = str(a.get("url") or "").strip()
        if not url:
            continue
        if not a.get("image_id"):
            a["image_id"] = url.rsplit("/", 1)[-1] or url


# ---------------------------------------------------------------------------
# 图片 24h 过期：懒删本地文件 + 标注 expired（前端渲灰块）
# ---------------------------------------------------------------------------
_image_status: dict[str, bool] = {}   # image_id -> 已过期？进程内缓存，60s 刷新一次
_image_status_ts = 0.0


def _sweep_expired_images() -> dict[str, bool]:
    """懒删：扫 images 表，超过 TTL 或已标 expired 的行删掉本地文件并标 expired=1。

    进程内缓存 60s，避免每个请求都全表扫。幂等（expired 判断 + unlink missing_ok），
    并发重复调用最多浪费一次 unlink 不报错。行保留（expired=1）——前端渲染历史时
    知道「这里曾有张图」，显示灰块而不是直接消失。返回 {image_id: 过期?} 全量映射。
    """
    global _image_status, _image_status_ts
    now = time.time()
    if now - _image_status_ts < 60:
        return _image_status
    status: dict[str, bool] = {}
    to_expire: list[tuple[str, str]] = []
    with db() as conn:
        rows = conn.execute(
            "SELECT image_id, local_path, created_at, expired FROM images"
        ).fetchall()
        now_ms = int(now * 1000)
        for r in rows:
            exp = bool(r["expired"])
            if not exp and r["created_at"]:
                try:
                    exp = now_ms - int(r["created_at"]) > IMAGE_TTL_MS
                except (TypeError, ValueError):
                    exp = False
            if exp:
                status[r["image_id"]] = True
                if not r["expired"]:
                    to_expire.append((r["image_id"], str(r["local_path"] or "")))
        for iid, lp in to_expire:
            name = lp.rstrip("/").rsplit("/", 1)[-1]   # /uploads/att-x.jpg → att-x.jpg
            if name:
                try:
                    (UPLOAD_DIR / name).unlink(missing_ok=True)
                except OSError:
                    pass
            conn.execute("UPDATE images SET expired = 1 WHERE image_id = ?", (iid,))
        if to_expire:
            conn.commit()
    _image_status = status
    _image_status_ts = now
    return status


def annotate_image_expiry(meta: dict) -> dict:
    """给 meta.attachments 的图片就地标 expired=1（只加标记，不改其它字段）。"""
    atts = meta.get("attachments") if isinstance(meta.get("attachments"), list) else []
    if not atts:
        return meta
    status = _sweep_expired_images()
    for a in atts:
        if isinstance(a, dict) and a.get("kind") == "image":
            iid = str(a.get("image_id") or "").strip() or str(a.get("url") or "").rsplit("/", 1)[-1]
            if iid and status.get(iid):
                a["expired"] = 1
    return meta


def save_message(direction: str, kind: str, text: str, meta: dict) -> dict:
    ts = meta.get("ts") or now_iso()
    attachments = meta.get("attachments") if isinstance(meta.get("attachments"), list) else []
    _assign_image_ids(attachments)
    # in 方向的对话行（user/voice）永远参与上下文：variant_active 只有多版本
    # reroll（out 行）才会置 0，in 行不打标会被 api_loop relay_rows 的
    # va=1 过滤整段滤出历史。setdefault：调用方显式传值时尊重之。
    if direction == "in" and kind in ("user", "voice"):
        meta.setdefault("variant_active", 1)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO messages (ts, direction, kind, text, meta) VALUES (?,?,?,?,?)",
            (ts, direction, kind, text, json.dumps(meta, ensure_ascii=False)),
        )
        mid = cur.lastrowid
        # 图片登记进 images 表（image_id / message_id / local_path / created_at）；
        # 同图重复发更新所属但不动 created_at（保留原始创建时间 → 24h 判龄稳定）。
        created_ms = int(time.time() * 1000)
        for a in attachments:
            if isinstance(a, dict) and a.get("kind") == "image" and a.get("image_id") and a.get("url"):
                conn.execute(
                    "INSERT INTO images (image_id, message_id, local_path, created_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(image_id) DO UPDATE SET message_id=excluded.message_id, local_path=excluded.local_path",
                    (a["image_id"], mid, a["url"], created_ms),
                )
        conn.commit()
    return {"id": mid, "ts": ts, "direction": direction, "kind": kind, "text": text, "meta": meta}


def _local_message_active(message_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT direction,kind,meta FROM messages WHERE id=?", (message_id,)).fetchone()
    if not row or (row["direction"], row["kind"]) not in (("in", "user"), ("out", "reply")):
        return False
    meta = json.loads(row["meta"] or "{}")
    return str(meta.get("variant_active", 1)) not in ("0", "False", "false")


async def _queue_local_message(msg: dict) -> None:
    if True:
        return
    try:
        source = None
        source_id = str((msg.get("meta") or {}).get("source_id") or "")
        if msg.get("direction") == "out" and source_id.isdigit():
            with db() as conn:
                row = conn.execute("SELECT kind,meta FROM messages WHERE id=?", (int(source_id),)).fetchone()
            if row:
                source = {"kind": row["kind"], "meta": json.loads(row["meta"] or "{}")}
        reply_blocks = None
        if msg.get("direction") == "out" and msg.get("kind") == "reply":
            remaining, reply_blocks = _split_blocks(str(msg.get("text") or ""), final=True)
            if remaining.strip():
                reply_blocks.append(remaining)
        event = raw_event(msg, ClaudeSDKProvider._session_key, source, reply_blocks=reply_blocks)
        if event is not None:
            await _local_outbox.queue(event)
    except Exception as exc:
        log.error("[local-raw] snapshot_failed message_id=%s type=%s", msg.get("id"), type(exc).__name__)


THINK_BLOCK_RE = re.compile(r"<(think|thinking)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
THINK_OPEN_RE = re.compile(r"<(think|thinking)>(.*)$", re.IGNORECASE | re.DOTALL)


def split_think_blocks(text: str):
    """Split a raw reply into ordered (kind, text) segments.

    Models emit reasoning inline as <think>...</think>. The PWA renders a
    collapsed 'thinking' row for kind == 'thinking', so reasoning has to live
    in its own message instead of leaking into the reply bubble.
    Returns [] when there is nothing worth splitting, so callers keep the
    original text untouched.
    """
    raw = text or ""
    if "<think" not in raw.lower():
        return []

    segments = []
    cursor = 0
    for match in THINK_BLOCK_RE.finditer(raw):
        before = raw[cursor:match.start()].strip()
        if before:
            segments.append(("reply", before))
        thought = (match.group(2) or "").strip()
        if thought:
            segments.append(("thinking", thought))
        cursor = match.end()

    tail = raw[cursor:]
    # An unterminated <think> means the stream cut off mid-thought: keep it as
    # thinking rather than dumping a raw tag into the reply bubble.
    open_tail = THINK_OPEN_RE.search(tail)
    if open_tail:
        before = tail[:open_tail.start()].strip()
        if before:
            segments.append(("reply", before))
        thought = (open_tail.group(2) or "").strip()
        if thought:
            segments.append(("thinking", thought))
    else:
        tail = tail.strip()
        if tail:
            segments.append(("reply", tail))

    return segments if any(kind == "thinking" for kind, _ in segments) else []


def set_reaction(message_id, who, emoji):
    # Set/clear one party's reaction on an existing message.
    # Returns the message's reactions dict, or None if the target doesn't exist.
    with db() as conn:
        row = conn.execute("SELECT meta FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            return None
        meta = json.loads(row["meta"] or "{}")
        reactions = meta.get("reactions") or {}
        if emoji:
            reactions[who] = emoji
        else:
            reactions.pop(who, None)
        if reactions:
            meta["reactions"] = reactions
        else:
            meta.pop("reactions", None)
        conn.execute(
            "UPDATE messages SET meta = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), message_id),
        )
        conn.commit()
    return reactions


def history(since: int, limit: int, latest: bool = False) -> list:
    with db() as conn:
        if latest:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            rows = list(reversed(rows))   # 返回时仍按 id 升序，前端不用改
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id ASC LIMIT ?",
                (since, limit),
            ).fetchall()
    return rows_to_messages(rows)


def history_for_session(session_id: str, since: int, limit: int, latest: bool = False, before: int = 0) -> list:
    session_id = (session_id or "").strip()
    if not session_id:
        return history(since, limit, latest)
    # api_session is a short-lived PWA/SDK worker marker, never a desktop
    # conversation boundary.  Desktop history must survive worker respawn.
    desktop_line = is_desktop_session(session_id)
    with db() as conn:
        if desktop_line:
            clause = desktop_line_clause()
            if latest:
                rows = conn.execute(f"SELECT * FROM messages WHERE {clause} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                return rows_to_messages(reversed(rows))
            if before:
                rows = conn.execute(
                    f"SELECT * FROM messages WHERE id < ? AND {clause} ORDER BY id DESC LIMIT ?",
                    (before, limit),
                ).fetchall()
                return rows_to_messages(reversed(rows))
            rows = conn.execute(f"SELECT * FROM messages WHERE id > ? AND {clause} ORDER BY id ASC LIMIT ?", (since, limit)).fetchall()
            return rows_to_messages(rows)
        if before:
            if session_id == "__legacy__":
                rows = conn.execute(
                    "SELECT * FROM messages WHERE id < ? "
                    "AND (json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '') "
                    "ORDER BY id DESC LIMIT ?",
                    (before, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE id < ? AND json_extract(meta, '$.api_session') = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (before, session_id, limit),
                ).fetchall()
            return rows_to_messages(reversed(rows))
        if latest:
            if session_id == "__legacy__":
                rows = conn.execute(
                    "SELECT * FROM messages "
                    "WHERE (json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '') "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages "
                    "WHERE json_extract(meta, '$.api_session') = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            rows = list(reversed(rows))   # 返回时仍按 id 升序，前端不用改
        elif session_id == "__legacy__":
            rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE id > ? AND (json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '') "
                "ORDER BY id ASC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE id > ? AND json_extract(meta, '$.api_session') = ? "
                "ORDER BY id ASC LIMIT ?",
                (since, session_id, limit),
            ).fetchall()
    return rows_to_messages(rows)


def inbound_history(since: int, limit: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE id > ? AND direction = 'in' ORDER BY id ASC LIMIT ?",
            (since, limit),
        ).fetchall()
    return rows_to_messages(rows)


def rows_to_messages(rows) -> list:
    return [
        {
            "id": r["id"], "ts": r["ts"], "direction": r["direction"],
            "kind": r["kind"], "text": r["text"], "meta": json.loads(r["meta"] or "{}"),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# web push — subscription storage + send
# ---------------------------------------------------------------------------

def save_subscription(endpoint: str, p256dh: str, auth: str, ua: str = "") -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, ua, created, last_ok)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, ua=excluded.ua
            """,
            (endpoint, p256dh, auth, ua, now_iso(), None),
        )
        conn.commit()


def delete_subscription(endpoint: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()


def list_subscriptions() -> list:
    with db() as conn:
        rows = conn.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions").fetchall()
    return [{"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in rows]


def mark_subscription_ok(endpoint: str) -> None:
    with db() as conn:
        conn.execute("UPDATE push_subscriptions SET last_ok = ? WHERE endpoint = ?", (now_iso(), endpoint))
        conn.commit()


def _send_one_push(sub: dict, data: str):
    """Blocking single send (run in a thread). Returns (endpoint, status): 0=ok, 404/410=dead, else=transient."""
    if webpush is None:
        return sub["endpoint"], -1
    try:
        webpush(
            subscription_info=sub,
            data=data,
            vapid_private_key=VAPID_PRIVATE_PEM,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
        )
        return sub["endpoint"], 0
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        return sub["endpoint"], code
    except Exception:
        return sub["endpoint"], -1


async def push_to_all(payload: dict) -> dict:
    """Best-effort fan-out to all subscriptions; never raises. 404/410 prunes dead subs."""
    if webpush is None or not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_PEM:
        return {"sent": 0, "dead": 0, "skipped": "not_configured"}
    subs = list_subscriptions()
    if not subs:
        return {"sent": 0, "dead": 0}
    data = json.dumps(payload, ensure_ascii=False)
    results = await asyncio.gather(*[asyncio.to_thread(_send_one_push, s, data) for s in subs])
    sent = dead = 0
    for endpoint, status in results:
        if status == 0:
            sent += 1
            mark_subscription_ok(endpoint)
        elif status in (404, 410):
            delete_subscription(endpoint)
            dead += 1
    return {"sent": sent, "dead": dead}


_PUSH_TAG_RE = re.compile(r"<[^>]+>")


def notification_from_message(msg: dict) -> dict:
    raw = (msg.get("text") or "").strip()
    # Drop translations before flattening lines or removing tone markers.
    body = re.sub(r"(?im)^.*\[zh\].*(?:\n|$)", "", raw)
    body = _PUSH_TAG_RE.sub("", body)
    body = re.sub(r"(?m)^\s*(?:[-*+]|\d+[.)])[ \t]+", "", body)
    body = re.sub(r"\*\*|__|`+", "", body)
    body = re.sub(r":[a-zA-Z0-9_]+:", "", body)
    body = VOICE_MARK_RE.sub("", body).strip()   # 语音整条级标记：通知正文剥掉 [voice]
    body = BR_MARK_RE.sub(" ", body).strip()     # [br] 分隔标记剥掉（通知单行，标记不漏出）
    body = re.sub(r"\[[a-zA-Z][a-zA-Z -]{0,30}\]", "", body)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > PUSH_PREVIEW_CHARS:
        body = body[:PUSH_PREVIEW_CHARS].rstrip() + "…"
    if not body:
        body = f"{AI_NAME}给你发来一条消息"
    return {"title": AI_NAME, "body": body, "url": APP_PATH, "id": msg.get("id"), "ts": msg.get("ts")}


# ---------------------------------------------------------------------------
# pub/sub — one asyncio.Queue per connected SSE client
# ---------------------------------------------------------------------------

plugin_subs: set[asyncio.Queue] = set()  # AI side    (GET /channel/in)
app_subs: set[asyncio.Queue] = set()     # human side (GET /app/stream)
keep_activity_subs: set[asyncio.Queue] = set()  # preview page only
_keep_event_loop: asyncio.AbstractEventLoop | None = None
stream_drafts: dict[tuple[str, str], dict] = {}
# State belongs to a live SSE queue, never to presence/last-contact or the DB.
app_sub_visibility: dict[asyncio.Queue, dict] = {}


def has_visible_app_subscribers() -> bool:
    # Unknown (old frontend) connections retain the previous suppression behavior.
    return any(app_sub_visibility.get(q, {}).get("visible", True) for q in app_subs)


def app_visibility_state(client_id, visible, sequence) -> dict:
    if (not isinstance(client_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id)
            or not isinstance(visible, bool)
            or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0):
        raise HTTPException(status_code=400, detail="invalid visibility state")
    return {"client_id": client_id, "visible": visible, "sequence": sequence}


async def broadcast(subs: set, payload: dict) -> None:
    from backend.demo_visits import visitor,queue_visitors
    for q in list(subs):
        if queue_visitors.get(q, '') != visitor.get():continue
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            subs.discard(q)  # slow/dead consumer — drop it
            if subs is app_subs:
                app_sub_visibility.pop(q, None)


def _broadcast_keep_from_mcp(msg: dict) -> None:
    """FastMCP sync tools run in a worker thread; publish on the relay loop."""
    loop = _keep_event_loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(lambda: asyncio.create_task(broadcast(keep_activity_subs, app_payload(msg))))


def app_payload(msg: dict) -> dict:
    """Shape the PWA renders: from = 'human' | 'ai', plus kind for styling."""
    meta = msg.get("meta") or {}
    annotate_image_expiry(meta)   # 历史图超 24h → 附件标 expired，前端渲灰块
    return {
        "id": msg["id"], "ts": msg["ts"],
        "from": "human" if msg["direction"] == "in" else "ai",
        "kind": msg["kind"], "text": msg["text"], "meta": meta,
    }


# ---------------------------------------------------------------------------
# turn_id 对齐 —— 聊天行的 ☆ 收藏要的是 OB gateway 的 conversation_turns.id，
# 不是 relay messages.id（两套自增互不相干，拿错必 404「这条收不到」）。
# gateway 没有按会话拉 turns 的 HTTP 接口，relay 只读挂同机的 gateway_state.db
# 自己对齐。对齐依据（08-29 实测）：一次往返 = 一轮；turn.created_at 恒比回复
# 落库晚 1~10s；turn.assistant_text == 回复文本把换行折叠成空格。
# 对不上的行不带 turn_id → 前端隐藏那条的收藏入口（turn 表 gateway 只留最近 500 轮）。
# ---------------------------------------------------------------------------

OB_GATEWAY_DB = Path(DB_PATH)
GW_TURNS_TTL = 30.0            # turns 快照缓存秒数；/app/history 分页拉取频繁，不能每请求开库
GW_TURN_AFTER_S = -15.0        # turn.created_at 允许比回复早的秒数（实测恒为正，留时钟余量）
GW_TURN_WITHIN_S = 120.0       # turn.created_at 比回复晚多少秒内算同一轮
GW_ANCHOR_WITHIN_S = 600.0     # user 行认领其后第一条回复锚点的上限
_gw_turns_cache: dict = {"at": 0.0, "rows": [], "ats": []}
_gw_turns_warned = False


def _norm_snap_text(t) -> str:
    """gateway 快照把正文换行折叠成了空格；对齐前两边都归一成单空格再比。"""
    return re.sub(r"\s+", " ", str(t or "")).strip()


def _parse_iso_utc(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def gateway_turns() -> list:
    return []  # No external archive in the replay copy.


def _match_reply_turn(reply_at, reply_norm: str, turns: list, ats: list, used: set):
    """回复行 → turn：时间窗（二分定位）内未占用的轮里，先挑 assistant_text
    全等的，再挑时间最近的。对不上返回 None（这一行不带 turn_id）。"""
    lo = bisect.bisect_left(ats, reply_at + timedelta(seconds=GW_TURN_AFTER_S))
    hi = bisect.bisect_right(ats, reply_at + timedelta(seconds=GW_TURN_WITHIN_S))
    best = None
    for t in turns[lo:hi]:
        if t["id"] in used:
            continue
        score = (0 if (reply_norm and t["assist"] == reply_norm) else 1,
                 abs((t["at"] - reply_at).total_seconds()), t["id"])
        if best is None or score < best[0]:
            best = (score, t)
    return best[1] if best else None


def attach_turn_ids(payloads: list, session_id: str = "", batch_tail_id: int = 0) -> None:
    """给 /app/history 的每条消息就地补 turn_id。

    reply 行直接对齐 turn；user/thinking 行认领所属往返的回复锚点（合并发送的
    多条 user 同属一轮）。批量边界可能把 user 行和它的回复劈进两批，借批后几条
    reply 行当额外锚点补上。锚点只往前认领，绝不跨轮错配。"""
    turns = gateway_turns()
    if not turns or not payloads:
        return
    ats = _gw_turns_cache["ats"] or [t["at"] for t in turns]
    used: set = set()
    anchors: list = []                          # (reply_at, turn_id)，按 at 升序

    for p in payloads:
        if p.get("from") == "ai" and p.get("kind") == "reply":
            at = _parse_iso_utc(p.get("ts"))
            if at is None:
                continue
            t = _match_reply_turn(at, _norm_snap_text(p.get("text")), turns, ats, used)
            if t:
                p["turn_id"] = t["id"]
                used.add(t["id"])
                anchors.append((at, t["id"]))
    if batch_tail_id:
        # 批尾 user 行的回复可能落在下一批 → 借批后几条 reply 行补锚点
        cond, args = "", [batch_tail_id]
        if (session_id or "").strip():
            cond = " AND json_extract(meta, '$.api_session') = ?"
            args.append(session_id.strip())
        try:
            with db() as conn:
                extra = conn.execute(
                    "SELECT ts, text FROM messages WHERE id > ? AND kind = 'reply'" + cond
                    + " ORDER BY id ASC LIMIT 8", args
                ).fetchall()
        except sqlite3.Error:
            extra = []                      # busy / 锁冲突：锚点缺失只是这条 user 行暂无 turn_id
        for r in extra:
            at = _parse_iso_utc(r["ts"])
            if at is None:
                continue
            t = _match_reply_turn(at, _norm_snap_text(r["text"]), turns, ats, used)
            if t:
                anchors.append((at, t["id"]))
    if not anchors:
        return
    anchor_ats = [a[0] for a in anchors]
    for p in payloads:
        kind = p.get("kind")
        at = _parse_iso_utc(p.get("ts"))
        if at is None:
            continue
        if p.get("from") == "ai":
            if kind != "thinking":
                continue                        # reply 已盖章；act 等环境行不收藏
            # thinking 行时间贴着自己的回复：取绝对距离最近的锚点
            i = bisect.bisect_left(anchor_ats, at)
            cands = [anchors[max(0, i - 1)], anchors[min(i, len(anchors) - 1)]]
            near = min(cands, key=lambda a: (abs((a[0] - at).total_seconds()), a[1]))
            if abs((near[0] - at).total_seconds()) <= GW_TURN_WITHIN_S:
                p["turn_id"] = near[1]
        elif p.get("from") == "human" and kind == "user":
            # user 行认领它后面第一条回复的轮（回复永远在消息之后，不往前认）
            i = bisect.bisect_left(anchor_ats, at + timedelta(seconds=-2))
            if i >= len(anchors):
                continue
            near = anchors[i]
            if (near[0] - at).total_seconds() <= GW_ANCHOR_WITHIN_S:
                p["turn_id"] = near[1]


def plugin_payload(msg: dict) -> dict:
    meta = msg.get("meta") or {}
    return {
        "id": msg["id"],
        "content": msg["text"],
        "user": meta.get("user") or "human",
        "ts": msg["ts"],
        "attachments": meta.get("attachments") or [],
    }


_brain_router: ProviderRouter | None = None


def brain_router() -> ProviderRouter:
    if _brain_router is None:
        raise RuntimeError("brain provider router is not initialized")
    return _brain_router


def brain_target() -> str:
    return brain_router().current_target()


def _forward_to_loop_sync(msg: dict) -> None:
    meta = msg.get("meta") or {}
    # context 段：转写完成 → loop ingest 即将发出。gateway 请求由 loop 再转一手，
    # relay 看不到，那部分归进 model_first_delta 段（可用 loop/gateway journal 对账拆分）。
    _vl("context", msg.get("id"), turn_id=(meta.get("turn_id") or "")[:60])
    data = json.dumps({
        "id": msg.get("id"),
        "text": msg.get("text", ""),
        "session_id": meta.get("api_session") or "",
        "attachments": meta.get("attachments") or [],
        # 语音关时 api_loop 不注入语音规则（AI 不知道 [voice] 就不会产出 voice 块）
        "voice_enabled": voice_mode_on,
        # 贴纸门线 = sticker_on and segment_on（贴纸须独立成段）→ loop 据此注入贴纸规则
        "sticker_enabled": sticker_mode_on and segment_mode_on,
        # 合并队列顺序：图在前/文在前 → loop build_messages 按它排图块与文块
        "images_first": meta.get("images_first", True),
        # 引用带原文：新消息只落 meta.quotes；旧 meta.quote 由统一读侧 helper 兼容
        "quotes": _quote_items_from_meta(meta),
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        LOOP_INGEST_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=300).read()


async def forward_to_loop(msg: dict) -> None:
    try:
        await asyncio.to_thread(_forward_to_loop_sync, msg)
    except Exception as exc:
        print(f"[loop] forward failed: {type(exc).__name__}: {exc}")


async def _handle_sdk_provider_event(event: dict) -> None:
    """Map the SDK provider stream into the relay's existing delta pipeline."""
    kind = event.get("type")
    if kind == "agent_event":
        envelope = event.get("event")
        if not isinstance(envelope, dict):
            print("[agent-event] WARNING ignored malformed provider envelope", flush=True)
            return
        try:
            run_id = envelope["runId"]
            event_id = envelope["eventId"]
            sequence = envelope["sequence"]
            event_type = envelope["type"]
            if (
                not isinstance(run_id, str) or not run_id
                or not isinstance(event_id, str) or not event_id
                or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
                or not isinstance(event_type, str) or not event_type
            ):
                raise ValueError("invalid envelope identity")
            payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            with db() as conn:
                # (runId, eventId) is the replay/idempotency key. A separate
                # (runId, sequence) constraint protects chronology.
                cursor = conn.execute(
                    "INSERT INTO agent_events "
                    "(run_id, sequence, event_id, turn_id, type, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id, event_id) DO NOTHING",
                    (run_id, sequence, event_id, str(envelope.get("turnId") or "") or None,
                     event_type, payload, now_iso()),
                )
                inserted = cursor.rowcount == 1
            if inserted:
                # New SSE name; legacy thinking_delta / act / reply_delta are untouched.
                await broadcast(app_subs, {"type": "agent_event", "event": envelope})
            return
        except Exception as exc:
            # Event storage is observational until the new UI consumes it.
            # It must never turn a healthy legacy desktop reply into an error.
            print(f"[agent-event] WARNING persist failed: {type(exc).__name__}: {exc}", flush=True)
            return
    common = {
        "stream_id": event.get("stream_id"),
        "source_id": event.get("source_id"),
        "api_session": event.get("api_session") or "",
        "provider": "desktop",
    }
    if kind == "text_delta":
        payload = {
            **common,
            "text": event.get("text") or "",
            "done": False,
        }
        if event.get("first_delta_ms") is not None:
            payload["sdk_first_delta_ms"] = event.get("first_delta_ms")
        await handle_stream_delta(
            "reply_delta",
            payload,
        )
        return
    if kind == "thinking_delta":
        await handle_stream_delta("thinking_delta", {**common, "text": event.get("text") or "", "done": False})
        return
    if kind == "act":
        meta = {**common, "glyph": event.get("glyph") or "memory", "steps": event.get("steps") if isinstance(event.get("steps"), list) else []}
        msg = save_message("out", "act", str(event.get("text") or "做了点什么"), meta)
        await broadcast(app_subs, app_payload(msg))
        act_id = msg["id"]
        stream_id = common["stream_id"] or "-"
        print(
            f"[sdk:act] id={act_id} stream={stream_id} subscribers={len(app_subs)}",
            flush=True,
        )
        return
    if kind == "done":
        done_payload = {
            **common,
            "text": "",
            "done": True,
            "sdk_usage": event.get("usage"),
            "sdk_duration_ms": event.get("duration_ms"),
        }
        if event.get("gateway_record_error"):
            done_payload["gateway_record_error"] = event.get("gateway_record_error")
        await handle_stream_delta(
            "reply_delta",
            done_payload,
        )
        return
    if kind == "error":
        await broadcast(app_subs, {"type": "sdk_worker_crash", "stream_id": common["stream_id"], "attempt": event.get("attempt"), "bridge_pid": event.get("bridge_pid"), "partial_usage": event.get("partial_usage") or {"complete": False}, "terminal": event.get("terminal") is True, "replay_blocked": event.get("replay_blocked") is True, "message": str(event.get("message") or "Desktop 模式暂时不可用。")})
        visible = str(event.get("message") or "Desktop 模式暂时不可用。")
        await handle_stream_delta(
            "reply_delta",
            {**common, "text": visible, "done": False, "sdk_error": True},
        )
        await handle_stream_delta(
            "reply_delta",
            {**common, "text": "", "done": True, "sdk_error": True},
        )
        return
    if kind == "permission_request":
        await broadcast(
            app_subs,
            {
                "type": "sdk_permission_request",
                **common,
                "permission_id": event.get("permission_id"),
                "session_key": event.get("session_key"),
                "tool_name": event.get("tool_name"),
                "input": event.get("input") or {},
                "title": event.get("title") or "",
                "display_name": event.get("display_name") or "",
                "description": event.get("description") or "",
            },
        )
        return
    if kind == "rate_limit":
        snapshot = _save_sdk_rate_limit(event.get("payload"))
        if snapshot:
            await broadcast(app_subs, {"type": "sdk_rate_limit", "snapshot": snapshot})
            if _max_sdk_utilization(snapshot) > 0.95 and brain_target() == "desktop":
                brain_router().set_target("loop")
                await broadcast(
                    app_subs,
                    {
                        "type": "sdk_auto_fallback",
                        "message": "Claude 额度已超过 95%，下一轮已自动切到 API。",
                    },
                )
        return
    if kind == "context_usage":
        await broadcast(
            app_subs,
            {
                "type": "sdk_context",
                "session_key": event.get("session_key"),
                "usage": event.get("data") or {},
            },
        )
        return
    if kind == "compact":
        # worker 已打 [sdk:compact] WARNING 日志（压缩可见记录的 journal 侧）。
        await broadcast(
            app_subs,
            {
                "type": "sdk_compact",
                "session_key": event.get("session_key"),
                "trigger": event.get("trigger"),
                "pre_tokens": event.get("pre_tokens"),
                "post_tokens": event.get("post_tokens"),
                "duration_ms": event.get("duration_ms"),
            },
        )
        return


async def _dispatch_api_loop(msg: dict) -> None:
    # Preserve the existing non-blocking API behavior: /app/send responds after
    # the turn is scheduled, while this task owns the selected provider until
    # the loop request itself finishes.
    asyncio.create_task(forward_to_loop(msg))


from backend.replay_provider import ReplayProvider
_sdk_provider = ReplayProvider(_handle_sdk_provider_event)
_brain_router = ProviderRouter(BRAIN_FILE, {"replay": _sdk_provider}, default_target="replay")


async def dispatch_to_brain(msg: dict) -> str:
    # Desktop SDK 也经 gateway context 取 persona。开关只随当前 dispatch
    # 透传，不写回已落库的用户消息；API loop 仍由自己的 ingest payload 读取同一状态。
    meta = msg.get("meta")
    if isinstance(meta, dict):
        meta["sticker_mode_on"] = sticker_mode_on
        meta["segment_mode_on"] = segment_mode_on
    return await brain_router().dispatch(msg)


async def _dispatch_and_echo_incoming(msg: dict) -> dict:
    """Use the ordinary incoming-message provider, SSE echo, and typing path."""
    await dispatch_to_brain(msg)
    await broadcast(app_subs, app_payload(msg))
    await broadcast(app_subs, {"type": "typing", "active": True})
    return msg


def _save_sdk_rate_limit(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    # Agent SDK wraps quota data in rate_limit_info. Keep the direct form as a
    # compatibility fallback for the older bridge event shape.
    info = payload.get("rate_limit_info") or payload.get("rateLimitInfo") or payload
    if not isinstance(info, dict):
        info = {}
    windows = info.get("unifiedWindows")
    if not isinstance(windows, dict):
        windows = {}
    snapshot = {
        "status": str(info.get("status") or ""),
        "rate_limit_type": str(info.get("rateLimitType") or ""),
        "resets_at": info.get("resetsAt"),
        "windows": windows,
        "updated_at": now_iso(),
    }
    try:
        SDK_RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SDK_RATE_LIMIT_FILE.with_name(f".{SDK_RATE_LIMIT_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SDK_RATE_LIMIT_FILE)
    except OSError as exc:
        print(f"[sdk:rate_limit] persist failed: {type(exc).__name__}: {exc}", flush=True)
    return snapshot


def _load_sdk_rate_limit() -> dict:
    try:
        payload = json.loads(SDK_RATE_LIMIT_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _max_sdk_utilization(snapshot: dict) -> float:
    values = []
    for window in (snapshot.get("windows") or {}).values():
        if isinstance(window, dict):
            try:
                values.append(float(window.get("utilization") or 0))
            except (TypeError, ValueError):
                pass
    return max(values, default=0.0)


def prune_stream_drafts() -> None:
    now = datetime.now(timezone.utc).timestamp()
    stale = [k for k, v in stream_drafts.items() if now - float(v.get("updated_at") or 0) > STREAM_DRAFT_TTL]
    for k in stale:
        draft = stream_drafts.pop(k, None)
        if draft:
            _cleanup_stream_draft(k, draft)


async def handle_stream_delta(kind: str, body: dict) -> dict:
    base_kind = kind[:-6] if kind.endswith("_delta") else kind
    if base_kind not in ("thinking", "reply"):
        raise HTTPException(status_code=400, detail="unknown stream kind")
    stream_id = str(body.get("stream_id") or "").strip()
    if not stream_id:
        raise HTTPException(status_code=400, detail="stream_id required")

    done = bool(body.get("done"))
    chunk = str(body.get("text") or "")
    meta = {k: v for k, v in body.items() if k not in ("type", "text", "done", "final_text")}
    meta["stream_id"] = stream_id
    key = (stream_id, base_kind)
    prune_stream_drafts()

    now_ts = datetime.now(timezone.utc).timestamp()
    draft = stream_drafts.get(key)
    if not draft:
        draft = {"text": "", "meta": meta, "ts": now_iso(), "updated_at": now_ts,
                 "seg_buf": "", "segments": [], "seg_seq": 0,
                 "_t0": time.monotonic()}   # §PATCH8: 本流第一个 delta 的单调时钟, 用于思考链流式判读
        stream_drafts[key] = draft
        # model_first_delta 段：本流第一个可见 delta 到达（含 loop 转发 + gateway + 上游首字）。
        # thinking/reply 各记一条；非通话轮（source_id 不在 VL_T0）自动静默。
        _vl(f"{base_kind}_first_delta", meta.get("source_id"),
            stream_id=stream_id, chunk_bytes=len(chunk))
    draft["text"] += chunk
    if done and isinstance(body.get("final_text"), str):
        final_text = body.get("final_text") or ""
        if base_kind == "reply" and segment_mode_on and draft.get("tool_boundary_offsets"):
            # api_loop's final_text is assembled from the unmodified deltas.
            # Keep its thinking prefix / final suffix, but restore our stream boundaries.
            streamed = draft["text"]
            raw = streamed
            for offset in reversed(draft["tool_boundary_offsets"]):
                raw = raw[:offset] + raw[offset + 2:]
            start = final_text.rfind(raw.strip()) if raw.strip() else -1
            if start >= 0:
                final_text = final_text[:start] + streamed.strip() + final_text[start + len(raw.strip()):]
        draft["text"] = final_text
    draft["meta"].update(meta)
    draft["updated_at"] = now_ts

    # ── 分段开（segment_mode_on）：reply 流按双换行切成多条「消息」(block)，每条独立判语音/文字 ──
    # 流式攒 buffer，攒到一个双换行就切出一个完整 block 处理并推出去，不等整条流完（首泡优先）。
    # 每个 block：[voice] 首行 + voice_mode_on → 语音气泡（[br] 分条、参数块不可分割、超长保护），
    # 否则 → 文字气泡（[br] 分隔、400 字无换行兜底；标点自动分句已关，TASK-bubble-split-redo）。
    # 整块结构（列表/代码/表格/引用）内部的空行不切 block。整段逻辑用 per-stream 锁串行：
    # 合成期间上游 token 继续进 buffer，但不抢推序。
    if base_kind == "reply" and segment_mode_on:
        lock = voice_stream_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            if not draft.get("msg_id"):
                _ensure_stream_msg(stream_id, draft)
            if not done:
                draft["seg_buf"] = (draft.get("seg_buf") or "") + chunk
                await _process_buffered_blocks(stream_id, draft)
            else:
                result = await _finalize_stream_reply(stream_id, draft)
        if done:
            stream_drafts.pop(key, None)
            voice_stream_locks.pop(stream_id, None)
            return result
        return {"ok": True, "stream_id": stream_id, "draft": True}

    if not done:
        if base_kind == "thinking":
            # §PATCH8: 思考链流式判读——t 是相对本流第一个 delta 的累计时长, 看 chunk 是否逐条分散到达
            print(f"[thinking_delta] t={time.monotonic() - draft.get('_t0', time.monotonic()):.3f}s len={len(chunk)}")
        await broadcast(app_subs, {
            "type": kind,
            "stream_id": stream_id,
            "text": chunk,
            "done": False,
            "ts": draft["ts"],
            "api_session": draft["meta"].get("api_session") or "",
        })
        return {"ok": True, "stream_id": stream_id, "draft": True}

    text = draft.get("text") or ""
    stream_drafts.pop(key, None)
    if not text:
        return {"ok": True, "stream_id": stream_id, "saved": False}

    # Same split as the non-streaming path: a streamed reply can still carry
    # inline <think> blocks, so it has to be broken up before it lands in a bubble.
    segments = split_think_blocks(text) if base_kind == "reply" else []
    if not segments:
        segments = [(base_kind, text)]

    await broadcast(app_subs, {"type": "typing", "active": False})

    # 多版本标记：一次生成（同一个 stream）的 thinking + reply 片段共享同一个
    # reroll_group（=触发它的用户消息 id，loop 通过 source_id 送过来）。第一次生成
    # 时 variant_index=1、variant_active=1；没有 source_id 的历史来源不加标记，
    # 上下文过滤的 IS NULL 兜底管它们。
    _meta = dict(draft.get("meta") or {})
    with db() as conn:
        activity = activity_for_run(conn, run_id_for_stream(stream_id))
    if activity:
        _meta["activity"] = activity
    _group = str(_meta.get("source_id") or "").strip() or None
    saved = []
    for seg_kind, seg_text in segments:
        seg_meta = dict(_meta)
        if _group:
            seg_meta["reroll_group"] = _group
            seg_meta["variant_index"] = 1
            seg_meta["variant_active"] = 1
        msg = save_message("out", seg_kind, seg_text, seg_meta)
        saved.append(msg)
        await _queue_local_message(msg)
        await broadcast(app_subs, app_payload(msg))

    if activity:
        with db() as conn:
            persist_activity_for_stream(conn, stream_id, activity)
            conn.commit()

    if not has_visible_app_subscribers():
        for msg in saved:
            if msg["kind"] != "reply":
                continue
            try:
                await push_to_all(notification_from_message(msg))
            except Exception:
                pass
    return {"id": saved[-1]["id"], "ids": [m["id"] for m in saved], "stream_id": stream_id, "saved": True}


def loop_base_url() -> str:
    parsed = urllib.parse.urlparse(LOOP_INGEST_URL)
    if not parsed.scheme or not parsed.netloc:
        return "http://127.0.0.1:3020"
    return f"{parsed.scheme}://{parsed.netloc}"


def loop_json(path: str, method: str = "GET", body=None):
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(loop_base_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise HTTPException(status_code=exc.code, detail=detail)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"loop proxy error: {exc}")


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def clean_filename(name: str) -> str:
    name = Path(name or "file").name
    name = SAFE_NAME_RE.sub("_", name).strip("._") or "file"
    return name[:80]


def ext_for(name: str, mime: str) -> str:
    ext = Path(name).suffix.lower()
    # Display names can end in a date (for example 09.24); don't treat its numeric
    # suffix as a file extension, or FileResponse will serve the wrong MIME type.
    if ext and re.fullmatch(r"\.[A-Za-z][A-Za-z0-9]{0,7}", ext):
        return ext
    guessed = mimetypes.guess_extension((mime or "").split(";", 1)[0].strip())
    return guessed or ".bin"


_TEXT_ATTACHMENT_EXTENSIONS = {
    ".csv", ".json", ".log", ".md", ".text", ".txt", ".xml", ".yaml", ".yml",
}


def _attachment_is_text(name: str, mime: str) -> bool:
    normalized_mime = (mime or "").split(";", 1)[0].strip().lower()
    return (
        normalized_mime.startswith("text/")
        or normalized_mime in {"application/json", "application/xml", "application/yaml", "application/x-yaml"}
        or Path(name).suffix.lower() in _TEXT_ATTACHMENT_EXTENSIONS
    )


def _bounded_command_output(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stdout or "") + (result.stderr or "")
    if len(output.encode("utf-8", errors="replace")) > MAX_ATTACHMENT_COMMAND_OUTPUT_BYTES:
        return "skipped(command_output_limit)"
    if result.returncode:
        return "skipped(extraction_failed)"
    return ""


def _pdf_page_count(path: Path) -> int | None:
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if _bounded_command_output(result):
        return None
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout or "", flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def _preprocess_attachment(path: Path, name: str, mime: str) -> dict[str, object]:
    """Return only the safe text-readable representation for a non-image upload."""
    if _attachment_is_text(name, mime):
        return {"text_path": str(path.resolve())}
    if (mime or "").split(";", 1)[0].strip().lower() != "application/pdf" and path.suffix.lower() != ".pdf":
        return {}

    pages = _pdf_page_count(path)
    result: dict[str, object] = {}
    if pages is None:
        return {"text_status": "skipped(page_count_unavailable)"}
    result["pages"] = pages
    if pages > MAX_PDF_TEXT_PAGES:
        result["text_status"] = "skipped(over_page_limit)"
        return result

    text_path = path.with_suffix(".txt")
    try:
        extracted = subprocess.run(
            ["pdftotext", str(path), str(text_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        result["text_status"] = "skipped(timeout)"
        return result
    except OSError:
        result["text_status"] = "skipped(extraction_unavailable)"
        return result
    status = _bounded_command_output(extracted)
    if status:
        text_path.unlink(missing_ok=True)
        result["text_status"] = status
        return result
    try:
        if text_path.stat().st_size > MAX_ATTACHMENT_COMMAND_OUTPUT_BYTES:
            text_path.unlink(missing_ok=True)
            result["text_status"] = "skipped(command_output_limit)"
            return result
        text = text_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        result["text_status"] = "skipped(extraction_failed)"
        return result
    if not text:
        text_path.unlink(missing_ok=True)
        result["text_status"] = "empty(likely_scanned)"
        return result
    text_path.write_text(text[:MAX_ATTACHMENT_TEXT_CHARS], encoding="utf-8")
    result["text_path"] = str(text_path.resolve())
    return result


def _image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read common image dimensions from at most 64 KiB of headers; never raises."""
    try:
        with path.open("rb") as image_file:
            data = image_file.read(64 * 1024)
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
        elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
        elif data.startswith(b"\xff\xd8"):
            width, height = _jpeg_dimensions(data)
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            width, height = _webp_dimensions(data)
        else:
            return (None, None)
    except (OSError, struct.error, ValueError):
        return (None, None)
    return (width, height) if width is not None and height is not None and width > 0 and height > 0 else (None, None)


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Find the first JPEG SOF segment, skipping markers without a length."""
    sof_markers = {*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)}
    pos = 2
    while pos < len(data):
        if data[pos] != 0xFF:
            return (None, None)
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            return (None, None)
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            return (None, None)
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > len(data):
            return (None, None)
        if marker in sof_markers:
            if length < 8:
                return (None, None)
            height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
            return (width, height)
        pos += length
    return (None, None)


def _webp_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Read VP8, VP8L, or VP8X canvas dimensions from a WebP RIFF header."""
    pos = 12
    while pos + 8 <= len(data):
        chunk, length = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        payload = pos + 8
        end = payload + length
        if end > len(data):
            return (None, None)
        if chunk == b"VP8 " and length >= 10 and data[payload + 3:payload + 6] == b"\x9d\x01\x2a":
            width = struct.unpack("<H", data[payload + 6:payload + 8])[0] & 0x3FFF
            height = struct.unpack("<H", data[payload + 8:payload + 10])[0] & 0x3FFF
            return (width, height)
        if chunk == b"VP8L" and length >= 5 and data[payload] == 0x2F:
            bits = int.from_bytes(data[payload + 1:payload + 5], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        if chunk == b"VP8X" and length >= 10:
            width = int.from_bytes(data[payload + 4:payload + 7], "little") + 1
            height = int.from_bytes(data[payload + 7:payload + 10], "little") + 1
            return (width, height)
        pos = end + (length & 1)
    return (None, None)


def save_upload_bytes(data: bytes, name: str, mime: str, prefix: str = "att") -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large")
    safe = clean_filename(name)
    ext = ext_for(safe, mime)
    stored = f"{prefix}-{secrets.token_urlsafe(10)}{ext}"
    path = UPLOAD_DIR / stored
    path.write_bytes(data)
    kind = "image" if (mime or "").startswith("image/") else ("audio" if (mime or "").startswith("audio/") else "file")
    upload = {
        "id": Path(stored).stem,
        "url": f"{PUBLIC_PREFIX}/uploads/{stored}" if PUBLIC_PREFIX else f"/uploads/{stored}",
        "name": safe,
        "size": len(data),
        "mime": mime or "application/octet-stream",
        "kind": kind,
    }
    if kind == "image":
        width, height = _image_dimensions(path)
        if width and height:
            upload["width"], upload["height"] = width, height
    if kind == "file":
        upload.update(_preprocess_attachment(path, safe, mime))
    return upload


def _file_delivery_error(raw: str) -> str | None:
    """Validate one model-authored file marker and return a user-safe reason on failure."""
    if not raw or not raw.startswith("/"):
        return "路径必须是绝对路径"
    try:
        source = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        return "文件不存在或无法读取"
    if not source.is_file():
        return "路径不是普通文件"
    if not any(source == root or root in source.parents for root in FILE_DELIVERY_ROOTS):
        return "该路径不在可发送目录内"
    name = source.name.lower()
    if (name.endswith((".env", ".pem", ".key")) or name.startswith("id_rsa")
            or source == Path("./data/valelin/relay.db")):
        return "该文件类型不允许发送"
    try:
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            return f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB 上限"
    except OSError:
        return "无法读取文件大小"
    return None


def _deliver_model_file(block: str) -> tuple[dict | None, str | None]:
    """Copy a valid [file:] source into UPLOAD_DIR and return an ordinary attachment payload."""
    match = FILE_MARK_RE.match(block or "")
    if not match:
        return None, "文件标记格式无效"
    raw_path, sep, override = match.group(1).strip().partition("|")
    raw_path = raw_path.strip()
    error = _file_delivery_error(raw_path)
    if error:
        return None, error
    try:
        source = Path(raw_path).resolve(strict=True)
        display_name = override.strip() if sep and override.strip() else source.name
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        # save_upload_bytes creates a new att-* file, so the delivered copy survives source edits/deletion.
        attachment = save_upload_bytes(source.read_bytes(), display_name, mime, "att")
        return attachment, None
    except OSError:
        return None, "文件复制失败"


def _file_error_segment(error: str) -> dict:
    return {"kind": "file_error", "text": f"文件发送失败：{error}"}


def transcribe_with_command(audio_path: Path, mime: str) -> str:
    """Optional local ASR hook. The command receives <audio_path> <mime> and prints a transcript."""
    if not VOICE_TRANSCRIBE_CMD:
        return ""
    try:
        proc = subprocess.run(
            [VOICE_TRANSCRIBE_CMD, str(audio_path), mime or "application/octet-stream"],
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def resolve_stt_mode() -> str:
    """卡上选的 provider 优先（空 = 跟随 env）；env 显式配置优先，auto 按序探测：
    api（配了 URL+key）→ elevenlabs（配了 key）→ cmd（配了命令）→ whisper（装了包）→ off。"""
    card = (voice_stt_cfg.get("stt_provider") or "").strip().lower()
    if card in ("api", "cmd", "whisper", "elevenlabs", "off"):
        return card
    if STT_MODE in ("api", "cmd", "whisper", "elevenlabs", "off"):
        return STT_MODE
    if STT_API_URL and STT_API_KEY:
        return "api"
    if STT_EL_KEY:
        return "elevenlabs"
    if VOICE_TRANSCRIBE_CMD:
        return "cmd"
    try:
        import faster_whisper  # noqa: F401
        return "whisper"
    except Exception:
        return "off"


def stt_effective_model(mode: str) -> str:
    """各路模型：卡上填了就用卡上的，空回退各路 env 默认。"""
    m = (voice_stt_cfg.get("stt_model") or "").strip()
    if m:
        return m
    if mode == "api":
        return STT_API_MODEL
    if mode == "elevenlabs":
        return STT_EL_MODEL
    if mode == "whisper":
        return STT_WHISPER_MODEL
    return ""


def stt_effective_lang() -> str:
    """语言：卡上填了就用卡上的，空回退 env；两边都空 = 引擎自动检测。"""
    return (voice_stt_cfg.get("stt_lang") or "").strip() or STT_LANG


def stt_keyterms_list() -> list[str]:
    """热词表：换行/逗号（中西）分隔 → 去空去重。scribe 上限 1000 条、每条 50 字符，超限截断。"""
    raw = voice_stt_cfg.get("stt_keyterms") or ""
    terms: list[str] = []
    for chunk in re.split(r"[\n,，、;；]+", raw):
        t = chunk.strip()
        if t and t not in terms:
            terms.append(t[:50])
    return terms[:1000]


# ── 实质人声判定（复盘坑 01/02）─────────────────────────────────────────────
# 转写引擎对纯背景音不会返回空文本，会「幻听」出事件描述（实测 "[silence]"、
# words=[{type:'audio_event'}]），而上游只看 text 非空，方括号内容穿透。
# 解法：剥掉所有事件描述（方/圆/中文括号）再剥标点空白，看还剩几个字。
# 红线（坑 02）：只做零值判断。绝不加「单字/短句视为幻听」的过滤——
# 宁可放过幻听，不可吞掉真话；「哦」「嗯」必须原样进来。
_AUDIO_EVENT_RE = re.compile(r"\[[^\[\]]*\]|\([^()]*\)|（[^（）]*）|【[^【】]*】")
_PUNCT_NOSPEECH_RE = re.compile(r"[\W_]+", re.UNICODE)


def substantive_text(text: str) -> str:
    """剥事件描述 + 标点空白后的净文本；空 = 纯事件/无实质内容。"""
    t = _AUDIO_EVENT_RE.sub(" ", text or "")
    return _PUNCT_NOSPEECH_RE.sub("", t).strip()


def transcribe_with_api(audio_path: Path, mime: str) -> str:
    """OpenAI 兼容 /audio/transcriptions（Groq 等），multipart 上传，返回转写文本（失败返回空，日志进 journal）。
    热词走 OpenAI 兼容的 prompt 参数（Groq whisper 系支持；对专名的对齐弱于 scribe 的 keyterms）。"""
    if not (STT_API_URL and STT_API_KEY):
        return ""
    try:
        with open(audio_path, "rb") as f:
            data = {"model": stt_effective_model("api")}
            lang = stt_effective_lang()
            if lang:
                data["language"] = lang
            terms = stt_keyterms_list()
            if terms:
                data["prompt"] = ", ".join(terms)
            files = {"file": (audio_path.name, f, mime or "application/octet-stream")}
            r = httpx.post(STT_API_URL, headers={"Authorization": f"Bearer {STT_API_KEY}"},
                           data=data, files=files, timeout=STT_TIMEOUT)
    except Exception as exc:
        print(f"[stt] api failed: {exc}", flush=True)
        return ""
    if r.status_code != 200:
        print(f"[stt] api {r.status_code}: {r.text[:200]}", flush=True)
        return ""
    try:
        return (r.json().get("text") or "").strip()[:STT_MAX_CHARS]
    except Exception:
        return ""


def transcribe_with_eleven(audio_path: Path, mime: str) -> str:
    """ElevenLabs Scribe /v1/speech-to-text（复盘同款）。keyterms 热词原生支持（scribe_v2 才认）。
    multipart：model_id / language_code（ISO-639-1，空省略=自动）/ keyterms 重复字段。
    返回 text；words 数组（逐词 start/end/type）此路暂不落库，留给以后的字幕/对齐。"""
    if not STT_EL_KEY:
        print("[stt] elevenlabs 未配 key（RELAY_STT_EL_KEY），丢弃这轮转写", flush=True)
        return ""
    terms = stt_keyterms_list()
    data = {"model_id": stt_effective_model("elevenlabs")}
    lang = stt_effective_lang()
    if lang:
        data["language_code"] = lang
    if terms:
        data["keyterms"] = terms
    # 验收要证据：keyterms 实际随请求送出才打这行（keyterms 是配置不是密钥，明文打前几条可对证）
    print(f"[stt] scribe 请求: model={data.get('model_id')} lang={lang or '(自动)'} "
          f"keyterms={len(terms)}条送出 [{'; '.join(terms[:5])}{'…' if len(terms) > 5 else ''}]", flush=True)
    try:
        with open(audio_path, "rb") as f:
            files = {"file": (audio_path.name, f, mime or "application/octet-stream")}
            r = httpx.post(STT_EL_URL, headers={"xi-api-key": STT_EL_KEY},
                           data=data, files=files, timeout=STT_TIMEOUT)
    except Exception as exc:
        print(f"[stt] elevenlabs failed: {exc}", flush=True)
        return ""
    if r.status_code != 200:
        print(f"[stt] elevenlabs {r.status_code}: {r.text[:200]}", flush=True)
        return ""
    try:
        return (r.json().get("text") or "").strip()[:STT_MAX_CHARS]
    except Exception:
        print("[stt] elevenlabs 响应解析失败（无 text 字段）", flush=True)
        return ""


def _get_whisper_model():
    """懒加载 faster-whisper 单例；失败置进程内标记不再重试（journal 有日志）。"""
    if STT_WHISPER["failed"]:
        return None
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        STT_WHISPER["failed"] = True
        print(f"[stt] faster-whisper 不可用（pip install faster-whisper）: {exc}", flush=True)
        return None
    with STT_WHISPER_LOCK:
        if STT_WHISPER["model"] is None:
            t0 = time.time()
            try:
                # 先只走本地缓存（模型预下在 STT_WHISPER_DIR）：不联网、不碰 ~/.cache
                # 的 hf token（systemd ProtectHome=true 下那个路径读不得）。
                STT_WHISPER["model"] = WhisperModel(
                    STT_WHISPER_MODEL, device="cpu", compute_type="int8",
                    download_root=str(STT_WHISPER_DIR), local_files_only=True)
            except Exception:
                try:
                    STT_WHISPER["model"] = WhisperModel(
                        STT_WHISPER_MODEL, device="cpu", compute_type="int8",
                        download_root=str(STT_WHISPER_DIR))
                except Exception as exc:
                    STT_WHISPER["failed"] = True
                    print(f"[stt] whisper 模型加载失败: {exc}", flush=True)
                    return None
            print(f"[stt] whisper 模型就绪 {STT_WHISPER_MODEL} ({time.time() - t0:.1f}s)", flush=True)
    return STT_WHISPER["model"]


def transcribe_with_whisper(audio_path: Path, mime: str) -> str:
    """本地 faster-whisper（CPU int8）。首次调用加载模型（已缓存则秒级），失败返回空。
    热词走 initial_prompt（whisper 系对专名的服从度弱于 scribe keyterms，但聊胜于无）。"""
    model = _get_whisper_model()
    if model is None:
        return ""
    try:
        kw = {}
        terms = stt_keyterms_list()
        if terms:
            kw["initial_prompt"] = ", ".join(terms)
        segments, _info = model.transcribe(
            str(audio_path), language=(stt_effective_lang() or None), beam_size=1,
            vad_filter=True, **kw)
        text = "".join(s.text for s in segments).strip()
        return text[:STT_MAX_CHARS]
    except Exception as exc:
        print(f"[stt] whisper 转写失败: {exc}", flush=True)
        return ""


async def transcribe_audio(audio_path: Path, mime: str) -> str:
    """STT 统一入口：按 resolve_stt_mode() 走一条路，不静默换路（配错配漏 journal 里看得见）。"""
    mode = resolve_stt_mode()
    if mode != _stt_mode_logged["mode"]:
        print(f"[stt] 转写路 = {mode}（card={voice_stt_cfg.get('stt_provider') or '(空)'} "
              f"env={STT_MODE}）", flush=True)
        _stt_mode_logged["mode"] = mode
    if mode == "off":
        return ""
    if mode == "api":
        return await asyncio.to_thread(transcribe_with_api, audio_path, mime)
    if mode == "elevenlabs":
        return await asyncio.to_thread(transcribe_with_eleven, audio_path, mime)
    if mode == "cmd":
        return await asyncio.to_thread(transcribe_with_command, audio_path, mime)
    return await asyncio.to_thread(transcribe_with_whisper, audio_path, mime)


def stt_warmup_once() -> None:
    """启动预载 whisper 模型（RELAY_STT_WARM=1 且解析出 whisper 模式时），daemon 线程不阻塞 boot。"""
    if not STT_WARMUP or resolve_stt_mode() != "whisper":
        return

    def _load():
        _get_whisper_model()

    threading.Thread(target=_load, daemon=True, name="stt-warmup").start()


def _upstream_http_detail(exc: Exception) -> str:
    """urllib HTTPError 的 str 不含响应体，而 key/额度类错误码都在 body 里
    （MOSS 4010 invalid credentials、MiniMax base_resp 等）——不透出就只能上机探测。"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode(errors="replace")[:200]
        except Exception:
            body = ""
        return f"HTTP {exc.code} {body}".strip()
    return f"{type(exc).__name__}: {exc}"


def minimax_tts_mp3(text: str, api_key: str = "", group_id: str = "",
                    voice_id: str = "", model: str = "") -> bytes:
    """MiniMax TTS 文字进 mp3 出。api_key/group_id/voice_id/model 有值用值，空则回退 env。"""
    api_key = api_key or MINIMAX_API_KEY
    voice_id = voice_id or MINIMAX_VOICE_ZH
    if not api_key or not voice_id:
        raise HTTPException(status_code=503, detail="minimax tts not configured")
    group_id = group_id or MINIMAX_GROUP_ID
    model = model or MINIMAX_MODEL
    clean = (text or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="empty text")
    clean = clean[:900]
    url = f"{MINIMAX_API_BASE.rstrip('/')}/v1/t2a_v2"
    if group_id:
        url += f"?GroupId={group_id}"
    payload = {
        "model": model,
        "text": clean,
        "stream": False,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=MINIMAX_TTS_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"minimax tts failed: {_upstream_http_detail(exc)}")
    audio_hex = (data.get("data") or {}).get("audio")
    if not audio_hex:
        # MiniMax 业务错误走 HTTP 200 + base_resp{status_code,status_msg}（如 1004 key 无效 / 2038 余额不足），
        # 不透出的话故障只显示「no audio」，key/额度问题查无可查。base_resp 无敏感字段。
        base = data.get("base_resp") or {}
        raise HTTPException(status_code=502, detail=(
            f"minimax tts returned no audio"
            + (f" (base_resp={json.dumps(base, ensure_ascii=False)[:200]})" if base else "")
        ))
    try:
        return bytes.fromhex(audio_hex)
    except ValueError:
        raise HTTPException(status_code=502, detail="bad minimax audio payload")


def moss_tts_mp3(text: str, api_key: str = "", voice_id: str = "",
                 model: str = "", version: str = "",
                 instruction: str = "", sound_event: str = "") -> bytes:
    """MOSS/MOSI TTS 文字进 mp3 出。

    请求构造按 Vale op 端 voice_bar synthesizeWithMoss 实测可通的版本（2026-08-30 任务书，
    旧版抄 OpenClaw skill 的 studio.mosi.cn/v1/audio/tts 是错的，对一切请求 401）：
    POST {base}/v1/audio/speech，Bearer 认证，body 严格 {"model","input","voice_id",
    "response_format","delivery_method"}——文本字段名是 input，不带 version/stream/多余字段。
    有效 model 按 model 形参优先、version 形参其次、最后回落到 "moss-tts"；两个
    形参都映射到上游的 model 字段，不发送独立的 version 字段。
    instruction/sound_event（TASK-moss-sound-instruction，2026-09-02 接口实测 200 且音频
    有客观差异）：非空才加进 body，裸五字段请求形状不变；上限是防模型跑飞的长值，正常取值远够。
    成功响应 JSON 的音频是 URL（j.url → j.data.url → j.output.url → j.result.url 兜底），
    下载后返回 mp3 字节。合成请求保留 connect 10s / read 90s；音频 CDN 下载
    则在 15 秒总预算内最多重试 3 次（0.5s / 1s / 2s），避免单个坏 URL
    把整轮回复长时间卡住。
    """
    api_key = api_key or MOSS_API_KEY
    voice_id = voice_id or MOSS_VOICE_ID
    if not api_key or not voice_id:
        raise HTTPException(status_code=503, detail="moss tts not configured")
    clean = (text or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="empty text")
    clean = clean[:900]
    chosen_model = (model or version or "moss-tts").strip() or "moss-tts"
    payload = {
        "model": chosen_model,
        "input": clean,
        "voice_id": voice_id,
        "response_format": "mp3",
        "delivery_method": "url",
    }
    if (instruction or "").strip():
        payload["instruction"] = instruction.strip()[:500]
    if (sound_event or "").strip():
        payload["sound_event"] = sound_event.strip()[:200]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(90.0, connect=10.0)
    base = MOSS_API_BASE.rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{base}/v1/audio/speech", json=payload, headers=headers)
            if r.status_code >= 400:
                try:
                    err = r.json()
                except ValueError:
                    err = {"raw": r.text[:200]}
                detail = {k: err[k] for k in ("message", "type", "code", "param") if err.get(k) is not None}
                raise HTTPException(status_code=502, detail=(
                    f"moss tts HTTP {r.status_code} model={chosen_model}: "
                    f"{json.dumps(detail or err, ensure_ascii=False)[:200]}"))
            j = r.json()
            url = j.get("url")
            if not url:
                url = (j.get("data") or {}).get("url")
            if not url:
                url = (j.get("output") or {}).get("url")
            if not url:
                url = (j.get("result") or {}).get("url")
            if not url:
                raise HTTPException(status_code=502, detail=(
                    f"moss tts: no audio url in response keys={sorted(j.keys())[:8]}"))
            # MOSS 成功响应给的是短期音频 URL。该 URL 的 CDN 偶发 5xx；只重试
            # 可恢复的 5xx/超时，且把下载（含退避）限制在一小段总预算内，不能阻塞整轮。
            download_deadline = time.monotonic() + 15.0
            retry_delays = (0.5, 1.0, 2.0)  # 初始请求后的三次重试
            attempts = 0
            last_error = ""
            audio = b""
            for retry_index in range(len(retry_delays) + 1):
                remaining = download_deadline - time.monotonic()
                if remaining <= 0:
                    last_error = "download total timeout exceeded"
                    break
                attempts += 1
                try:
                    # Per-attempt cap leaves room for later retries while the deadline is authoritative.
                    attempt_timeout = min(6.0, remaining)
                    ar = client.get(url, timeout=httpx.Timeout(
                        attempt_timeout, connect=min(3.0, attempt_timeout)))
                except httpx.TimeoutException as exc:
                    last_error = f"timeout: {_upstream_http_detail(exc)}"
                except httpx.RequestError as exc:
                    # Transport errors other than a timeout are not requested retry cases.
                    raise HTTPException(status_code=502, detail=(
                        f"moss audio download request failed after {attempts} attempt(s): "
                        f"{_upstream_http_detail(exc)}"))
                else:
                    if ar.status_code < 400:
                        audio = ar.content
                        break
                    last_error = f"HTTP {ar.status_code}"
                    if ar.status_code < 500:
                        raise HTTPException(status_code=502, detail=f"moss audio download {last_error}")

                if retry_index == len(retry_delays):
                    break
                delay = retry_delays[retry_index]
                remaining = download_deadline - time.monotonic()
                if remaining <= delay:
                    last_error += "; total timeout would be exceeded before next retry"
                    break
                print(f"[moss-tts] audio download retry={retry_index + 1}/3 "
                      f"attempt={attempts} delay={delay:.1f}s reason={last_error}", flush=True)
                time.sleep(delay)
            if not audio:
                raise HTTPException(status_code=502, detail=(
                    f"moss audio download failed after {attempts} attempt(s): {last_error}"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"moss tts failed: {_upstream_http_detail(exc)}")
    if not audio:
        raise HTTPException(status_code=502, detail="moss tts: empty audio download")
    return audio


def mimo_tts(text: str, api_key: str = "", voice: str = "") -> bytes:
    """MiMo v2.5 音色复刻 TTS（OpenAI 兼容 /v1/chat/completions）。voice=样本 data URL。返回 mp3 字节。"""
    api_key = api_key or MIMO_API_KEY
    if not api_key:
        raise HTTPException(status_code=503, detail="mimo tts not configured")
    if not voice:
        raise HTTPException(status_code=400, detail="mimo voice sample required")
    clean = (text or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="empty text")
    clean = clean[:5000]
    payload = {
        "model": MIMO_MODEL,
        "messages": [{"role": "assistant", "content": clean}],
        "audio": {"voice": voice, "format": "mp3"},
    }
    req = urllib.request.Request(
        f"{MIMO_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"api-key": api_key, "Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TTS_UPSTREAM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mimo tts failed: {exc}")
    try:
        b64 = (data.get("choices") or [{}])[0]["message"]["audio"]["data"]
        return base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=502, detail="mimo tts bad payload")


def sse_data(payload: dict) -> str:
    lines: list[str] = []
    event_id = payload.get("id")
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def sse_ping() -> str:
    payload = {"type": "ping", "ts": datetime.now(timezone.utc).isoformat()}
    return "event: ping\n" + sse_data(payload)


async def sse_stream(
    subs: set,
    request: Request,
    initial: list[dict] | None = None,
    shutdown_event: asyncio.Event | None = None,
    extra_subs: set | None = None,
    visibility: dict | None = None,
):
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    subs.add(q)
    from backend.demo_visits import visitor,queue_visitors
    queue_visitors[q]=visitor.get()
    if subs is app_subs and visibility is not None:
        app_sub_visibility[q] = dict(visibility)
    if extra_subs is not None:
        extra_subs.add(q)
    try:
        yield "retry: 3000\n: connected\n\n"
        for payload in initial or []:
            yield sse_data(payload)
        while True:
            if await request.is_disconnected():
                break
            queue_task = asyncio.create_task(q.get())
            shutdown_task = (
                asyncio.create_task(shutdown_event.wait())
                if shutdown_event is not None
                else None
            )
            waiters = {queue_task}
            if shutdown_task is not None:
                waiters.add(shutdown_task)
            try:
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waiters:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

            if shutdown_task is not None and shutdown_task in done:
                yield 'event: shutdown\ndata: {"type":"server_shutdown"}\n\n'
                break
            if queue_task in done:
                yield sse_data(queue_task.result())
            else:
                yield sse_ping()  # keep the connection alive and let clients watchdog it
    finally:
        queue_visitors.pop(q,None)
        subs.discard(q)
        if subs is app_subs:
            app_sub_visibility.pop(q, None)
        if extra_subs is not None:
            extra_subs.discard(q)


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # tell nginx not to buffer the stream
    "Connection": "keep-alive",
}


# ---------------------------------------------------------------------------
# auth — one shared Bearer secret on every endpoint (single user)
# ---------------------------------------------------------------------------

def check_auth(request: Request) -> None:
    return  # loopback-only local demo


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_event_loop
    _keep_event_loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    app.state.shutdown_requested = shutdown_event
    _check_db_persistence()
    init_db()
    with db() as conn:
        archive_floor = int(conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
    from backend.demo_local import seed
    seed()

    command_expiry_task = asyncio.create_task(
        _command_expiry_worker(), name="command-card-expiry"
    )
    try:
        yield
    finally:
        _keep_event_loop = None
        shutdown_started = time.monotonic()
        shutdown_event.set()
        log.info("relay shutdown: stopping background tasks and SDK sessions")
        command_expiry_task.cancel()
        await asyncio.gather(command_expiry_task, return_exceptions=True)
        await _local_outbox.close()
        await _sdk_provider.close()
        log.info(
            "relay shutdown complete elapsed_ms=%d",
            int((time.monotonic() - shutdown_started) * 1000),
        )


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def keeps_mcp_internal_auth(request: Request, call_next):
    if request.url.path.startswith("/internal/keeps/"):
        host = request.client.host if request.client else ""
        auth = request.headers.get("authorization", "")
        supplied = auth[7:] if auth.startswith("Bearer ") else ""
        expected = _keeps_mcp_token()
        if host not in {"127.0.0.1", "::1"} or not expected or not supplied or not hmac.compare_digest(supplied, expected):
            return JSONResponse({"detail": "not found"}, status_code=404)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def cache_control(request: Request, call_next):
    """Keep every HTML page fresh, including preview and study pages.

    Cache the few near-immutable static assets separately.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/disabled-preview/"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    elif path == "/app/notes" or path.startswith("/app/notes/"):
        response.headers["Cache-Control"] = "no-store"
    elif path == "/" or path.endswith(".html"):
        # no-cache: revalidate on every visit · no-store: don't persist at all
        # (some old browsers only honour one of the two)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif path in ("/manifest.json", "/sw.js") or path.startswith("/icon-"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


async def _command_expiry_worker() -> None:
    from backend.demo_visits import visitor,rooms,housekeeping,reap_rooms
    while True:
        await asyncio.sleep(5)
        reap_rooms()
        for key in ['', *list(rooms)]:
            token=visitor.set(key);maintenance=housekeeping.set(True)
            try:
                with db() as conn:
                    expire_overdue_commands(conn)
            except asyncio.CancelledError:raise
            except Exception as exc:log.warning("[command-card] expiry check failed type=%s",type(exc).__name__)
            finally:housekeeping.reset(maintenance);visitor.reset(token)



@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "plugin_subs": len(plugin_subs),
        "app_subs": len(app_subs),
        "sdk": _sdk_provider.status(),
        "sdk_rate_limit": _load_sdk_rate_limit(),
    }


# ---- AI side ---------------------------------------------------------------

@app.get("/channel/in")
async def channel_in(request: Request, since: int = 0, limit: int = 100):
    """SSE stream the plugin holds open. The human's messages get pushed down here."""
    check_auth(request)
    backlog = [plugin_payload(m) for m in inbound_history(since, min(limit, 500))]
    return StreamingResponse(
        sse_stream(
            plugin_subs,
            request,
            backlog,
            shutdown_event=getattr(app.state, "shutdown_requested", None),
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.post("/channel/out")
async def channel_out(request: Request):
    """The AI's reply/react. Persist + fan out to the PWA."""
    check_auth(request)
    body = await request.json()
    kind = body.get("type", "reply")
    # temp: log loop payload to diagnose empty replies
    _log_body = {k: v for k, v in body.items() if k not in ("api",)}
    print(f"[relay:channel_out] kind={kind} body={json.dumps(_log_body, ensure_ascii=False)[:500]}")
    if kind in ("thinking_delta", "reply_delta"):
        return await handle_stream_delta(kind, body)
    if kind == "react":
        # An emoji reaction attached to an existing message's meta.reactions; no new
        # message is created. An empty emoji clears that reaction.
        try:
            target_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="react: numeric id required")
        emoji = (body.get("emoji") or "").strip()
        reactions = set_reaction(target_id, "ai", emoji)
        if reactions is None:
            raise HTTPException(status_code=404, detail="react: message not found")
        await broadcast(app_subs, {"type": "reaction", "id": target_id, "reactions": reactions, "by": "ai"})
        # A react is also the AI "acting" once — clear the typing indicator so the
        # header doesn't stay stuck typing when no reply follows.
        await broadcast(app_subs, {"type": "typing", "active": False})
        return {"id": target_id, "reactions": reactions}
    if kind in ("tool_start", "tool_done"):
        # Only segmented replies treat tool_start as a paragraph boundary.
        # Share the delta/TTS lock so a tool never overtakes its preceding block.
        stream_id = str(body.get("stream_id") or "").strip()
        if kind == "tool_start" and segment_mode_on and stream_id:
            async with _stream_lock(stream_id):
                draft = stream_drafts.get((stream_id, "reply"))
                buf = (draft or {}).get("seg_buf") or ""
                if buf.strip() and not _block_struct_open(buf):
                    draft.setdefault("tool_boundary_offsets", []).append(len(draft["text"]))
                    draft["text"] += "\n\n"
                    draft["seg_buf"] = buf + "\n\n"
                    await _process_buffered_blocks(stream_id, draft)
            if (stream_id, "reply") not in stream_drafts:
                voice_stream_locks.pop(stream_id, None)
        # Tool progress itself is still broadcast only; no tool message is stored.
        await broadcast(app_subs, body)
        return {"ok": True, "stream_id": body.get("stream_id")}
    text = body.get("text", "")
    meta = {k: v for k, v in body.items() if k not in ("type", "text")}

    # Inline <think> blocks arrive glued to the reply. Persist them as separate
    # 'thinking' messages so the PWA can render them collapsed; only the reply
    # segments stay in a bubble.
    segments = split_think_blocks(text) if kind == "reply" else []
    if not segments:
        segments = [(kind, text)]

    # the AI replied — clear the typing state
    await broadcast(app_subs, {"type": "typing", "active": False})

    # 多版本标记（非流式路径）：同一次回复的片段共享 reroll_group = 触发它的用户消息 id。
    _group = str(meta.get("source_id") or "").strip() or None
    # 语音（非流式路径）：整段回复一次到 → 按 reply 段串行合成 <voice> 块，挂到对应段
    saved = []
    for seg_kind, seg_text in segments:
        seg_meta = dict(meta)
        if _group:
            seg_meta["reroll_group"] = _group
            seg_meta["variant_index"] = 1
            seg_meta["variant_active"] = 1
        msg = save_message("out", seg_kind, seg_text, seg_meta)
        saved.append(msg)
        if seg_kind == "reply" and kind == "reply" and segment_mode_on:
            await _attach_reply_segments(msg, seg_text)
        await _queue_local_message(msg)
        await broadcast(app_subs, app_payload(msg))

    # Unread push: only when no connected PWA page reports itself visible;
    # only push real replies, not 'thinking' chatter.
    if not has_visible_app_subscribers():
        for msg in saved:
            if msg["kind"] != "reply":
                continue
            try:
                await push_to_all(notification_from_message(msg))
            except Exception:
                pass  # a push failure must never affect persistence/fan-out
    return {"id": saved[-1]["id"], "ids": [m["id"] for m in saved]}


# ---- human side ------------------------------------------------------------

# ── 引用带原文 ──────────────────────────────────────────────────────────────
# 前端长按气泡只发 {msg_id, seq?}，relay 按锚点从自己库解析原文快照进 meta.quotes，
# loop 把快照内联进当轮 user 块。存快照不存指针：原消息之后被删/reroll 置 0 都不丢。
# 锚点体系（防混用）：引用 = relay messages.id（msg_id 系，气泡级）；
# 收藏 = OB conversation_turns.id（turn_id 系，轮级）。两套并存，职责不同。
QUOTE_MAX_CHARS = 2000                      # 引用给模型的字数上限，超了截头留尾加省略号（Lin 定死）
QUOTE_KINDS = ("reply", "voice", "user")    # thinking/call 等内部行不可引


def _clip_quote_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= QUOTE_MAX_CHARS:
        return text
    return "……" + text[-QUOTE_MAX_CHARS:]    # 截头留尾：尾部更贴近当下语境


def _quote_plain_text(text: str) -> str:
    """整条回退（锚点无 seq）：剥 [voice]/[zh]/<voice> 标记与 [audio] 声学行，显示层口径对齐前端 stripVoiceTags。
    声学行是给模型看的附注，引用快照是她的话——整行跳过，不进引用。"""
    lines: list[str] = []
    for ln in (text or "").split("\n"):
        t = re.sub(r"^\[[a-z]{2}\]\s*", "", ln.strip())   # 语言标签行剥标签留正文
        if not t or t.startswith("[audio]"):
            continue                                       # 声学行整行跳过
        lines.append(t)
    s = VOICE_MARK_RE.sub("", "\n".join(lines))           # 整条级首行 [voice · 0:12 · mood]
    s = BR_MARK_RE.sub("\n", s)                           # [br] 分隔标记剥掉留换行（快照不漏标记）
    return re.sub(r"</?voice>", "", s).strip()            # 旧句子级标签兜底


def _quote_items_from_meta(meta) -> list[dict]:
    """统一读取引用：新数据读 meta.quotes，旧数据把 meta.quote 视作单元素列表。"""
    if not isinstance(meta, dict):
        return []
    quotes = meta.get("quotes")
    if isinstance(quotes, list):
        return [q for q in quotes if isinstance(q, dict)]
    quote = meta.get("quote")
    return [quote] if isinstance(quote, dict) else []


def _quote_items_from_payload(body) -> list:
    """统一读取入参：quotes 优先；旧客户端的 quote 兼容成单元素列表。"""
    if not isinstance(body, dict):
        return []
    if "quotes" in body:
        return body.get("quotes") if isinstance(body.get("quotes"), list) else []
    quote = body.get("quote")
    return [quote] if quote is not None else []


def _resolve_quote_snapshot(quote) -> dict | None:
    """quote 锚点 {msg_id, seq?, at_seg?} → 原文快照 {msg_id, seq?, at_seg?, text}。解析失败返回 None
    （引用静默丢弃，绝不挡发送）。指名 seq 时段必须命中，宁可丢弃也不降级引整条——防引错归属。"""
    if not isinstance(quote, dict):
        log.warning("[quote] dropped msg_id=%r seq=%r reason=invalid_quote", None, None)
        return None
    try:
        msg_id = int(quote.get("msg_id"))
    except (TypeError, ValueError):
        log.warning("[quote] dropped msg_id=%r seq=%r reason=invalid_msg_id", quote.get("msg_id"), quote.get("seq"))
        return None
    seq: int | None = None
    if quote.get("seq") is not None:
        try:
            seq = int(quote.get("seq"))
        except (TypeError, ValueError):
            log.warning("[quote] dropped msg_id=%r seq=%r reason=invalid_seq", msg_id, quote.get("seq"))
            return None
    at_seg: int | None = None
    if "at_seg" in quote:
        raw_at_seg = quote.get("at_seg")
        if isinstance(raw_at_seg, bool) or not isinstance(raw_at_seg, int) or raw_at_seg < 0:
            log.warning("[quote] at_seg dropped msg_id=%r at_seg=%r reason=invalid_at_seg", msg_id, raw_at_seg)
        else:
            at_seg = raw_at_seg
    with db() as conn:
        row = conn.execute(
            "SELECT id, ts, direction, kind, text, meta FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
    if not row or row["kind"] not in QUOTE_KINDS:
        log.warning("[quote] dropped msg_id=%r seq=%r reason=message_missing_or_not_quotable", msg_id, seq)
        return None
    if seq is not None:
        try:
            meta = json.loads(row["meta"] or "{}")
        except Exception:
            meta = {}
        segs = keeps_store.message_segments(row, meta) if row["direction"] == "in" else (
            meta.get("segments") if isinstance(meta.get("segments"), list) else []
        )
        text = ""
        segment_kind = ""
        for sd in segs:
            if not isinstance(sd, dict) or sd.get("kind") == "sticker":
                continue
            try:
                if int(sd.get("seq")) != seq:
                    continue
            except (TypeError, ValueError):
                continue
            text = str(sd.get("text") or "").strip()      # 原文快照保留标签；展示端按 kind 清理
            segment_kind = str(sd.get("kind") or "")
            break
        if not text:
            log.warning("[quote] dropped msg_id=%r seq=%r reason=segment_missing_or_not_quotable", msg_id, seq)
            return None
    else:
        text = _quote_plain_text(row["text"])
    text = _clip_quote_text(text)
    if not text:
        log.warning("[quote] dropped msg_id=%r seq=%r reason=empty_quote_text", msg_id, seq)
        return None
    snap: dict = {"msg_id": msg_id, "text": text}
    if seq is not None:
        snap["seq"] = seq
    if seq is not None and segment_kind == "voice":
        snap["kind"] = "voice"    # 展示端要区别 voice 原文与普通文本中的方括号
    if at_seg is not None:
        snap["at_seg"] = at_seg
    return snap


@app.post("/app/send")
async def app_send(request: Request):
    """Human types in the PWA. Persist, push to the AI (plugin), echo to other PWA tabs."""
    check_auth(request)
    body = await request.json()
    text = (body.get("text") or "").strip()
    attachments = body.get("attachments") if isinstance(body.get("attachments"), list) else []
    api_session = str(body.get("api_session") or body.get("session_id") or "").strip()
    client_token = str(body.get("client_token") or "").strip()[:120]
    if not text and not attachments:
        raise HTTPException(status_code=400, detail="empty text")
    meta = {"user": "human", "attachments": attachments}
    if client_token:
        meta["client_token"] = client_token
    # 合并队列顺序：前端 flush 时把「图在前/文在前」带过来，loop build_messages 按它排块
    images_first = body.get("images_first")
    if isinstance(images_first, bool):
        meta["images_first"] = images_first
    if api_session:
        meta["api_session"] = api_session
    resolved_quotes = []
    quote_items = _quote_items_from_payload(body)
    if "quotes" in body and not isinstance(body.get("quotes"), list):
        log.warning("[quote] dropped quotes reason=invalid_quotes_container")
    for index, item in enumerate(quote_items):
        resolved = _resolve_quote_snapshot(item)
        if resolved:
            resolved_quotes.append(resolved)
        else:
            log.warning("[quote] dropped item index=%d reason=unresolved", index)
    if resolved_quotes:
        meta["quotes"] = resolved_quotes
    msg = save_message("in", "user", text, meta)
    await _queue_local_message(msg)
    # The router snapshots one provider for this turn. A concurrent hot switch
    # affects the next turn only; it cannot move this one between runtimes.
    msg = await _dispatch_and_echo_incoming(msg)
    return {"id": msg["id"], "client_token": client_token}


def _loop_dry_reroll(text: str, msg_id: int | None, session_id: str, quotes: list[dict] | None = None) -> dict:
    """Ask the loop to regenerate a reply for the given user message WITHOUT saving/streaming (dry).

    重 roll 用 dry：先生成成功，再删旧回复、落库新回复 —— 满足「先请求成功再替换」。
    超时放宽到 300s（生成整段回复可能比 loop_json 的 35s 久）。
    quotes：被重 roll 的那条 user 消息若带引用快照，原样带回，保证新版本与原版本同一语境。
    """
    data = json.dumps({
        "id": msg_id,
        "text": text,
        "session_id": session_id or "",
        "dry": True,
        "quotes": quotes or [],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        loop_base_url() + "/loop/ingest",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise HTTPException(status_code=exc.code, detail=f"reroll loop error: {detail}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"reroll loop error: {exc}")


# ── 重 roll 多版本：helpers ────────────────────────────────────────────────
def _variant_rows(conn, group: str) -> list:
    """group 里带 variant_index 的所有消息（按 id 升序）。"""
    return conn.execute(
        "SELECT id, kind, meta FROM messages "
        "WHERE json_extract(meta, '$.reroll_group') = ? AND json_extract(meta, '$.variant_index') IS NOT NULL "
        "ORDER BY id ASC",
        (group,),
    ).fetchall()


def _variant_summary(conn, group: str) -> dict:
    """返回 group 的 {total, active_index, variants:[{index, msg_id, active}]}。"""
    by_idx: dict[int, dict] = {}
    active_index: int | None = None
    for r in _variant_rows(conn, group):
        meta = json.loads(r["meta"] or "{}")
        idx = int(meta.get("variant_index") or 0)
        if idx <= 0:
            continue
        entry = by_idx.setdefault(idx, {"msg_id": None})
        if entry["msg_id"] is None:
            entry["msg_id"] = r["id"]              # 先存任一片段的 id 兜底
        if r["kind"] != "thinking":
            entry["msg_id"] = r["id"]              # 有 reply 片段 → 用它的 id 当锚点
        if int(meta.get("variant_active") or 0) == 1:
            active_index = idx
    variants = [
        {"index": i, "msg_id": by_idx[i]["msg_id"], "active": (i == active_index)}
        for i in sorted(by_idx)
    ]
    return {"total": len(variants), "active_index": active_index, "variants": variants}


def _set_variant_active(conn, group: str, active_index: int | None = None) -> None:
    """把 group 所有版本的 variant_active 置为 (index == active_index)。active_index=None → 全置 0。"""
    for r in _variant_rows(conn, group):
        meta = json.loads(r["meta"] or "{}")
        idx = int(meta.get("variant_index") or 0)
        if idx <= 0:
            continue
        want = 1 if idx == active_index else 0
        if int(meta.get("variant_active") or 0) != want:
            meta["variant_active"] = want
            conn.execute("UPDATE messages SET meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), r["id"]))


def _backfill_variant(conn, msg_id: int, group: str, index: int, active: int) -> None:
    """给一条没有 group 的旧回复补上版本标记。历史消息不回填，只回填被 roll 的那条。"""
    row = conn.execute("SELECT meta FROM messages WHERE id = ?", (msg_id,)).fetchone()
    if not row:
        return
    meta = json.loads(row["meta"] or "{}")
    meta["reroll_group"] = group
    meta["variant_index"] = index
    meta["variant_active"] = active
    conn.execute("UPDATE messages SET meta = ? WHERE id = ?", (json.dumps(meta, ensure_ascii=False), msg_id))


def _variant_response(conn, group: str, active_index: int) -> dict:
    """返回 reroll / select 的响应：整组概览 + 当前版本的所有消息。"""
    summary = _variant_summary(conn, group)
    rows = conn.execute(
        "SELECT id, ts, direction, kind, text, meta FROM messages "
        "WHERE json_extract(meta, '$.reroll_group') = ? AND json_extract(meta, '$.variant_index') = ? "
        "ORDER BY id ASC",
        (group, active_index),
    ).fetchall()
    return {
        "group": group,
        "active_index": active_index,
        "total": summary["total"],
        "messages": [
            app_payload({
                "id": r["id"], "ts": r["ts"], "direction": r["direction"],
                "kind": r["kind"], "text": r["text"], "meta": json.loads(r["meta"] or "{}"),
            })
            for r in rows
        ],
        "variants": summary["variants"],
    }


async def _broadcast_group(group: str) -> None:
    """把 group 里所有消息的最新状态广播出去（让其他 tab 也同步 active 变更）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, ts, direction, kind, text, meta FROM messages "
            "WHERE json_extract(meta, '$.reroll_group') = ? ORDER BY id ASC",
            (group,),
        ).fetchall()
    for r in rows:
        await broadcast(app_subs, app_payload({
            "id": r["id"], "ts": r["ts"], "direction": r["direction"],
            "kind": r["kind"], "text": r["text"], "meta": json.loads(r["meta"] or "{}"),
        }))


@app.post("/app/reroll")
async def app_reroll(request: Request):
    """重 roll：不删旧回复，把旧版本标成 inactive，存成一个新版本（variant_index = max+1）。

    语义：找到这条回复之前的那条用户消息，用 loop 的 dry ingest 以同样的上下文重新生成。
    成功后才动库：旧版本一条都不删，只是 variant_active 置 0；失败则所有现有版本原样保留。
    只有 active 版本会进后续上下文（api_loop.relay_rows 过滤 variant_active=1），
    所以旧版本留着也不会让模型同时看到新旧两条回复。
    """
    check_auth(request)
    if brain_target() != "loop":
        raise HTTPException(status_code=400, detail="reroll only works in loop brain mode")
    body = await request.json()
    try:
        target_id = int(body.get("msg_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="msg_id required")

    with db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (target_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="message not found")
        if row["direction"] != "out" or row["kind"] not in ("reply", "thinking"):
            raise HTTPException(status_code=400, detail="not an assistant reply")
        target_meta = json.loads(row["meta"] or "{}")
        session_id = str(target_meta.get("api_session") or "").strip()
        if session_id:
            prev = conn.execute(
                "SELECT * FROM messages WHERE direction='in' AND id < ? "
                "AND kind IN ('user','voice','call') "
                "AND json_extract(meta, '$.api_session') = ? "
                "ORDER BY id DESC LIMIT 1",
                (target_id, session_id),
            ).fetchone()
        else:
            prev = conn.execute(
                "SELECT * FROM messages WHERE direction='in' AND id < ? "
                "AND kind IN ('user','voice','call') "
                "AND (json_extract(meta, '$.api_session') IS NULL OR json_extract(meta, '$.api_session') = '') "
                "ORDER BY id DESC LIMIT 1",
                (target_id,),
            ).fetchone()
        if not prev:
            raise HTTPException(status_code=400, detail="no preceding user message")
        prev_text = str(prev["text"] or "").strip()
        if not prev_text:
            raise HTTPException(status_code=400, detail="preceding message is empty")
        prev_id = int(prev["id"])
        try:
            prev_quotes = _quote_items_from_meta(json.loads(prev["meta"] or "{}"))
        except Exception:
            prev_quotes = []

        # group：已有 reroll_group 直接复用；否则以触发它的用户消息 id 起组，
        # 并把这条旧回复回填成 variant 1（回填后下面还会整体置 0，所以这里先标 1）。
        group = str(target_meta.get("reroll_group") or "").strip()
        legacy_backfill = not group
        if not group:
            group = str(prev_id)
        summary = _variant_summary(conn, group)
        total = summary["total"]
        if total >= MAX_REROLL_VARIANTS:
            raise HTTPException(status_code=400, detail=f"这条最多 {MAX_REROLL_VARIANTS} 个版本")
        if legacy_backfill:
            next_index = 2                       # 旧回复回填成 1，新的从 2 开始
        else:
            next_index = max([v["index"] for v in summary["variants"]] + [0]) + 1

    dry = _loop_dry_reroll(prev_text, prev_id, session_id, quotes=prev_quotes)
    reply = (dry.get("reply") or "").strip()
    if not reply or reply == "(The API loop did not produce a reply.)":
        raise HTTPException(status_code=502, detail="reroll: loop returned no reply")

    with db() as conn:
        if legacy_backfill:
            _backfill_variant(conn, target_id, group, 1, 1)
        _set_variant_active(conn, group, active_index=None)   # 现有版本全部置 0
        # with 块退出时统一 commit

    api_meta = dry.get("api") or {}
    segments = split_think_blocks(reply)
    if not segments:
        segments = [("reply", reply)]
    new_stream = "api-" + secrets.token_hex(8)
    for seg_kind, seg_text in segments:
        seg_meta = {
            "reroll_of": target_id,
            "stream_id": new_stream,
            "api": api_meta,
            "reroll_group": group,
            "variant_index": next_index,
            "variant_active": 1,
        }
        if session_id:
            seg_meta["api_session"] = session_id
        msg = save_message("out", seg_kind, seg_text, seg_meta)
        await broadcast(app_subs, app_payload(msg))
    # 广播整组最新状态（新版本 + 已置 0 的旧版本），其他 tab 才能同步
    await _broadcast_group(group)
    await broadcast(app_subs, {"type": "typing", "active": False})

    with db() as conn:
        return _variant_response(conn, group, next_index)


@app.post("/app/reroll/select")
async def app_reroll_select(request: Request):
    """切换 group 里选中哪个版本。一个事务里做完：先全组置 0，再把目标版本置 1。"""
    check_auth(request)
    body = await request.json()
    group = str(body.get("group") or "").strip()
    try:
        index = int(body.get("index"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="index required")
    if not group:
        raise HTTPException(status_code=400, detail="group required")

    with db() as conn:
        summary = _variant_summary(conn, group)
        if summary["total"] == 0:
            raise HTTPException(status_code=404, detail="group not found")
        if index < 1 or index > summary["total"]:
            raise HTTPException(status_code=400, detail=f"index out of range (1..{summary['total']})")
        _set_variant_active(conn, group, active_index=index)
        # with 块退出时统一 commit
    await _broadcast_group(group)
    with db() as conn:
        return _variant_response(conn, group, index)


@app.delete("/app/message/{msg_id}")
async def app_message_delete(request: Request, msg_id: int):
    """物理删除一条消息，relay.db 里真的消失（不是本地标记）。

    - 删一条 = 删整个 reroll_group（所有版本），与前端「删一条删整组」语义对齐
    - 删 user 消息（direction='in'）额外成对删：紧跟的 assistant 回复
      （direction='out' 的 thinking + reply 全段）。不能只按 id 相邻找下一条 out：
      thinking 独立成行，且 reroll 新版本插在表尾（id 比后续 user 消息还大）。
      所以分两路：① 本 user 消息与下一条 in 之间的所有 out 行（兜底非 loop 回复）；
      ② 所有 meta.reroll_group = str(user_id) 的行（loop 回复靠 source_id 起组）。
    - 返回实际删掉的 id 列表，前端据此移除 DOM；并广播给其他 tab 同步。
    """
    check_auth(request)
    with db() as conn:
        row = conn.execute(
            "SELECT id, direction, kind, meta FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="message not found")
        meta = json.loads(row["meta"] or "{}")
        ids: set[int] = {int(row["id"])}

        # 1. reroll_group 整组（被删这条的所有版本）
        group = str(meta.get("reroll_group") or "").strip()
        if group:
            for r in conn.execute(
                "SELECT id FROM messages WHERE json_extract(meta, '$.reroll_group') = ?", (group,)
            ).fetchall():
                ids.add(int(r["id"]))

        if row["direction"] == "in":
            # 2a. 成对：本 user 消息与「下一条 in 之间」的全部 out 行
            next_in = conn.execute(
                "SELECT MIN(id) AS mid FROM messages WHERE direction = 'in' AND id > ?",
                (row["id"],),
            ).fetchone()["mid"]
            if next_in is not None:
                out_rows = conn.execute(
                    "SELECT id FROM messages WHERE direction = 'out' AND id > ? AND id < ?",
                    (row["id"], next_in),
                ).fetchall()
            else:
                out_rows = conn.execute(
                    "SELECT id FROM messages WHERE direction = 'out' AND id > ?", (row["id"],)
                ).fetchall()
            for r in out_rows:
                ids.add(int(r["id"]))
            # 2b. reroll 后插到表尾的新版本：meta.reroll_group = str(user id)
            for r in conn.execute(
                "SELECT id FROM messages WHERE json_extract(meta, '$.reroll_group') = ?",
                (str(row["id"]),),
            ).fetchall():
                ids.add(int(r["id"]))

        deleted = sorted(ids)
        conn.executemany("DELETE FROM messages WHERE id = ?", [(i,) for i in deleted])
        conn.commit()

    await broadcast(app_subs, {"type": "delete", "ids": deleted})
    return {"deleted": deleted}


@app.post("/app/upload")
async def app_upload(request: Request, name: str = "file"):
    check_auth(request)
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    mime = request.headers.get("content-type", "application/octet-stream")
    return save_upload_bytes(data, name, mime, "att")


def attachment_original_name(stored_name: str) -> str:
    """Return the original attachment name recorded with this stored upload."""
    with db() as conn:
        rows = conn.execute("SELECT meta FROM messages ORDER BY id DESC").fetchall()
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attachments = (meta.get("attachments") or []) if isinstance(meta, dict) else []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            stored_url = str(attachment.get("url") or "")
            if Path(urllib.parse.urlparse(stored_url).path).name == stored_name:
                return clean_filename(str(attachment.get("name") or stored_name))
    return stored_name


@app.get("/uploads/{name}/download")
async def upload_download(request: Request, name: str):
    check_auth(request)
    safe = clean_filename(name)
    path = UPLOAD_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, filename=attachment_original_name(safe), content_disposition_type="attachment")


def _upload_preview_response(path: Path) -> FileResponse:
    if path.suffix.lower() == ".svg":
        # Keep SVG usable in <img>, while a direct same-origin navigation cannot run scripts.
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Content-Security-Policy": "sandbox"},
        )
    return FileResponse(path)


@app.get("/uploads/{name}")
async def uploads(request: Request, name: str):
    check_auth(request)
    safe = clean_filename(name)
    path = UPLOAD_DIR / safe
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return _upload_preview_response(path)


def _audio_note_line(qp) -> str:
    """前端声学采样统计 → [audio] 行（复盘第 04 章格式：vol=均值/峰值 pitch=中位Hz frames=帧数）。
    全部参数可选：缺帧/老客户端/桌面无麦 → 返回空串，消息不带这一行。只信数字，脏值按缺省处理。"""
    def _f(name):
        try:
            v = float(qp.get(name) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if v == v and v not in (float("inf"), float("-inf")) else 0.0   # NaN/Inf 防御
    frames = int(_f("a_frames"))
    if frames <= 0:
        return ""
    v_avg, v_peak = _f("a_vol"), _f("a_peak")
    pitch = int(_f("a_pitch"))
    parts = [f"vol={v_avg:.1f}/{v_peak:.1f}"]
    if pitch > 0:
        parts.append(f"pitch={pitch}Hz")
    parts.append(f"frames={frames}")
    return "[audio] " + " ".join(parts)


@app.post("/app/voice")
async def app_voice(request: Request):
    """Voice input from the PWA. Prefer the browser transcript; fall back to an audio attachment."""
    check_auth(request)
    ctype = request.headers.get("content-type", "")

    if ctype.startswith("application/json"):
        body = await request.json()
        transcript = (body.get("text") or body.get("transcript") or "").strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="empty transcript")
        if not transcript.startswith("🎤"):
            transcript = "🎤 " + transcript
        meta = {"user": "human", "voice": True, "source": body.get("source") or "browser_speech"}
        api_sid = (body.get("api_session") or "").strip()[:120]
        if api_sid:
            meta["api_session"] = api_sid
        msg = save_message("in", "voice", transcript, meta)
        # 与 /app/send 共用 provider 入口；不改变转写或后续 TTS 逻辑。
        msg = await _dispatch_and_echo_incoming(msg)
        return {"id": msg["id"], "text": transcript}

    t_req = time.monotonic()
    data = await request.body()
    t_upload = time.monotonic()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="voice too large")

    mime = ctype or "audio/webm"
    upload = save_upload_bytes(data, request.query_params.get("name", "voice.webm"), mime, "voice")
    try:
        _a_dur = float(request.query_params.get("a_dur") or 0)
    except (TypeError, ValueError):
        _a_dur = 0.0
    if _a_dur > 0:
        # 前端录音秒数 → 附件 duration：语音条渲染的占位时长（前端解码后用真实值覆盖）
        upload["duration"] = round(_a_dur, 1)
    stored = Path(upload["url"]).name
    local_audio = UPLOAD_DIR / stored
    transcript = await transcribe_audio(local_audio, mime)
    t_stt = time.monotonic()
    qp = request.query_params
    is_call = bool(qp.get("call_id"))
    is_pushtalk = (qp.get("src") or "").strip() == "pushtalk"   # 聊天页按住说话（非通话）
    if is_call:
        # 七段计时的 upload/transcription 两段：只有通话轮打点（语音条聊天不打，免噪音）
        _vl("stt", None, force=True,
            call_id=(qp.get("call_id") or "")[:60], turn_id=(qp.get("turn_id") or "")[:60],
            upload_ms=round((t_upload - t_req) * 1000),
            transcription_ms=round((t_stt - t_upload) * 1000),
            stt_backend=resolve_stt_mode(), audio_bytes=len(data))
    if transcript and not substantive_text(transcript):
        # 实质人声判定（复盘坑 01）：纯事件描述（如 "[silence]"）/净文本为零 → 整段焚毁，
        # 不写账、不存音频。坑 05：这是有日志的丢弃出口，不是静默 return。
        # 坑 02 红线：只判零值，绝无单字/短句过滤——「哦」「嗯」在这里畅通。
        print(f"[stt] 丢弃纯事件转写（mode={resolve_stt_mode()} raw_len={len(transcript)} "
              f"bytes={len(data)} call={bool(is_call)} push={is_pushtalk}）", flush=True)
        try:
            local_audio.unlink()
        except OSError:
            pass
        return {"id": None, "text": "", "transcribed": False, "discarded": "non_speech"}
    if is_call or is_pushtalk:
        # 通话录音隐私边界：用完即弃。转写完成（无论成败）就地删音频文件，
        # meta 里也不留 attachment——通话轮的文字（转写）才是要留的痕迹。
        # 按住说话不同：音频是她的聊天附件，保留（与收藏语音同一存放方式）。
        if is_call:
            try:
                local_audio.unlink()
            except OSError:
                pass
    if not transcript and (is_call or is_pushtalk):
        # 通话/按住说话路径识别失败：不落库不广播（否则一条「未配置 ASR」的附件消息会引出 AI 对空气回复），
        # 前端拿 transcribed=false 自己重试。坑 05：丢弃出口必打日志（引擎空返回在这里现形）。
        print(f"[stt] 未转写出文本（mode={resolve_stt_mode()} bytes={len(data)} "
              f"call_id={(qp.get('call_id') or '')[:60] or '-'} push={is_pushtalk}）", flush=True)
        if is_pushtalk:
            try:
                local_audio.unlink()
            except OSError:
                pass
        return {"id": None, "text": "", "transcribed": False}
    # 声学行（复盘第 04 章）：前端本地采样的音量/基频统计，拼在转写后喂给模型看。
    # 数据来自手机本地（RMS + 自相关基频），零 API 成本；缺帧/老客户端自然没有这一行。
    audio_note = _audio_note_line(qp)
    text = ("🎤 " + transcript) if transcript else f"🎤 [语音] {HUMAN_NAME}发来一段语音；当前 relay 未配置 ASR，音频已作为附件送达。"
    if audio_note:
        text += "\n" + audio_note
    meta = {
        "user": "human",
        "voice": True,
        "source": "media_recorder",
        "attachments": [] if is_call else [upload],
        "transcribed": bool(transcript),
    }
    for k in ("call_id", "call_session_id", "turn_id"):   # 通话身份轻量落 meta，双工升级时按它对轮
        v = (qp.get(k) or "").strip()[:120]
        if v:
            meta[k] = v
    api_sid = (qp.get("api_session") or "").strip()[:120]
    if api_sid:
        meta["api_session"] = api_sid
    msg = save_message("in", "voice", text, meta)
    if is_call:
        _vl_note_t0(msg["id"])   # 之后 context/model/tts 各段都相对这个时刻
    # 与 /app/send 共用 provider 入口；provider 只接收已经完成的转写/附件消息，
    # 不参与 STT 和 TTS。API loop 本来就支持附件输入，空转写的普通语音条也由
    # 当前 provider 单路接收，不再同时投递给另一条运行时。
    msg = await _dispatch_and_echo_incoming(msg)
    return {"id": msg["id"], "text": transcript, "transcribed": bool(transcript), "attachment": upload}


@app.post("/app/call")
async def app_call(request: Request):
    """Call lifecycle events from the PWA so the AI knows this is voice, not typing."""
    check_auth(request)
    body = await request.json()
    action = (body.get("action") or "").strip().lower()
    call_id = (body.get("call_id") or "").strip()
    if action not in {"start", "end"}:
        raise HTTPException(status_code=400, detail="invalid call action")
    if action == "start":
        text = f"📞 [call_start] {HUMAN_NAME}开启了语音通话。接下来带 🎤 的消息来自语音。请用适合朗读的短句回复。"
    else:
        text = f"📞 [call_end] {HUMAN_NAME}结束了语音通话。"
    meta = {"user": "human", "call": action, "call_id": call_id}
    sid = (body.get("call_session_id") or "").strip()[:120]
    if sid:
        meta["call_session_id"] = sid
    api_sid = (body.get("api_session") or "").strip()[:120]
    if api_sid:
        meta["api_session"] = api_sid   # 通话轮与其回复要落进 PWA 当前打开的会话
    msg = save_message("in", "call", text, meta)
    if action == "end":
        await broadcast(plugin_subs, plugin_payload(msg))
    if action == "start":
        await broadcast(app_subs, {"type": "typing", "active": True})
    return {"id": msg["id"]}


ELEVENLABS_VOICE_SETTINGS = {
    # 官方未给 v4 标签专用数值；两组均保留 API 文档默认值，分别按模型族选择。
    "v4": {"stability": 0.5, "similarity_boost": 0.75},
    "other": {"stability": 0.5, "similarity_boost": 0.75},
}


async def _tts_synthesize(text: str, provider: str, custom_url: str, custom_method: str,
                          el_api_key: str = "", el_voice_id: str = "", el_model: str = "",
                          mm_api_key: str = "", mm_group_id: str = "", mm_voice_id: str = "",
                          mm_model: str = "", mi_api_key: str = "", mi_voice_id: str = "",
                          mo_api_key: str = "", mo_voice_id: str = "", mo_model: str = "",
                          mo_version: str = "", voice_params: dict | None = None,
                          voice_segment: bool = False) -> Response:
    """按 provider 路由 TTS，统一返回二进制音频（wav/mpeg）。前端不碰 base64。

    voice_params = [voice|…] 管道参数（instruction/sound_event，TASK-moss-sound-instruction）：
    只有 MOSS 分支透传进请求体；minimax 等不认这两个字段的 provider 一律丢弃不传
    （解析照做、剥离照做，参数绝不当正文念出来）。voice_segment=True 时，只对
    非 Eleven v4 模型在上游请求前清理方括号音频标签；消息/segments 中的原文不变。"""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    provider = (provider or "mimo").strip().lower()
    is_el_v4 = provider == "elevenlabs" and (el_model or "").strip().lower().startswith("eleven_v4")
    if voice_segment and not is_el_v4:
        text = strip_voice_audio_tags(text)
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
    headers = {"Cache-Control": "no-store"}
    vp = voice_params or {}

    if provider == "minimax":
        # 前端可自填 minimax 参数（mm_* 透传）；留空回退服务端 MINIMAX_* env（朗读/通话都走它）。
        return Response(content=minimax_tts_mp3(text, api_key=mm_api_key, group_id=mm_group_id,
                                                voice_id=mm_voice_id, model=mm_model),
                        media_type="audio/mpeg", headers=headers)

    if provider == "moss":
        # MOSS/MOSI：mo_* 透传；model 优先、version 备用并统一映射到上游 model；实际回 mp3。
        # instruction/sound_event 仅在 [voice|…] 带参数时下发；裸 [voice] 请求体五字段不变。
        # moss_tts_mp3 使用同步 HTTP；把它（以及下载退避 sleep）留在线程里，不能卡住
        # relay 的事件循环或同轮文本 SSE。总下载预算仍由 moss_tts_mp3 自身限制。
        audio = await asyncio.to_thread(
            moss_tts_mp3, text, api_key=mo_api_key, voice_id=mo_voice_id,
            model=mo_model, version=mo_version,
            instruction=vp.get("instruction", ""), sound_event=vp.get("sound_event", ""))
        return Response(content=audio,
                        media_type="audio/mpeg", headers=headers)

    if provider == "elevenlabs":
        if not el_api_key:
            raise HTTPException(status_code=400, detail="el_api_key missing")
        voice_id = el_voice_id or "21m00Tcm4TlvDq8ikWAM"
        model = el_model or "eleven_multilingual_v2"
        settings_family = "v4" if model.strip().lower().startswith("eleven_v4") else "other"
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        try:
            async with httpx.AsyncClient(timeout=TTS_UPSTREAM_TIMEOUT) as client:
                r = await client.post(
                    url,
                    headers={"xi-api-key": el_api_key, "Content-Type": "application/json"},
                    json={
                        "text": text,
                        "model_id": model,
                        "voice_settings": dict(ELEVENLABS_VOICE_SETTINGS[settings_family]),
                    },
                )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"elevenlabs upstream failed: {exc}")
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"elevenlabs upstream error: {r.text[:200]}")
        return Response(content=r.content, media_type="audio/mpeg", headers=headers)

    if provider == "custom":
        if not custom_url or not custom_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="custom_url must be http(s)")
        method = (custom_method or "GET").upper()
        try:
            async with httpx.AsyncClient(timeout=TTS_UPSTREAM_TIMEOUT) as client:
                if method == "POST":
                    r = await client.post(custom_url, json={"text": text})
                else:
                    r = await client.get(custom_url, params={"text": text})
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"tts upstream failed: {exc}")
        # OpenAI 风格 JSON+base64 → 解包成二进制；二进制直接透传
        if "json" in r.headers.get("content-type", ""):
            try:
                b64 = r.json()["choices"][0]["message"]["audio"]["data"]
                return Response(content=base64.b64decode(b64), media_type="audio/wav", headers=headers)
            except Exception:
                pass
        return Response(content=r.content, media_type=r.headers.get("content-type") or "audio/mpeg", headers=headers)

    # mimo v2.5 自填音色复刻：前端配了 mi_api_key → OpenAI 兼容 /v1/chat/completions（zero-shot 传样本）
    if provider == "mimo" and mi_api_key:
        return Response(content=mimo_tts(text, api_key=mi_api_key, voice=mi_voice_id),
                        media_type="audio/mpeg", headers=headers)

    # 默认 mimo（未配 key：旧无鉴权 GET 转发）
    try:
        async with httpx.AsyncClient(timeout=TTS_UPSTREAM_TIMEOUT) as client:
            r = await client.get(MIMO_TTS_URL, params={"text": text})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mimo tts failed: {exc}")
    try:
        b64 = r.json()["choices"][0]["message"]["audio"]["data"]
        audio = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=502, detail="mimo tts bad payload")
    return Response(content=audio, media_type="audio/wav", headers=headers)


@app.post("/app/tts")
async def app_tts(request: Request):
    """多提供商 TTS。body: {text, provider?, custom_url?, custom_method?, el_*?, mm_*?, mi_*?, mo_*?}。默认 mimo。"""
    check_auth(request)
    body = await request.json()
    return await _tts_synthesize(
        body.get("text", ""),
        body.get("provider", "mimo"),
        body.get("custom_url", ""),
        body.get("custom_method", "GET"),
        body.get("el_api_key", ""),
        body.get("el_voice_id", ""),
        body.get("el_model", ""),
        body.get("mm_api_key", ""),
        body.get("mm_group_id", ""),
        body.get("mm_voice_id", ""),
        body.get("mm_model", ""),
        body.get("mi_api_key", ""),
        body.get("mi_voice_id", ""),
        body.get("mo_api_key", ""),
        body.get("mo_voice_id", ""),
        body.get("mo_model", ""),
        body.get("mo_version", ""),
    )


@app.get("/app/tts")
async def app_tts_get(request: Request):
    """GET 变体（语音条）：?text=...&provider=...&custom_url=...&custom_method=...&el_*=&mm_*=&mo_*="""
    check_auth(request)
    qp = request.query_params
    return await _tts_synthesize(
        qp.get("text", ""),
        qp.get("provider", "mimo"),
        qp.get("custom_url", ""),
        qp.get("custom_method", "GET"),
        qp.get("el_api_key", ""),
        qp.get("el_voice_id", ""),
        qp.get("el_model", ""),
        qp.get("mm_api_key", ""),
        qp.get("mm_group_id", ""),
        qp.get("mm_voice_id", ""),
        qp.get("mm_model", ""),
        qp.get("mi_api_key", ""),
        qp.get("mi_voice_id", ""),
        qp.get("mo_api_key", ""),
        qp.get("mo_voice_id", ""),
        qp.get("mo_model", ""),
        qp.get("mo_version", ""),
    )


@app.post("/app/voice_mode")
async def app_voice_mode(request: Request):
    """前端 RHYTHM 卡分段/语音/贴纸开关 + TTS 提供商配置整包同步。切分与 [voice] 合成是两件事。
    STT 四字段不在这收（2026-09-01 根因 A 定案）：页面加载/刷新/重连都会整包 POST，
    前端 localStorage 旧快照会把服务端切的 STT 配置打回空。STT 服务端权威（voice_mode.json
    唯一真相源），唯一写入口是 POST /app/stt_config 单字段 patch；这里收到 stt_* 一律丢弃并留痕。"""
    check_auth(request)
    body = await request.json()
    global segment_mode_on, voice_mode_on, sticker_mode_on
    if "segment_enabled" in body:
        segment_mode_on = bool(body["segment_enabled"])
    if "voice_enabled" in body:
        voice_mode_on = bool(body["voice_enabled"])
    if "sticker_enabled" in body:
        sticker_mode_on = bool(body["sticker_enabled"])
    for k in voice_tts_cfg:
        if k in body:
            if k.endswith("_api_key") and not str(body.get(k) or "").strip():
                continue
            voice_tts_cfg[k] = body.get(k) or ""
    dropped_stt = [k for k in voice_stt_cfg if k in body]
    if dropped_stt:
        # 老缓存前端（或回归的整包同步）还在发 STT 字段 → 丢弃 + journal 留痕，能看出哪台设备没更新
        print(f"[stt-config] 整包同步里的 STT 字段已忽略（服务端权威，显式改动走 /app/stt_config）: "
              f"{','.join(dropped_stt)}", flush=True)
    _save_voice_state()
    # 摘要日志：只记 provider 和 key 是否在场（值为空是静默合成失败的常见根因），永不落 key 明文
    print(f"[voice-mode] sync provider={voice_tts_cfg.get('provider') or '(空)'} "
          f"segment={segment_mode_on} voice={voice_mode_on} sticker={sticker_mode_on} "
          f"stt={voice_stt_cfg.get('stt_provider') or '(env)'} "
          f"stt_keyterms={len(stt_keyterms_list())}条 "
          f"keys_present={{mm:{bool(voice_tts_cfg.get('mm_api_key'))}, "
          f"mm_vid:{bool(voice_tts_cfg.get('mm_voice_id'))}, mo:{bool(voice_tts_cfg.get('mo_api_key'))}, "
          f"el:{bool(voice_tts_cfg.get('el_api_key'))}, mi:{bool(voice_tts_cfg.get('mi_api_key'))}}}", flush=True)
    return {"ok": True, "segment_mode": segment_mode_on, "voice_mode": voice_mode_on,
            "sticker_mode": sticker_mode_on}


@app.post("/app/stt_config")
async def app_stt_config(request: Request):
    """STT 卡显式改动专用：单字段 patch（voice_mode.json 是 STT 配置唯一真相源）。
    body 只认 stt_provider / stt_model / stt_lang / stt_keyterms 的子集（≥1 个），其余 400。
    stt_provider 合法值：api / cmd / whisper / elevenlabs / off / 空（空=跟随 env）。"""
    check_auth(request)
    body = await request.json()
    if not isinstance(body, dict) or not body:
        raise HTTPException(status_code=400, detail="empty payload")
    unknown = [k for k in body if k not in voice_stt_cfg]
    if unknown:
        raise HTTPException(status_code=400, detail="unknown stt fields: " + ",".join(map(str, unknown)))
    if "stt_provider" in body:
        v = str(body.get("stt_provider") or "").strip().lower()
        if v and v not in ("api", "cmd", "whisper", "elevenlabs", "off"):
            raise HTTPException(status_code=400, detail="bad stt_provider")
        voice_stt_cfg["stt_provider"] = v
    for k in ("stt_model", "stt_lang", "stt_keyterms"):
        if k in body:
            voice_stt_cfg[k] = str(body.get(k) or "")
    _save_voice_state()
    mode = resolve_stt_mode()
    print(f"[stt-config] patch fields={','.join(body.keys())} "
          f"stt={voice_stt_cfg.get('stt_provider') or '(env)'} resolved={mode} "
          f"model={stt_effective_model(mode) or '(默认)'} "
          f"keyterms={len(stt_keyterms_list())}条", flush=True)
    return {"ok": True, "stt_provider": voice_stt_cfg.get("stt_provider") or "",
            "stt_mode_resolved": mode, "stt_effective_model": stt_effective_model(mode)}


@app.get("/app/voice_mode")
async def app_voice_mode_get(request: Request):
    """STT 卡 bootstrap：回当前卡配置 + 服务端 key 是否在场（只回布尔，永不回 key 值/明文）。
    key 只存 /etc/valelin/relay.env（RELAY_STT_API_KEY / RELAY_STT_EL_KEY），浏览器零 key。"""
    check_auth(request)
    return {
        "stt_provider": voice_stt_cfg.get("stt_provider") or "",
        "stt_model": voice_stt_cfg.get("stt_model") or "",
        "stt_lang": voice_stt_cfg.get("stt_lang") or "",
        "stt_keyterms": voice_stt_cfg.get("stt_keyterms") or "",
        "stt_key_present": {"api": bool(STT_API_KEY), "elevenlabs": bool(STT_EL_KEY)},
        "stt_mode_env": STT_MODE,
        "stt_mode_resolved": resolve_stt_mode(),
        "stt_effective_model": stt_effective_model(resolve_stt_mode()),
        "segment_mode": segment_mode_on,
        "voice_mode": voice_mode_on,
        "sticker_mode": sticker_mode_on,
        "tts": {k: v for k, v in voice_tts_cfg.items() if not k.endswith("_api_key")},
    }


@app.post("/app/latency")
async def app_latency(request: Request):
    """客户端 voice_latency 打点回传（endpoint/upload/首声只有手机端知道）。只打日志，零存储。"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="bad payload")
    print(f"[voice_latency] {json.dumps({'event': 'voice_latency', 'side': 'client', **body},
          ensure_ascii=False)[:800]}", flush=True)
    return {"ok": True}


# ── 服务端语音合成辅助（SSE 随流推音频）──────────────────────────────────────

def _voice_duration(audio: bytes) -> float:
    """WAV/MP3 通用时长（秒）。RIFF/WAVE 头解析；MP3 跳 ID3 后按首帧头 CBR 估算。解析失败返回 0。"""
    try:
        if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
            i, channels, sample_rate, bits = 12, 0, 0, 0
            while i + 8 <= len(audio):
                cid = audio[i:i + 4]
                size = int.from_bytes(audio[i + 4:i + 8], "little")
                body = audio[i + 8:i + 8 + size]
                if cid == b"fmt " and len(body) >= 16:
                    channels = int.from_bytes(body[2:4], "little")
                    sample_rate = int.from_bytes(body[4:8], "little")
                    bits = int.from_bytes(body[14:16], "little")
                elif cid == b"data":
                    bps = max(1, channels * (bits // 8))
                    if sample_rate and bps:
                        return size / (sample_rate * bps)
                    return 0.0
                i += 8 + size + (size & 1)
            return 0.0
        # MP3：跳 ID3，扫首个 MPEG1 Layer III 帧头取 bitrate/采样率，按 CBR 估时长。
        # 用标准 MPEG1 Layer III bitrate 表（index 9 = 128kbps）；版本位 (b1 & 0x18) == 0x18 限定 MPEG1。
        i = 0
        n = len(audio)
        if audio[:3] == b"ID3":
            i = 10 + ((audio[6] << 21) | (audio[7] << 14) | (audio[8] << 7) | audio[9])
        bitrates = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
        srates = [44100, 48000, 32000, 22050]
        while i + 4 <= n:
            b1, b2, _ = audio[i + 1], audio[i + 2], audio[i + 3]
            if audio[i] == 0xFF and (b1 & 0xE0) == 0xE0 and (b1 & 0x18) == 0x18:
                layer = (b1 >> 1) & 0x03
                br_i = (b2 >> 4) & 0x0F
                sr_i = (b2 >> 2) & 0x03
                if layer == 0x01 and br_i not in (0, 15) and sr_i < 4 and srates[sr_i] and bitrates[br_i]:
                    return (n - i) / (bitrates[br_i] * 1000 / 8.0)
            i += 1
        return 0.0
    except Exception:
        pass
    return 0.0


def _ts_plus_ms(iso: str, ms: int) -> str:
    """ISO 时间戳加毫秒。解析失败原样返回（保序兜底：thinking 在 reply 前靠 ts 相等 + id 兜底）。"""
    try:
        return (datetime.fromisoformat(iso) + timedelta(milliseconds=ms)).isoformat()
    except Exception:
        return iso


def _strip_inline_think(s: str) -> str:
    """剥掉流式文本里意外混入的 <think> 块/开标签（思考走 thinking_delta，reply 流不应有）。"""
    s = re.sub(r"<(?:think|thinking)>[\s\S]*?</(?:think|thinking)>", "", s or "", flags=re.I)
    s = re.sub(r"<(?:think|thinking)>[\s\S]*$", "", s, flags=re.I)
    return s.strip()


def _clean_seg_text(s: str) -> str:
    """段文本清洗：剥意外混入的 <think> 块、旧句子级 <voice> 标签、[voice] 标记
    （判断误落文字路径时兜底）；[br] 残留 → 换行（正常切分路径已消费标记，这里兜底防漏出）。
    注意：语音路径的正文不用这个（要保留内容只剥标记）。"""
    s = _strip_inline_think(s)
    s = LEGACY_VOICE_TAG_RE.sub("", s)
    s = VOICE_MARK_RE.sub("", s)
    s = BR_MARK_RE.sub("\n", s)
    return s.strip()


def _cut_sentences(region: str) -> tuple[list[str], str]:
    """层 3 兜底专用：从 region 头部切出完整句子（到每个句末标点为止）。返回 (句子们, 未闭合尾巴)。
    只在 _fallback_cut（400 字无换行）里调用——正常文本永不按标点分气泡。"""
    sentences, pos = [], 0
    while True:
        m = SENTENCE_END_RE.search(region[pos:])
        if not m:
            break
        end = pos + m.end()
        s = _clean_seg_text(region[pos:end])
        pos = end
        # 纯标点（"！"、"……"）或清洗后为空（整句都是旧标签）不成句
        if s and not re.fullmatch(r"[。！？!?\s]+", s):
            sentences.append(s)
    return sentences, region[pos:]


def _split_br(text: str) -> list[str]:
    """层 1：按显式标记 [br] 切段（大小写不敏感）。连续标记/首尾标记由滤空消化，不产生空气泡。
    标记本身随切分消费，不进气泡。"""
    if not text:
        return []
    return [p for p in BR_MARK_RE.split(text) if p.strip()]


def _fallback_cut(piece: str) -> list[str]:
    """层 3 防御兜底（唯一触发条件）：单个气泡文本超 400 字且内部完全没有换行，
    才按句末标点切多段（Lin 拍板 2026-09-03），防止无法阅读的巨型气泡。
    触发记 INFO 日志——要知道兜底有没有在被触发；正常说话永远走不到这。
    收尾引号规则（SENTENCE_END_RE）只在这条路径生效：『……小猫。」』的 」跟前句走。"""
    if len(piece) <= TEXT_BUBBLE_MAX_CHARS or "\n" in piece:
        return [piece]
    sentences, keep = _cut_sentences(piece)
    out = list(sentences)
    keep = _clean_seg_text(keep)
    if keep:
        out.append(keep)
    print(f"[bubble-split] INFO 400字无换行兜底触发 len={len(piece)} → {len(out)} 段", flush=True)
    return out


def _split_text_bubbles(block: str) -> list[str]:
    """文字 block → 文字气泡列表（规范 §5，与前端 splitTextBubbles 同规则）：
    层 1 [br] 切 → 每段清洗 → 层 3 400 字无换行兜底。单换行不分，段内句号不切。
    尾部残缺 [br 前缀剥掉（半个标记不渲染）。"""
    block = BR_PARTIAL_TAIL_RE.sub("", block or "")
    out: list[str] = []
    for piece in _split_br(block):
        piece = _clean_seg_text(piece)
        if not piece:
            continue
        for s in _fallback_cut(piece):
            if s:
                out.append(s)
    return out


# ══════════════ 语音 · 气泡分割（显式驱动重做，TASK-bubble-split-redo）══════════════
# 层 2 双换行切 block（整块结构空行不切）→ 每 block 首行 [voice] → 语音气泡，否则 → 文字气泡。
# 流式边攒边切：攒到一个双换行就处理一个 block 推出去，不等整条流完（首泡优先：
# 第一个气泡闭合即合成，不在块内切句——2026-09-03 Lin 拍板，取代旧首句优先）。

def _voice_pipe_params(line: str) -> dict:
    """首行 [voice|k:v|k:v] 管道参数 → {k: v}（TASK-moss-sound-instruction 第三节）。

    只认 instruction / sound_event，未知参数名静默忽略（不报错、不透传）；参数顺序任意、
    0~2 个都行；值里可有空格/逗号/英文标点，到 ] 为止。裸 [voice] / · 时长 · 情绪形 → {}。
    """
    m = VOICE_MARK_RE.match((line or "").strip())
    if not m or "|" not in m.group(1):
        return {}
    out: dict = {}
    for part in m.group(1)[1:-1].split("|")[1:]:    # 去括号；首段恒为 "voice"
        k, _, v = part.partition(":")
        k = k.strip().lower()
        if k in VOICE_KNOWN_PARAMS and v.strip():
            out[k] = v.strip()
    return out


def _parse_voice_block(block: str) -> tuple[str, str, dict]:
    """语音 block：剥首行 [voice] 标记取 TTS 文本，可选第二行 [zh] 存中文翻译（不念）。
    返回 (tts 文本, zh 文本, 管道参数)。zh 空 = 没有 [zh] 行；参数空 dict = 无/裸 [voice]。"""
    lines = [ln.strip() for ln in (block or "").split("\n")]
    params = _voice_pipe_params(lines[0]) if lines else {}
    tts = VOICE_MARK_RE.sub("", lines[0]).strip() if lines else ""
    zh = ""
    i = 1
    if len(lines) > 1 and re.match(r"^\[zh\]\s*", lines[1]):
        zh = re.sub(r"^\[zh\]\s*", "", lines[1]).strip()
        i = 2
    tail = "\n".join(lines[i:]).strip()
    if tail:
        tts = (tts + "\n" + tail).strip() if tts else tail
    tts = re.sub(r"</?voice>", "", tts).strip()   # 旧句子级标签兜底：剥标签留内容
    tts = BR_PARTIAL_TAIL_RE.sub("", tts).strip() # 尾部残缺 [br 前缀剥掉（半个标记不进 TTS）
    tts = _strip_inline_think(tts)
    return tts, zh, params


def _split_voice_tts(text: str) -> list[str]:
    """语音块超长处理：把 [voice] 文本切成若干段，全部朗读，不丢任何片段。

    短文本（≤ VOICE_MAX_CHARS）原样一条。超长从 start 起每次在「距起点 ≤ VOICE_MAX_CHARS
    的最后一个句末标点（中英都认）」处切段；段内无标点则退到最近空格断词，绝不拦腰切单词。
    段与段直接切片拼接 == 原文（只去头尾空白），无片段丢失。返回段列表（每段都进 TTS）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= VOICE_MAX_CHARS:
        return [text]
    bounds = [m.end() for m in VOICE_SPLIT_END_RE.finditer(text)]  # 句末标点后一字符
    pieces: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = start + VOICE_MAX_CHARS
        candidates = [b for b in bounds if start < b <= end]
        if candidates:
            cut = candidates[-1]                 # 段内最远的句末标点 → 整句完整
        else:
            sp = text.rfind(" ", start, end)     # 无标点：最近空格断词
            cut = sp if sp > start else end      # 无空格 → 硬切（极端情况）
        pieces.append(text[start:cut])
        start = cut
    return pieces  # 原样切片（入口已 strip），拼接即原文，无片段丢失


def _split_voice_bubbles(tts: str, params: dict) -> list[str]:
    """语音 block → 语音气泡列表（规范 §6，一个气泡 = 一次 TTS = 一条语音）：
    层 1 [br] 分条（一个 [br] = 一条新语音，管道参数每条同参）→ 每条过超长保护。
    带管道参数的条目是不可分割单元：跳过超长保护，内部任何标点/换行/兜底都不切（只加约束，
    解析复用现有实现）；裸 [voice] 保留 VOICE_MAX_CHARS 超长保护（拆段全朗读不丢片段）。"""
    pieces: list[str] = []
    for part in _split_br(tts):
        if params:
            pieces.append(part)
        else:
            pieces.extend(_split_voice_tts(part))
    return pieces


async def _process_block(stream_id: str, draft: dict, block: str) -> None:
    """处理一个完整 block：贴纸、[file:]、[voice] 或文字段（按此顺序互斥）。"""
    block = (block or "").strip()
    if not block:
        return
    sm = STICKER_RE.match(block)
    if sm:
        # 合法 id → 贴纸段（不做 TTS、不参与 VOICE_MAX_CHARS）；编造 id → 静默丢弃（记 warn）
        if _sticker_known(sm.group(1)):
            await _push_sticker_msg(stream_id, draft, sm.group(1))
        else:
            print(f"[sticker] drop unknown id {sm.group(1)!r} from stream {stream_id}", flush=True)
        return
    if FILE_MARK_RE.match(block):
        seen = int(draft.get("file_markers_seen") or 0) + 1
        draft["file_markers_seen"] = seen
        if seen > FILE_DELIVERY_MAX_PER_REPLY:
            print(f"[file-delivery] WARNING stream={stream_id} ignored marker #{seen} (max {FILE_DELIVERY_MAX_PER_REPLY})", flush=True)
            return
        attachment, error = _deliver_model_file(block)
        if attachment:
            await _push_file_msg(stream_id, draft, attachment)
        else:
            await _push_file_error_msg(stream_id, draft, error or "文件发送失败")
        return
    if VOICE_MARK_RE.match(block) and voice_mode_on:
        tts, zh, params = _parse_voice_block(block)
        if not tts:
            return
        pieces = _split_voice_bubbles(tts, params)
        for i, piece in enumerate(pieces):
            # [zh] 翻译只挂 block 的第一个语音气泡（它是整块的翻译，拆条后对不上后半条）；
            # 管道参数（instruction/sound_event）挂整块 → 拆出的每条都用同一组参数合成。
            await _push_voice_msg(stream_id, draft, piece, zh if i == 0 else "", params)
    else:
        for s in _split_text_bubbles(block):
            await _push_text_msg(stream_id, draft, s)


def _fence_open(block: str) -> bool:
    """代码围栏是否未闭合：以 ``` 开头的行数为奇数。围栏内的双换行总是内部空行。"""
    fence = 0
    for ln in (block or "").split("\n"):
        if ln.lstrip().startswith("```"):
            fence += 1
    return fence % 2 == 1


def _block_struct_open(block: str) -> bool:
    """block 是否处于「结构未闭合」态：围栏奇数，或末条非空行是结构行
    （列表/表格/引用/围栏/缩进代码）。结构行后的空行可能只是结构内部的空行（现象 A）。"""
    last = ""
    for ln in (block or "").split("\n"):
        if ln.strip():
            last = ln
    if _fence_open(block):
        return True
    return bool(last) and bool(STRUCT_LINE_RE.match(last))


def _split_blocks(buf: str, final: bool = False) -> tuple[str, list[str]]:
    """层 2：按双换行（中间可夹空白）切 block，整块结构的空行不切（规范 §3/§4，现象 A 修复）。

    闭合判定：围栏未闭合 → 总是 hold；末行是结构行 → 看前瞻（双换行后第一条非空行），
    前瞻行是结构行 → 空行是结构内部的，跳过继续扫；前瞻行不是（或流还没到）→ hold 等数据。
    其余 → 在该双换行处闭合。流式（final=False）无前瞻数据时把双换行留在 buffer 里等更多流；
    done 收尾（final=True）无前瞻数据就在该处闭合（整块 flush）。
    返回 (剩余未闭合 buffer, [完整 blocks])。"""
    blocks: list[str] = []
    remaining = buf or ""
    scan = 0
    while True:
        m = BREAK_RE.search(remaining, scan)
        if not m:
            break
        block = remaining[:m.start()]
        rest = remaining[m.end():]
        if block.strip() and not _block_struct_open(block):
            blocks.append(block)
            remaining = rest
            scan = 0
            continue
        if not block.strip():          # 空候选（连续空行）：吞掉断行继续
            remaining = rest
            scan = 0
            continue
        # 结构 hold：围栏内空行总是内部；其余结构看前瞻是否延续
        nl = next((ln for ln in rest.split("\n") if ln.strip()), None)
        if nl is None:
            if final:
                blocks.append(block)
                remaining = rest
                scan = 0
                continue
            break                       # 前瞻数据没到，等更多流（双换行留在 buffer）
        if _fence_open(block) or STRUCT_LINE_RE.match(nl):
            scan = m.end()              # 结构内部空行：跳过这个断行继续找下一个
            continue
        blocks.append(block)
        remaining = rest
        scan = 0
    return remaining, blocks


async def _process_buffered_blocks(stream_id: str, draft: dict, final: bool = False) -> None:
    """把 seg_buf 里所有完整 block（按双换行切，结构 hold）逐个处理推送，剩余未闭合的留在 seg_buf。
    done 收尾传 final=True：无前瞻数据的结构 block 整块闭合。"""
    remaining, blocks = _split_blocks(draft.get("seg_buf") or "", final=final)
    for block in blocks:
        await _process_block(stream_id, draft, block)
    draft["seg_buf"] = remaining


def _stream_lock(stream_id: str) -> asyncio.Lock:
    lock = voice_stream_locks.get(stream_id)
    if lock is None:
        lock = voice_stream_locks[stream_id] = asyncio.Lock()
    return lock


def _ensure_stream_msg(stream_id: str, draft: dict) -> int:
    """segment_mode_on：首条 reply_delta 时预插 reply 占位行，拿真实 msg_id 供所有段事件引用。

    占位行 text 为空、meta.streaming=True；done 时更新为 content+segments。流没完成（超时
    prune / 空回复）会被删除，不留僵尸行。
    """
    msg_id = draft.get("msg_id")
    if msg_id:
        return int(msg_id)
    meta = dict(draft.get("meta") or {})
    meta["stream_id"] = stream_id
    meta["streaming"] = True
    meta["ts"] = draft.get("ts") or now_iso()
    msg = save_message("out", "reply", "", meta)
    draft["msg_id"] = msg["id"]
    return msg["id"]


def _delete_stream_placeholder(msg_id) -> None:
    """删掉仍未落库的占位行（meta.streaming=True 才算，避免误删已落库消息）。"""
    if not msg_id:
        return
    try:
        with db() as conn:
            row = conn.execute("SELECT meta FROM messages WHERE id=?", (msg_id,)).fetchone()
            if row:
                meta = json.loads(row["meta"] or "{}")
                if meta.get("streaming"):
                    conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
                    conn.commit()
    except Exception:
        pass


def _cleanup_stream_draft(key: tuple, draft: dict) -> None:
    """prune 超时 draft：删掉未落库的占位行 + 释放 per-stream 锁。"""
    _delete_stream_placeholder(draft.get("msg_id"))
    voice_stream_locks.pop(key[0], None)


async def _synth_voice_final(msg_id: int, seq: int, text: str,
                             params: dict | None = None) -> tuple[bool, float, str, int]:
    """合成单个 voice 段（用前端选的 provider），写 {msg_id}_{seq}.{wav|mp3}。
    params = [voice|…] 管道参数（instruction/sound_event），只对认这些字段的 provider 生效。
    返回 (ok, duration, ext, synth_ms)。失败不抛（ext 空串）。"""
    t_synth0 = time.monotonic()
    try:
        cfg = voice_tts_cfg
        async with _tts_lock:
            resp = await _tts_synthesize(
                text, cfg.get("provider") or "mimo",
                cfg.get("custom_url", ""), cfg.get("custom_method", "GET"),
                cfg.get("el_api_key", ""), cfg.get("el_voice_id", ""), cfg.get("el_model", ""),
                cfg.get("mm_api_key", ""), cfg.get("mm_group_id", ""),
                cfg.get("mm_voice_id", ""), cfg.get("mm_model", ""),
                cfg.get("mi_api_key", ""), cfg.get("mi_voice_id", ""),
                cfg.get("mo_api_key", ""), cfg.get("mo_voice_id", ""),
                cfg.get("mo_model", ""), cfg.get("mo_version", ""),
                voice_params=params, voice_segment=True,
            )
        synth_ms = round((time.monotonic() - t_synth0) * 1000)
        audio = resp.body
        if not audio:
            print(f"[voice-tts] 合成失败 provider={cfg.get('provider') or 'mimo'} msg={msg_id} seq={seq}: "
                  f"上游返回空音频（HTTP {getattr(resp, 'status_code', '?')}）", flush=True)
            return False, 0.0, "", synth_ms
        ext = ".mp3" if (resp.media_type or "").startswith("audio/mpeg") else ".wav"
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        (VOICE_DIR / f"{msg_id}_{seq}{ext}").write_bytes(audio)
        return True, round(_voice_duration(audio), 2), ext, synth_ms
    except Exception as exc:
        # 失败必须留痕：这里静默吞掉过一次 minimax 故障，url=null 前端降级文字，journal 里查无可查
        print(f"[voice-tts] 合成失败 provider={cfg.get('provider') or 'mimo'} msg={msg_id} seq={seq}: "
              f"{type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return False, 0.0, "", round((time.monotonic() - t_synth0) * 1000)


async def _push_text_msg(stream_id: str, draft: dict, text: str) -> None:
    """推一条 text_msg 段事件并记进 draft['segments']（seq 全回复唯一递增）。"""
    seq = draft.get("seg_seq") or 0
    msg_id = draft.get("msg_id")
    await broadcast(app_subs, {
        "type": "text_msg", "msg_id": msg_id, "seq": seq, "text": text,
        "stream_id": stream_id, "ts": draft.get("ts") or "",
        "api_session": (draft.get("meta") or {}).get("api_session") or "",
    })
    draft.setdefault("segments", []).append({"seq": seq, "kind": "text", "text": text})
    draft["seg_seq"] = seq + 1


async def _push_voice_msg(stream_id: str, draft: dict, text: str, zh: str = "",
                          params: dict | None = None) -> None:
    """推一条 voice_msg（一个 voice block 一条）：先合成再推，失败 url:null 前端降级文字。
    zh 是可选的中文翻译（[zh] 第二行），只存转写用、不念。params 是 [voice|…] 管道参数
    （instruction/sound_event），随合成下发；非 MOSS provider 或裸 [voice] 时为空、行为不变。"""
    seq = draft.get("seg_seq") or 0
    msg_id = draft.get("msg_id")
    # Recording omitted pending provenance; use the existing text fallback.
    ok, dur, ext, synth_ms = False, 0.0, ".mp3", 0
    url = None
    # tts_first_audio 段：第一个 voice 段「合成完+已推 SSE」= 服务端第一声可下载的时刻
    src_id = (draft.get("meta") or {}).get("source_id")
    if not draft.get("_vl_tts_logged"):
        draft["_vl_tts_logged"] = True
        _vl("tts_first_audio", src_id, msg_id=msg_id, seq=seq, ok=ok, synth_ms=synth_ms,
           provider=(voice_tts_cfg.get("provider") or "mimo"))
    await broadcast(app_subs, {
        "type": "voice_msg", "msg_id": msg_id, "seq": seq, "text": text,
        "zh": zh or "", "url": url, "duration": dur if ok else 0.0, "stream_id": stream_id,
        "ts": draft.get("ts") or "",
        "api_session": (draft.get("meta") or {}).get("api_session") or "",
    })
    entry = {"seq": seq, "kind": "voice", "text": text, "url": url, "duration": dur if ok else 0.0}
    if zh:
        entry["zh"] = zh
    draft.setdefault("segments", []).append(entry)
    draft["seg_seq"] = seq + 1


async def _push_sticker_msg(stream_id: str, draft: dict, sid: str) -> None:
    """推一条 sticker_msg（一个贴纸独立成段）：无音频、不打字，前端查 stickerMap 渲染图片。
    id 已由 _process_block 查表校验过（合法才到这里）。"""
    seq = draft.get("seg_seq") or 0
    msg_id = draft.get("msg_id")
    await broadcast(app_subs, {
        "type": "sticker_msg", "msg_id": msg_id, "seq": seq, "id": sid,
        "text": ":" + sid + ":", "stream_id": stream_id,
        "ts": draft.get("ts") or "",
        "api_session": (draft.get("meta") or {}).get("api_session") or "",
    })
    draft.setdefault("segments", []).append({"seq": seq, "kind": "sticker", "id": sid, "text": ":" + sid + ":"})
    draft["seg_seq"] = seq + 1


async def _push_file_msg(stream_id: str, draft: dict, attachment: dict) -> None:
    """推一条 file_msg，并把同一附件落进 reply meta，复用既有卡片与下载命名。"""
    seq = draft.get("seg_seq") or 0
    msg_id = draft.get("msg_id")
    delivered = dict(attachment)
    delivered["segment_seq"] = seq
    await broadcast(app_subs, {
        "type": "file_msg", "msg_id": msg_id, "seq": seq, "attachment": delivered,
        "stream_id": stream_id, "ts": draft.get("ts") or "",
        "api_session": (draft.get("meta") or {}).get("api_session") or "",
    })
    draft.setdefault("attachments", []).append(delivered)
    draft.setdefault("segments", []).append({"seq": seq, "kind": "file", "text": "", "attachment": delivered})
    draft["seg_seq"] = seq + 1


async def _push_file_error_msg(stream_id: str, draft: dict, error: str) -> None:
    """文件标记失败也要占一个终态段，不能静默吞掉。"""
    seq = draft.get("seg_seq") or 0
    msg_id = draft.get("msg_id")
    text = _file_error_segment(error)["text"]
    await broadcast(app_subs, {
        "type": "file_error", "msg_id": msg_id, "seq": seq, "text": text,
        "stream_id": stream_id, "ts": draft.get("ts") or "",
        "api_session": (draft.get("meta") or {}).get("api_session") or "",
    })
    draft.setdefault("segments", []).append({"seq": seq, "kind": "file_error", "text": text})
    draft["seg_seq"] = seq + 1


async def _finalize_stream_reply(stream_id: str, draft: dict) -> dict:
    """segment_mode_on 的 reply done：flush 剩余 block（final=True，结构 block 整块闭合）
    → 落库（thinking 独立消息 + reply 占位更新）→ 广播。

    content 保留完整原文（含 [voice]/[zh]/[br] 标记，供上下文回送）；segments 按序落 meta。
    流式中已按双换行逐 block 推送，done 只 flush 最后一个未闭合 block（可能无双换行结尾；
    一次性 done 直接取全文切完）。流结束时 typing 一定清掉。
    """
    await broadcast(app_subs, {"type": "typing", "active": False})

    # flush 剩余 buffer：流式时 seg_buf 存最后未闭合的 block；一次性 done（无流式块）直接取全文
    if not draft.get("seg_buf") and not draft.get("segments"):
        draft["seg_buf"] = draft.get("text") or ""
    await _process_buffered_blocks(stream_id, draft, final=True)   # 切出并处理所有双换行块
    tail = (draft.get("seg_buf") or "").strip()
    if tail:
        await _process_block(stream_id, draft, tail)   # 最后一个 block（可能无双换行结尾）

    text = draft.get("text") or ""
    segs = split_think_blocks(text)
    if not segs:
        segs = [("reply", text)]
    thinking_parts = [t for k, t in segs if k == "thinking"]
    # 贴纸：编造 id 的独立段静默丢弃（不落库、不渲染）。合法 id 保留（历史仍渲染）。
    reply_text = _strip_invalid_sticker_blocks("".join(t for k, t in segs if k == "reply")).strip()
    msg_id = draft.get("msg_id")

    if not reply_text and not thinking_parts:
        _delete_stream_placeholder(msg_id)
        return {"ok": True, "stream_id": stream_id, "saved": False}

    _meta = dict(draft.get("meta") or {})
    with db() as conn:
        activity = activity_for_run(conn, run_id_for_stream(stream_id))
    if activity:
        _meta["activity"] = activity
    _group = str(_meta.get("source_id") or "").strip() or None
    ts_base = draft.get("ts") or now_iso()

    # thinking（若有）：独立消息，ts = 流起始（排序在 reply 前）
    thinking_msg = None
    for thought in thinking_parts:
        t_meta = dict(_meta)
        t_meta["stream_id"] = stream_id
        t_meta["ts"] = ts_base
        if _group:
            t_meta["reroll_group"] = _group
            t_meta["variant_index"] = 1
            t_meta["variant_active"] = 1
        thinking_msg = save_message("out", "thinking", thought, t_meta)

    if not reply_text:
        _delete_stream_placeholder(msg_id)
        if activity:
            with db() as conn:
                persist_activity_for_stream(conn, stream_id, activity)
                conn.commit()
        if thinking_msg:
            await broadcast(app_subs, app_payload(thinking_msg))
        return {"ok": True, "stream_id": stream_id, "saved": False}

    r_meta = dict(_meta)
    r_meta["stream_id"] = stream_id
    r_meta["segments"] = draft.get("segments") or []
    if draft.get("attachments"):
        r_meta["attachments"] = draft["attachments"]
    if _group:
        r_meta["reroll_group"] = _group
        r_meta["variant_index"] = 1
        r_meta["variant_active"] = 1
    # ts：有 thinking 时 reply 后移 1ms，保证时间排序 thinking 在前（占位行 id 更小，等 ts 会排反）
    reply_ts = _ts_plus_ms(ts_base, 1) if thinking_parts else ts_base
    with db() as conn:
        conn.execute(
            "UPDATE messages SET text=?, meta=?, ts=? WHERE id=?",
            (reply_text, json.dumps(r_meta, ensure_ascii=False), reply_ts, msg_id),
        )
        if activity:
            persist_activity_for_stream(conn, stream_id, activity)
        conn.commit()
    msg = {"id": msg_id, "ts": reply_ts, "direction": "out", "kind": "reply",
           "text": reply_text, "meta": r_meta}
    await _queue_local_message(msg)

    if thinking_msg:
        await broadcast(app_subs, app_payload(thinking_msg))
    await broadcast(app_subs, app_payload(msg))

    if not has_visible_app_subscribers():
        try:
            await push_to_all(notification_from_message(msg))
        except Exception:
            pass
    return {"id": msg_id, "ids": [msg_id], "stream_id": stream_id, "saved": True}


async def _attach_reply_segments(msg: dict, reply_text: str) -> None:
    """非流式 reply + segment_mode on：气泡分割（双换行切 block、结构 hold，与流式同算法
    `_split_blocks` → 每 block 判语音/文字）挂 msg meta。

    [voice] block + voice_mode_on → 语音气泡（[br] 分条、参数块不可分割、[zh] 存 zh）；
    否则文字气泡（[br] 分隔、400 字无换行兜底，见 _split_text_bubbles）。
    """
    text = reply_text or ""
    remaining, blocks = _split_blocks(text, final=True)
    tail = remaining.strip()
    if tail:
        blocks.append(tail)
    segs: list[dict] = []
    attachments: list[dict] = []
    file_markers_seen = 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        sm = STICKER_RE.match(block)
        if sm:
            # 合法 id → 贴纸段；编造 id → 静默丢弃（记 warn）
            if _sticker_known(sm.group(1)):
                segs.append({"kind": "sticker", "id": sm.group(1), "text": block})
            else:
                print(f"[sticker] drop unknown id {sm.group(1)!r} from reply {msg.get('id')}", flush=True)
            continue
        if FILE_MARK_RE.match(block):
            file_markers_seen += 1
            if file_markers_seen > FILE_DELIVERY_MAX_PER_REPLY:
                print(f"[file-delivery] WARNING reply={msg.get('id')} ignored marker #{file_markers_seen} (max {FILE_DELIVERY_MAX_PER_REPLY})", flush=True)
                continue
            attachment, error = _deliver_model_file(block)
            if attachment:
                attachment = dict(attachment)
                attachment["segment_seq"] = len(segs)
                attachments.append(attachment)
                segs.append({"kind": "file", "text": "", "attachment": attachment})
            else:
                segs.append(_file_error_segment(error or "文件发送失败"))
            continue
        if VOICE_MARK_RE.match(block) and voice_mode_on:
            tts, zh, params = _parse_voice_block(block)
            if not tts:
                continue
            pieces = _split_voice_bubbles(tts, params)
            for i, piece in enumerate(pieces):
                entry: dict = {"kind": "voice", "text": piece}
                if zh and i == 0:          # 翻译只挂第一条
                    entry["zh"] = zh
                if params:                 # 管道参数只给 _synth_voice_final 用，不落 meta
                    entry["voice_params"] = params
                segs.append(entry)
        else:
            for s in _split_text_bubbles(block):
                segs.append({"kind": "text", "text": s})
    if not segs:
        return
    full = []
    for i, sd in enumerate(segs):
        entry = {"seq": i, "kind": sd["kind"], "text": sd["text"]}
        if sd.get("id"):
            entry["id"] = sd["id"]
        if sd.get("zh"):
            entry["zh"] = sd["zh"]
        if sd.get("attachment"):
            entry["attachment"] = sd["attachment"]
        if sd["kind"] == "voice":
            # synth_ms 弃用：非流式路径不做 voice_latency 打点（77b822d 起返回 4 元组，原 3 元
            # 解包在这条路径一触发就 ValueError——非流式 + segment_mode 才会走到，属潜伏 bug）
            ok, dur, ext, _synth_ms = await _synth_voice_final(msg["id"], i, sd["text"], sd.get("voice_params"))
            entry["url"] = f"{VOICE_PUBLIC_PREFIX}/{msg['id']}_{i}{ext}" if ok else None
            entry["duration"] = dur if ok else 0.0
        full.append(entry)
    meta = dict(msg.get("meta") or {})
    meta["segments"] = full
    if attachments:
        meta["attachments"] = attachments
    with db() as conn:
        conn.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), msg["id"]))
        conn.commit()
    msg["meta"] = meta


# ══════════════ 贴纸接口（聊天面板 / 管理页）══════════════
@app.get("/api/stickers")
async def api_stickers(request: Request):
    """聊天面板：只下发 id/url/owner/enabled（不下 desc/tags）。带 version 供前端按版本重拉；
    aliases {旧id:新id} 让历史消息里的旧 id 改名后仍能渲染。"""
    check_auth(request)
    data = _load_stickers()
    version = int(data.get("version") or 1)
    resp = {"version": version, "stickers": _sticker_public(data.get("stickers") or [])}
    aliases = data.get("aliases")
    if isinstance(aliases, dict) and aliases:
        resp["aliases"] = {str(k): str(v) for k, v in aliases.items()}
    return JSONResponse(resp, headers={"ETag": f'"{version}"', "Cache-Control": "no-cache"})


@app.get("/api/stickers/admin")
async def api_stickers_admin(request: Request):
    """管理页：完整字段（含 desc/tags/enabled），供上传/编辑/待补充判定。desc 空 = 待补充。"""
    check_auth(request)
    data = _load_stickers()
    items = []
    for s in data.get("stickers") or []:
        if not isinstance(s, dict):
            continue
        sid = s.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        items.append({
            "id": sid,
            "url": _sticker_media_url(sid),
            "desc": s.get("desc") or "",
            "owner": s.get("owner") or "both",
            "tags": s.get("tags") or [],
            "enabled": bool(s.get("enabled", True)),
        })
    return {"version": int(data.get("version") or 1), "stickers": items}


@app.post("/api/stickers")
async def api_stickers_upload(request: Request, id: str = "", desc: str = "", owner: str = "both",
                              tags: str = "", filename: str = ""):
    """上传新贴纸：raw body 是图片字节，id/desc/owner/tags/filename 走查询参数
    （与现有 raw-body 上传模式一致，不引 python-multipart）。魔数验图、≤2MB、
    Pillow 转 WebP 重新编码；desc 必填（纯人工，不做任何自动生成）；id 空则从 filename 派生。"""
    check_auth(request)
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty image")
    if len(body) > STICKER_MAX_BYTES:
        raise HTTPException(413, f"image too large (> {STICKER_MAX_BYTES // (1024 * 1024)}MB)")
    if _detect_image_mime(body) is None:
        raise HTTPException(415, "not a valid image (png/jpeg/gif/webp)")
    desc = (desc or "").strip()
    if not desc:
        raise HTTPException(422, "desc is required")
    owner = (owner or "both").strip()
    if owner not in ("Vale", "Lin", "both"):
        raise HTTPException(422, "owner must be Vale/Lin/both")
    sid = (id or "").strip()
    if sid and not STICKER_ID_RE.match(sid):
        raise HTTPException(422, "id must match ^[a-z0-9_]{1,32}$")
    data = _load_stickers()
    existing = {s.get("id") for s in data.get("stickers") or [] if isinstance(s, dict)}
    if not sid:
        sid = _unique_sticker_id(_derive_sticker_id(filename), existing)
    elif sid in existing:
        raise HTTPException(409, f"sticker id '{sid}' already exists")
    try:
        webp = _to_webp(body)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"image conversion failed: {exc}")
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    (STICKER_DIR / f"{sid}.webp").write_bytes(webp)
    tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    data.setdefault("stickers", []).append({
        "id": sid, "desc": desc, "owner": owner,
        "tags": tags_list, "enabled": True,
    })
    _save_stickers(data)
    return {"ok": True, "id": sid, "url": _sticker_media_url(sid), "version": int(data.get("version") or 1)}


@app.patch("/api/stickers/{sid}")
async def api_stickers_patch(sid: str, request: Request):
    """改 desc / owner / tags / enabled。desc 空拒绝；给待补充贴纸补上描述 → 正常启用。"""
    check_auth(request)
    if not STICKER_ID_RE.match(sid):
        raise HTTPException(404, "no such sticker")
    body = await request.json()
    data = _load_stickers()
    items = data.get("stickers") or []
    idx = next((i for i, s in enumerate(items) if isinstance(s, dict) and s.get("id") == sid), None)
    if idx is None:
        raise HTTPException(404, "no such sticker")
    entry = dict(items[idx])
    was_pending = not (entry.get("desc") or "").strip()
    if "desc" in body:
        nd = str(body["desc"] or "").strip()
        if not nd:
            raise HTTPException(422, "desc cannot be empty")
        entry["desc"] = nd
        if was_pending and "enabled" not in body:
            entry["enabled"] = True       # 待补充补上描述 → 正常启用
    if "owner" in body:
        ov = str(body["owner"] or "").strip()
        if ov not in ("Vale", "Lin", "both"):
            raise HTTPException(422, "owner must be Vale/Lin/both")
        entry["owner"] = ov
    if "tags" in body:
        tg = body["tags"]
        entry["tags"] = [str(t).strip() for t in (tg if isinstance(tg, list) else str(tg).split(",")) if str(t).strip()]
    if "enabled" in body:
        entry["enabled"] = bool(body["enabled"])
    items[idx] = entry
    _save_stickers(data)
    return {"ok": True, "id": sid, "version": int(data.get("version") or 1)}


@app.delete("/api/stickers/{sid}")
async def api_stickers_delete(sid: str, request: Request):
    """删除文件 + 配置项。历史消息里引用该 id 的贴纸会变不渲染（前端已提示）。"""
    check_auth(request)
    if not STICKER_ID_RE.match(sid):
        raise HTTPException(404, "no such sticker")
    data = _load_stickers()
    before = len(data.get("stickers") or [])
    data["stickers"] = [s for s in data.get("stickers") or []
                        if not (isinstance(s, dict) and s.get("id") == sid)]
    if len(data["stickers"]) == before:
        raise HTTPException(404, "no such sticker")
    _save_stickers(data)
    try:
        (STICKER_DIR / f"{sid}.webp").unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True, "id": sid, "version": int(data.get("version") or 1)}


@app.post("/api/stickers/import")
async def api_stickers_import(request: Request):
    """单条 URL 导入：{url, id?, desc?, tags?, owner?}。
    服务端下载 → 魔数验图 → Pillow 转 WebP → 入库。desc 空 → 待补充入库
    （不自动生成；列表半透明、不进 prompt，补描述后启用）。SSRF 防护 + 10s 超时 + ≤5MB。"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected JSON body")
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(422, "url is required")
    owner = str(body.get("owner") or "both").strip()
    if owner not in ("Vale", "Lin", "both"):
        raise HTTPException(422, "owner must be Vale/Lin/both")
    tags = str(body.get("tags") or "").strip()
    sid = str(body.get("id") or "").strip()
    if sid and not STICKER_ID_RE.match(sid):
        raise HTTPException(422, "id must match ^[a-z0-9_]{1,32}$")
    desc = str(body.get("desc") or "").strip()
    raw = await asyncio.to_thread(_download_sticker_url, url)
    if _detect_image_mime(raw) is None:
        raise HTTPException(415, "not a valid image (png/jpeg/gif/webp)")
    try:
        webp = _to_webp(raw)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"image conversion failed: {exc}")
    st = _load_stickers()
    existing = {s.get("id") for s in st.get("stickers") or [] if isinstance(s, dict)}
    if not sid:
        sid = _derive_sticker_id_from_url(url, desc)
    elif sid in existing:
        raise HTTPException(409, f"sticker id '{sid}' already exists")
    sid = _save_sticker_image(webp, sid, desc, owner, tags, existing, st)
    _save_stickers(st)
    return {"ok": True, "id": sid, "url": _sticker_media_url(sid), "version": int(st.get("version") or 1)}


@app.post("/api/stickers/import/batch")
async def api_stickers_import_batch(request: Request):
    """批量 URL 导入：{owner, items:[{url, desc?}]}。
    下载阶段并发（上限 STICKER_URL_CONCURRENCY），落库阶段串行（配置文件共享）。
    逐条回结果 {ok, id?, error?}，单条失败不中断整批。desc 空 → 待补充入库（不自动生成）。"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected JSON body")
    owner = str(body.get("owner") or "both").strip()
    if owner not in ("Vale", "Lin", "both"):
        raise HTTPException(422, "owner must be Vale/Lin/both")
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(422, "items is required")
    if len(raw_items) > 200:
        raise HTTPException(422, "too many items (max 200)")

    async def _fetch_one(item):
        url = str((item or {}).get("url") or "").strip()
        if not url:
            return {"ok": False, "url": url, "error": "url 为空"}
        try:
            raw = await asyncio.to_thread(_download_sticker_url, url)
        except HTTPException as exc:
            return {"ok": False, "url": url,
                    "error": str(exc.detail) if hasattr(exc, "detail") else str(exc)}
        except Exception as exc:
            return {"ok": False, "url": url, "error": type(exc).__name__}
        if _detect_image_mime(raw) is None:
            return {"ok": False, "url": url, "error": "不是有效图片 (png/jpeg/gif/webp)"}
        try:
            webp = _to_webp(raw)
        except Exception as exc:
            return {"ok": False, "url": url, "error": f"图片转码失败: {type(exc).__name__}"}
        return {"ok": True, "url": url, "webp": webp}

    sem = asyncio.Semaphore(STICKER_URL_CONCURRENCY)

    async def _limited(item):
        async with sem:
            return await _fetch_one(item)

    fetched = await asyncio.gather(*[_limited(it) for it in raw_items])

    st = _load_stickers()
    existing = {s.get("id") for s in st.get("stickers") or [] if isinstance(s, dict)}
    results = []
    for item, got in zip(raw_items, fetched):
        url = str((item or {}).get("url") or "").strip()
        if not got.get("ok"):
            results.append({"ok": False, "url": url, "error": got.get("error") or "失败"})
            continue
        desc = str((item or {}).get("desc") or "").strip()
        tags = ""
        base = _derive_sticker_id_from_url(url, desc)
        sid = _save_sticker_image(got["webp"], base, desc, owner, tags, existing, st)
        results.append({"ok": True, "id": sid, "url": url})
    _save_stickers(st)
    return {"results": results, "version": int(st.get("version") or 1)}


@app.post("/api/stickers/{sid}/rename")
async def api_stickers_rename(sid: str, request: Request):
    """改名：同步重命名文件、更新配置，并记录 alias（旧 id → 新 id）。
    历史消息里存的旧 id 经 alias 仍能解析渲染，不碰历史数据（方案 a）。"""
    check_auth(request)
    if not STICKER_ID_RE.match(sid):
        raise HTTPException(404, "no such sticker")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "expected JSON body")
    new_id = str(body.get("new_id") or "").strip()
    if not STICKER_ID_RE.match(new_id):
        raise HTTPException(422, "id must match ^[a-z0-9_]{1,32}$")
    data = _load_stickers()
    items = data.get("stickers") or []
    idx = next((i for i, s in enumerate(items) if isinstance(s, dict) and s.get("id") == sid), None)
    if idx is None:
        raise HTTPException(404, "no such sticker")
    if new_id == sid:
        return {"ok": True, "id": sid, "version": int(data.get("version") or 1)}
    existing_ids = {s.get("id") for s in items if isinstance(s, dict)}
    aliases = {str(k): str(v) for k, v in (data.get("aliases") or {}).items()}
    if new_id in existing_ids or new_id in aliases or new_id in aliases.values():
        raise HTTPException(409, f"id '{new_id}' already in use")
    old_path = STICKER_DIR / f"{sid}.webp"
    if old_path.is_file():
        new_path = STICKER_DIR / f"{new_id}.webp"
        if new_path.is_file():
            raise HTTPException(409, f"file '{new_id}.webp' already exists")
        old_path.rename(new_path)
    entry = dict(items[idx])
    entry["id"] = new_id
    items[idx] = entry
    aliases = {a: (new_id if c == sid else c) for a, c in aliases.items()}
    aliases[sid] = new_id
    data["aliases"] = aliases
    _save_stickers(data)
    return {"ok": True, "id": new_id, "version": int(data.get("version") or 1)}


@app.get("/media/stickers/{filename}")
async def media_stickers(filename: str):
    """图片静态服务兜底（生产走 nginx immutable；这条让本地跑通）。文件名即 id，无额外映射。"""
    if not filename.endswith(".webp") or not STICKER_ID_RE.match(filename[:-5]):
        raise HTTPException(404, "no such sticker")
    path = STICKER_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "no such sticker")
    return FileResponse(path, media_type="image/webp")


# ══════════════ 头像与名字 · 接口 ══════════════
# GET /config 返回四字段；POST /config 支持单字段（或一次多个）更新，名字不允许空串；
# POST /upload_avatar 走 raw-body（跟贴纸同一套，不引 python-multipart），type 是查询参数；
# GET /static/avatars/{filename} 是图片兜底（生产走 nginx alias，这条让本地跑通）。


@app.get("/config")
async def get_config(request: Request):
    check_auth(request)
    return _load_avatar_config()


@app.api_route("/config", methods=["POST", "PATCH"])
async def set_config(request: Request):
    """单字段更新（头像、名字或聊天背景，可一次多个）。"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json body")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    cfg = _load_avatar_config()
    changed = False
    for k in AVATAR_KEYS:
        if k not in body:
            continue
        val = body[k]
        if k == "chat_background_opacity":
            try:
                val = max(0, min(100, int(val)))
            except (TypeError, ValueError):
                raise HTTPException(422, "chat_background_opacity must be an integer from 0 to 100")
        elif k.endswith("_name"):
            val = (val or "").strip()
            if not val:
                raise HTTPException(422, f"{k} cannot be empty")
        elif not isinstance(val, str):
            val = "" if val is None else str(val)
        cfg[k] = val
        changed = True
    if changed:
        _save_avatar_config(cfg)
    return {"ok": True, "config": cfg}


@app.post("/upload_avatar")
async def upload_avatar(request: Request, type: str = "assistant"):
    """上传头像或聊天背景：raw body 是图片字节，type 走查询参数。
    头像保持原有 WebP 行为；background 另存为长边≤1600 的 JPEG。
    """
    check_auth(request)
    if type not in ("assistant", "user", "background"):
        raise HTTPException(422, "type must be assistant/user/background")
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty file")
    if len(body) > AVATAR_MAX_BYTES:
        raise HTTPException(413, f"image too large (> {AVATAR_MAX_BYTES // (1024 * 1024)}MB)")
    if _detect_image_mime(body) is None:
        raise HTTPException(415, "not a valid image (png/jpeg/gif/webp)")
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if type == "background":
            encoded, width, height = _to_chat_background_jpeg(body)
            name = f"background_{int(time.time() * 1000)}.jpg"
            media_type = "image/jpeg"
        else:
            encoded = _to_webp(body)
            width = height = None
            name = f"{type}_{int(time.time() * 1000)}.webp"
            media_type = "image/webp"
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"image conversion failed: {exc}")
    (AVATAR_DIR / name).write_bytes(encoded)
    key = "chat_background" if type == "background" else ("assistant_avatar" if type == "assistant" else "user_avatar")
    cfg = _load_avatar_config()
    cfg[key] = _avatar_media_url(name)
    _save_avatar_config(cfg)
    result = {"ok": True, "url": cfg[key]}
    if type == "background":
        result.update({"filename": name, "bytes": len(encoded), "width": width, "height": height, "media_type": media_type, "config": cfg})
    return result


@app.get("/static/avatars/{filename}")
async def static_avatars(filename: str):
    """头像图片静态服务兜底（生产走 nginx alias /static/avatars/ → ./data/valelin/avatars/，
    这条让本地跑通）。文件名 {type}_{ts}.webp，防穿越只取 basename。"""
    safe = Path(filename).name
    if safe != filename or Path(safe).suffix.lower() not in (".webp", ".jpg"):
        raise HTTPException(404, "no such avatar")
    path = AVATAR_DIR / safe
    if not path.is_file():
        raise HTTPException(404, "no such avatar")
    media_type = "image/jpeg" if path.suffix.lower() == ".jpg" else "image/webp"
    return FileResponse(path, media_type=media_type)


@app.get("/app/favorites")
async def app_favorites(request: Request):
    """只读代理 → OB /api/favorites（留存列表），回来按 turn_id 补齐语音快照。

    留存数据层在 OB gateway（favorites 表，按 turn_id 存快照），relay 透传：
    OB 侧 Bearer 用 relay.env 的 GATEWAY_KEY，key 只留服务端，前端带 companion
    secret 访问本路由。透传 saved_by/limit 过滤。语音是 relay 旁挂数据
    （favorites_voice + kept/ 文件），OB 无感知；补齐失败退回纯透传，列表不砸。
    """
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    params = {
        k: v
        for k, v in request.query_params.items()
        if k in ("saved_by", "limit")
    }
    return await _favorites_proxy(
        "GET", OB_BASE_URL + "/api/favorites", params=params or None,
        transform=_attach_voice_snapshots,
    )


@app.post("/app/favorites")
async def app_favorites_add(request: Request):
    """写代理 → OB /api/favorites（钉一条留存），转发后旁挂语音快照。

    前端 body {turn_id, saved_by, msg_id}：msg_id 是 relay messages.id（语音所在的
    回复行），只在 relay 侧用——转发给 OB 的 body 剥掉它保持 {turn_id, saved_by} 原样，
    OB 无感知。gateway 侧幂等（同轮同人重复收不报错）、turn 不存在返回 404
    （reason=turn not found）。上游状态码原样带回，让前端能区分「收藏成功」和
    「这一轮不在聊天记录里」。OB 收成功才拷语音快照（幂等；拷失败不挡收藏，
    降级纯文字）；锚点体系见 /app/send 的注释：收藏 turn_id 系、引用 msg_id 系，
    favorites_voice 主键是 turn_id，msg_id 只是语音定位符。
    """
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    try:
        body = await request.json()
    except Exception:
        body = {}
    ob_body = body if isinstance(body, dict) else {}
    msg_id = ob_body.pop("msg_id", None)          # relay 专用锚点，绝不透传给 OB
    resp = await _favorites_proxy(
        "POST", OB_BASE_URL + "/api/favorites", json=ob_body
    )
    if 200 <= resp.status_code < 300 and msg_id is not None and isinstance(ob_body.get("turn_id"), int):
        # 拷文件放线程：本地 IO 几百 KB，别占事件循环；await 完成再返回，保证 POST 成功 = 快照已就位
        await asyncio.to_thread(
            _snapshot_favorite_voice, int(ob_body["turn_id"]), int(msg_id),
            str(ob_body.get("saved_by") or ""),
        )
    return resp


@app.delete("/app/favorites/{favorite_id}")
async def app_favorites_delete(request: Request, favorite_id: int):
    """写代理 → OB /api/favorites/{id}（取消收藏），透传成功后清 relay 侧旁挂。

    前端 DELETE 时顺带 ?turn_id=（收藏列表项里两个 id 都有）——OB 行删了，
    favorites_voice 行 + kept/ 文件也按 turn_id 清掉，不留孤儿快照。
    OB 返回 404（已被别处取消）也算取消完成，relay 侧照样清。
    """
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    turn_id = request.query_params.get("turn_id")
    resp = await _favorites_proxy(
        "DELETE", OB_BASE_URL + "/api/favorites/" + str(favorite_id)
    )
    if turn_id and (200 <= resp.status_code < 300 or resp.status_code == 404):
        try:
            await asyncio.to_thread(_drop_favorite_voice, int(turn_id))
        except (TypeError, ValueError):
            pass
    return resp


# ---- WORDS / ALBUM shared keeps -------------------------------------------

@app.get("/app/keeps/tags")
async def app_keeps_tags(request: Request):
    check_auth(request)
    with db() as conn:
        return {"tags": keeps_store.list_tags(conn)}


@app.get("/app/keeps")
async def app_keeps_list(
    request: Request, view: str = "all", saved_by: str = "all",
    tag: str = "", since: str = "", until: str = "", limit: int = 500,
):
    check_auth(request)
    if view not in {"all", "words", "album"}:
        raise HTTPException(status_code=400, detail="view must be all, words, or album")
    if saved_by not in {"all", "her", "Vale"}:
        raise HTTPException(status_code=400, detail="saved_by must be all, her, or Vale")
    with db() as conn:
        items = keeps_store.list_items(
            conn, view=view, source=saved_by, tag=tag, after=since, until=until, limit=limit,
        )
        return {"items": items, "tags": keeps_store.list_tags(conn)}


@app.post("/app/keeps")
async def app_keeps_save(request: Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    try:
        message_id = int(body.get("message_id"))
        seg = body.get("seg")
        if seg is not None and (isinstance(seg, bool) or not isinstance(seg, int)):
            raise ValueError("seg must be a non-negative integer")
        image_id = str(body.get("image_id") or "").strip() or None
        tags = body.get("tags") if isinstance(body.get("tags"), list) else []
        with db() as conn:
            source_key = f"chat:her:{message_id}:{image_id or '*'}" + (f":seg:{seg}" if seg is not None else "")
            existed = conn.execute("SELECT 1 FROM keep_items WHERE source_key=?", (source_key,)).fetchone() is not None
            keep_id = keeps_store.save_message(
                conn, message_id=message_id, saved_by="her", tags=tags,
                upload_dir=UPLOAD_DIR, voice_dir=VOICE_DIR, sticker_dir=STICKER_DIR,
                keep_media_dir=KEEP_MEDIA_DIR, media_public_path=KEEP_MEDIA_PUBLIC_PATH,
                image_id=image_id, seg=seg,
                context=body.get("context", "auto"), context_message_id=body.get("context_message_id"),
                context_match_text=body.get("context_match_text", ""),
            )
            result = keeps_store.item_payload(conn, keep_id)
            activity = keeps_activity.record(conn, action="save", actor="her", item=result) if not existed else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if activity:
        await broadcast(keep_activity_subs, app_payload(activity))
    return result


@app.get("/app/keeps/media/{filename}")
async def app_keeps_media(filename: str, request: Request):
    check_auth(request)
    safe = Path(filename).name
    if safe != filename or not safe.startswith("keep-"):
        raise HTTPException(status_code=404, detail="no such media")
    try:
        path = (KEEP_MEDIA_DIR / safe).resolve(strict=True)
        root = KEEP_MEDIA_DIR.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="no such media")
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="no such media")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")


@app.get("/app/keeps/{keep_id}")
async def app_keeps_get(keep_id: int, request: Request):
    check_auth(request)
    with db() as conn:
        result = keeps_store.item_payload(conn, keep_id)
    if not result:
        raise HTTPException(status_code=404, detail="keep not found")
    return result


@app.post("/app/keeps/{keep_id}/share")
async def app_keeps_share(keep_id: int, request: Request):
    """Send a saved-item card as a normal user turn and preserve its visual snapshot."""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    api_session = str(body.get("api_session") or body.get("session_id") or "").strip()[:120]
    with db() as conn:
        item = keeps_store.item_payload(conn, keep_id)
    if not item:
        raise HTTPException(status_code=404, detail="keep not found")
    images = [m for m in item.get("media", {}).get("items", []) if isinstance(m, dict) and m.get("kind") == "image"]
    text = keeps_store.preview_text(item)
    if not text:
        text = "图片收藏" if images else "收藏卡片"
    share_data = {
        "id": item["id"], "saved_by": item["saved_by"], "speaker": item["speaker"],
        "message_at": item["message_at"], "text": text,
        "display_text": keeps_store.display_preview_text(item),
        "quote": keeps_store.plain_text_preview(item.get("quote")),
        "tags": item["tags"], "images": images,
    }
    meta = {"user": "human", "keep_card": share_data}
    if api_session:
        meta["api_session"] = api_session
    msg = save_message("in", "user", keeps_store.share_context_text(item, text), meta)
    msg = await _dispatch_and_echo_incoming(msg)
    return {"id": msg["id"], "keep_id": keep_id}


@app.delete("/app/keeps/{keep_id}")
async def app_keeps_delete(keep_id: int, request: Request):
    check_auth(request)
    try:
        with db() as conn:
            deleted = keeps_store.delete_item(conn, keep_id, "her")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="keep not found")
    return {"ok": True}


@app.delete("/app/keeps/{keep_id}/context")
async def app_keeps_clear_context(keep_id: int, request: Request):
    check_auth(request)
    try:
        with db() as conn:
            keeps_store.clear_item_context(conn, keep_id)
            return keeps_store.item_payload(conn, keep_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="keep not found")


@app.post("/app/keeps/{keep_id}/tags")
async def app_keeps_set_tags(keep_id: int, request: Request):
    check_auth(request)
    body = await request.json()
    tags = body.get("tags") if isinstance(body, dict) and isinstance(body.get("tags"), list) else []
    try:
        with db() as conn:
            result = keeps_store.set_item_tags(conn, keep_id, tags)
    except LookupError:
        raise HTTPException(status_code=404, detail="keep not found")
    return {"tags": result}


@app.post("/app/keeps/{keep_id}/notes")
async def app_keeps_add_note(keep_id: int, request: Request):
    check_auth(request)
    body = await request.json()
    try:
        with db() as conn:
            note_id = keeps_store.add_note(conn, keep_id, "her", body.get("content") if isinstance(body, dict) else "")
            item = keeps_store.item_payload(conn, keep_id)
            activity = keeps_activity.record(conn, action="note", actor="her", item=item,
                                              note=body.get("content") if isinstance(body, dict) else "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await broadcast(keep_activity_subs, app_payload(activity))
    return {"id": note_id, "item": item}


@app.delete("/app/keeps/{keep_id}/notes/{note_id}")
async def app_keeps_delete_note(keep_id: int, note_id: int, request: Request):
    check_auth(request)
    try:
        with db() as conn:
            belongs = conn.execute("SELECT 1 FROM keep_notes WHERE id=? AND keep_id=?", (note_id, keep_id)).fetchone()
            if not belongs:
                raise HTTPException(status_code=404, detail="note not found")
            deleted = keeps_store.delete_note(conn, note_id, "her")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="note not found")
    return {"ok": True}


keeps_mcp = FastMCP("Valelin WORDS / ALBUM", streamable_http_path="/mcp")
_claimed_keep_tool_uses: dict[str, float] = {}
_keep_tool_anchor_lock = threading.Lock()


def _keep_text_segments_before_tool(prefix: str) -> int | None:
    """Count text bubbles using the same splitters as the reply stream."""
    if not prefix.strip():
        return 0
    if len(prefix) > 1600 or "[voice" in prefix.lower() or "[file:" in prefix.lower():
        return None  # A partial special block cannot be counted as a text bubble.
    remaining, blocks = _split_blocks(prefix, final=True)
    if remaining.strip():
        blocks.append(remaining)
    if any(_block_struct_open(block) or STICKER_RE.fullmatch(block.strip()) for block in blocks):
        return None
    return sum(len(_split_text_bubbles(block)) for block in blocks)


def _keep_tool_anchor(tool_name: str, actual: dict) -> tuple[int | None, int | None]:
    """Match one live SDK tool event by name and arguments; never infer a position."""
    since = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
    for _ in range(9):
        with _keep_tool_anchor_lock:
            cutoff = time.monotonic() - 120
            for old_id in [key for key, used_at in _claimed_keep_tool_uses.items() if used_at < cutoff]:
                _claimed_keep_tool_uses.pop(old_id, None)
        with db() as conn:
            try:
                candidates = conn.execute(
                    "SELECT run_id, sequence, payload FROM agent_events "
                    "WHERE type='tool_state' AND created_at >= ? "
                    "AND json_extract(payload, '$.status')='running' "
                    "ORDER BY created_at DESC, sequence DESC LIMIT 30", (since,),
                ).fetchall()
            except sqlite3.OperationalError:
                return None, None
            matches = []
            for candidate in candidates:
                state = json.loads(candidate["payload"])
                use_id = str(state.get("toolUseId") or "")
                inputs = state.get("input")
                if not use_id or not isinstance(inputs, dict):
                    continue
                claim_key = f"{candidate['run_id']}:{use_id}"
                with _keep_tool_anchor_lock:
                    if claim_key in _claimed_keep_tool_uses:
                        continue
                if any(key not in actual or actual[key] != value for key, value in inputs.items()):
                    continue
                start = conn.execute(
                    "SELECT payload FROM agent_events WHERE run_id=? AND type='block_start' "
                    "AND json_extract(payload, '$.tool.id')=? LIMIT 1",
                    (candidate["run_id"], use_id),
                ).fetchone()
                if not start:
                    continue
                name = str((json.loads(start["payload"]).get("tool") or {}).get("name") or "")
                if name != tool_name and not name.endswith("__" + tool_name):
                    continue
                finished = conn.execute(
                    "SELECT 1 FROM agent_events WHERE run_id=? AND type='tool_result' "
                    "AND json_extract(payload, '$.toolUseId')=? LIMIT 1",
                    (candidate["run_id"], use_id),
                ).fetchone()
                if finished:
                    continue
                turn_id = state.get("turnId")
                try:
                    turn_id = int(turn_id)
                except (TypeError, ValueError):
                    continue
                inbound = conn.execute(
                    "SELECT 1 FROM messages WHERE id=? AND direction='in' "
                    "AND kind IN ('user','voice') LIMIT 1", (turn_id,),
                ).fetchone()
                if inbound:
                    matches.append((claim_key, turn_id, candidate))
            if len(matches) == 1:
                claim_key, turn_id, candidate = matches[0]
                with _keep_tool_anchor_lock:
                    if claim_key in _claimed_keep_tool_uses:
                        return None, None
                    _claimed_keep_tool_uses[claim_key] = time.monotonic()
                chunks = conn.execute(
                    "SELECT payload FROM agent_events WHERE run_id=? "
                    "AND type IN ('text_delta','block_start') "
                    "AND sequence<? ORDER BY sequence", (candidate["run_id"], candidate["sequence"]),
                ).fetchall()
                text_joiner = ToolTextJoiner()
                prefix = "".join(text_joiner.agent_event(json.loads(chunk["payload"])) for chunk in chunks)
                return turn_id, _keep_text_segments_before_tool(prefix)
            if len(matches) > 1:
                return None, None
        time.sleep(0.05)
    return None, None


def _safe_keep_tool_anchor(tool_name: str, actual: dict) -> tuple[int | None, int | None]:
    try:
        return _keep_tool_anchor(tool_name, actual)
    except Exception as exc:
        print(f"[keeps] optional tool position unavailable: {type(exc).__name__}", flush=True)
        return None, None


@keeps_mcp.tool(name="keep_save_message", description=_KEEPS_MCP_TOOLS[0]["description"], meta={"anthropic/alwaysLoad": True})
def keep_save_message(message_id: int | None = None, image_id: str = "", tags: list[str] | None = None,
                      seg: int | None = None, match_text: str = "", context: Literal["auto", "none", "message"] = "auto",
                      context_message_id: int | None = None, context_match_text: str = "") -> str:
    try:
        turn_id, after_seg = _safe_keep_tool_anchor("keep_save_message", {
            "message_id": message_id, "image_id": image_id, "tags": tags,
            "seg": seg, "match_text": match_text,
            "context": context, "context_message_id": context_message_id, "context_match_text": context_match_text,
        })
        with db() as conn:
            if message_id is None:
                resolved_id, _matched_seg, candidates = keeps_store.match_inbound_text(conn, match_text)
                if resolved_id is None:
                    return json.dumps({"ok": False, "error": "ambiguous match_text" if candidates else "match_text not found",
                                       "candidates": candidates}, ensure_ascii=False)
                message_id = resolved_id
            source_key = f"chat:Vale:{int(message_id)}:{image_id.strip() or '*'}" + (f":seg:{seg}" if seg is not None else "")
            existed = conn.execute("SELECT 1 FROM keep_items WHERE source_key=?", (source_key,)).fetchone() is not None
            keep_id = keeps_store.save_message(
                conn, message_id=int(message_id), saved_by="Vale", tags=tags or [],
                upload_dir=UPLOAD_DIR, voice_dir=VOICE_DIR, sticker_dir=STICKER_DIR,
                keep_media_dir=KEEP_MEDIA_DIR, media_public_path=KEEP_MEDIA_PUBLIC_PATH,
                image_id=image_id.strip() or None, seg=seg, require_inbound=True,
                context=context, context_message_id=context_message_id, context_match_text=context_match_text,
            )
            item = keeps_store.item_payload(conn, keep_id)
            activity = keeps_activity.record(conn, action="save", actor="Vale", item=item,
                                             turn_id=turn_id, after_seg=after_seg) if not existed else None
        if activity:
            _broadcast_keep_from_mcp(activity)
        return json.dumps({"ok": True, "item": item}, ensure_ascii=False)
    except (ValueError, LookupError, PermissionError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_list", description=_KEEPS_MCP_TOOLS[1]["description"], meta={"anthropic/alwaysLoad": True})
def keep_list(saved_by: str = "all", tag: str = "", since: str = "", until: str = "", view: str = "all") -> str:
    try:
        with db() as conn:
            items = keeps_store.list_items(
                conn, view=view if view in {"all", "words", "album"} else "all",
                source=saved_by if saved_by in {"her", "Vale"} else "all",
                tag=tag, after=since, until=until, limit=100,
            )
        return json.dumps({"items": items}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:160]}"}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_add_note", description=_KEEPS_MCP_TOOLS[2]["description"], meta={"anthropic/alwaysLoad": True})
def keep_add_note(keep_id: int, content: str) -> str:
    try:
        turn_id, after_seg = _safe_keep_tool_anchor("keep_add_note", {"keep_id": keep_id, "content": content})
        with db() as conn:
            item_before = keeps_store.item_payload(conn, int(keep_id))
            note_id = keeps_store.add_note(conn, int(keep_id), "Vale", content)
            item = keeps_store.item_payload(conn, int(keep_id))
            replying = any(note["author"] == "her" for note in item_before["notes"])
            activity = keeps_activity.record(conn, action="note", actor="Vale", item=item,
                                              note=content, replying=replying,
                                              turn_id=turn_id, after_seg=after_seg)
        _broadcast_keep_from_mcp(activity)
        return json.dumps({"ok": True, "note_id": note_id}, ensure_ascii=False)
    except (ValueError, LookupError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_list_notes", description=_KEEPS_MCP_TOOLS[3]["description"])
def keep_list_notes(keep_id: int) -> str:
    try:
        with db() as conn:
            item = keeps_store.item_payload(conn, int(keep_id))
        if not item:
            return json.dumps({"ok": False, "error": "keep not found"}, ensure_ascii=False)
        return json.dumps({"notes": item["notes"]}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:160]}"}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_delete", description=_KEEPS_MCP_TOOLS[4]["description"])
def keep_delete(keep_id: int) -> str:
    try:
        with db() as conn:
            deleted = keeps_store.delete_item(conn, int(keep_id), "Vale")
        return json.dumps({"ok": deleted, "error": None if deleted else "keep not found"}, ensure_ascii=False)
    except PermissionError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_clear_context", description=_KEEPS_MCP_TOOLS[7]["description"])
def keep_clear_context(keep_id: int) -> str:
    try:
        with db() as conn:
            keeps_store.clear_item_context(conn, int(keep_id))
            item = keeps_store.item_payload(conn, int(keep_id))
        return json.dumps({"ok": True, "item": item}, ensure_ascii=False)
    except LookupError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_delete_note", description=_KEEPS_MCP_TOOLS[5]["description"])
def keep_delete_note(note_id: int) -> str:
    try:
        with db() as conn:
            deleted = keeps_store.delete_note(conn, int(note_id), "Vale")
        return json.dumps({"ok": deleted, "error": None if deleted else "note not found"}, ensure_ascii=False)
    except PermissionError as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@keeps_mcp.tool(name="keep_send_image", description=_KEEPS_MCP_TOOLS[6]["description"])
def keep_send_image(keep_id: int, image_index: int = 0) -> str:
    with db() as conn:
        item = keeps_store.item_payload(conn, int(keep_id))
    if not item:
        return json.dumps({"ok": False, "error": "keep not found"}, ensure_ascii=False)
    images = [m for m in item.get("media", {}).get("items", []) if isinstance(m, dict) and m.get("kind") == "image"]
    if image_index < 0 or image_index >= len(images):
        return json.dumps({"ok": False, "error": "image index out of range"}, ensure_ascii=False)
    filename = Path(str(images[image_index].get("url") or "")).name
    try:
        source = (KEEP_MEDIA_DIR / filename).resolve(strict=True)
        root = KEEP_MEDIA_DIR.resolve()
    except (OSError, RuntimeError):
        return json.dumps({"ok": False, "error": "image file is unavailable"}, ensure_ascii=False)
    if root not in source.parents or not source.is_file():
        return json.dumps({"ok": False, "error": "image file is unavailable"}, ensure_ascii=False)
    display_name = Path(str(images[image_index].get("name") or source.name)).name.replace("|", "_")
    return f"[file: {source} | {display_name}]"


def _keeps_mcp_token() -> str:
    try:
        with db() as conn:
            row = conn.execute("SELECT token FROM mcp_servers WHERE id=?", (KEEPS_MCP_ID,)).fetchone()
        return str(row["token"] or "") if row else ""
    except sqlite3.Error:
        return ""


keeps_mcp_app = keeps_mcp.streamable_http_app()


def _daily_document_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "data": json.loads(row["data_json"]),
        "updated_at": row["updated_at"],
    }


def _valid_daily_document(doc_key: str, value: Any) -> bool:
    if doc_key in ("valelin_notes", "valelin_anniv"):
        return _valid_model_daily_document(doc_key, value)
    if doc_key == "valelin_period":
        return isinstance(value, dict)
    if doc_key == "valelin_jot":
        return isinstance(value, list)
    return False


@app.get("/app/weather/city")
async def app_weather_city_get(request: Request):
    check_auth(request)
    city = DEFAULT_CITY
    with db() as conn:
        row = conn.execute(
            "SELECT data_json FROM daily_documents WHERE doc_key = ?", (WEATHER_CITY_KEY,)
        ).fetchone()
    if row is not None:
        try:
            city = normalize_weather_city(json.loads(row["data_json"]))
        except (TypeError, ValueError):
            pass
    return {"city": city}


@app.put("/app/weather/city")
async def app_weather_city_put(request: Request):
    check_auth(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or "city" not in payload:
            raise ValueError("city is required")
        city = normalize_weather_city(payload["city"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid weather city")
    with db() as conn:
        conn.execute(
            "INSERT INTO daily_documents (doc_key, data_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_key) DO UPDATE SET data_json = excluded.data_json, "
            "updated_at = excluded.updated_at",
            (WEATHER_CITY_KEY, json.dumps(city, ensure_ascii=False), now_iso()),
        )
    cached_weather_line(city)
    return {"city": city}


@app.get("/app/daily")
async def app_daily_get_all(request: Request):
    """Read the authenticated user's four small daily documents."""
    check_auth(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT doc_key, data_json, updated_at FROM daily_documents"
        ).fetchall()
    documents = {}
    for row in rows:
        if row["doc_key"] not in DAILY_DOCUMENT_KEYS:
            continue
        documents[row["doc_key"]] = _daily_document_record(row)
    return {"documents": documents}


@app.get("/app/home/today")
async def app_home_today(request: Request):
    """Read today's authenticated HOME events from relay-owned data."""
    check_auth(request)
    with db() as conn:
        return home_today_payload(conn)


@app.get("/app/daily/{doc_key}")
async def app_daily_get_one(request: Request, doc_key: str):
    check_auth(request)
    if doc_key not in DAILY_DOCUMENT_KEYS:
        raise HTTPException(status_code=404, detail="daily document not found")
    with db() as conn:
        row = conn.execute(
            "SELECT doc_key, data_json, updated_at FROM daily_documents WHERE doc_key = ?",
            (doc_key,),
        ).fetchone()
    record = _daily_document_record(row)
    return {"key": doc_key, **(record or {"data": None, "updated_at": None})}


@app.put("/app/daily/{doc_key}")
async def app_daily_put(request: Request, doc_key: str):
    check_auth(request)
    if doc_key not in DAILY_DOCUMENT_KEYS:
        raise HTTPException(status_code=404, detail="daily document not found")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict) or "data" not in payload:
        raise HTTPException(status_code=400, detail="data is required")
    if "expected_updated_at" not in payload:
        raise HTTPException(status_code=400, detail="expected_updated_at is required")
    data = payload["data"]
    if not _valid_daily_document(doc_key, data):
        raise HTTPException(status_code=400, detail="invalid daily document shape")
    try:
        data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid daily document data")
    if len(data_json.encode("utf-8")) > 64 * 1024:
        raise HTTPException(status_code=413, detail="daily document too large")
    expected_updated_at = payload.get("expected_updated_at")
    if expected_updated_at is not None and not isinstance(expected_updated_at, str):
        raise HTTPException(status_code=400, detail="invalid expected_updated_at")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT doc_key, data_json, updated_at FROM daily_documents WHERE doc_key = ?",
            (doc_key,),
        ).fetchone()
        current = _daily_document_record(row)
        current_updated_at = current["updated_at"] if current else None
        if expected_updated_at != current_updated_at:
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={"error": "daily document changed", "current": current},
            )
        updated_at = now_iso()
        conn.execute(
            "INSERT INTO daily_documents (doc_key, data_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(doc_key) DO UPDATE SET data_json = excluded.data_json, "
            "updated_at = excluded.updated_at",
            (doc_key, data_json, updated_at),
        )
    return {"key": doc_key, "data": data, "updated_at": updated_at}


def _ledger_http_error(error: LedgerToolError) -> HTTPException:
    message = str(error)
    if message == "ledger entry not found":
        return HTTPException(status_code=404, detail=message)
    if "author is protected" in message or "only user-authored" in message:
        return HTTPException(status_code=403, detail=message)
    if "changed" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


@app.get("/app/ledger")
async def app_ledger_list(request: Request, status: str | None = None):
    """Read the authenticated user's ledger entries."""
    check_auth(request)
    try:
        with db() as conn:
            entries = list_ledger_entries(conn, status)
    except LedgerToolError as error:
        raise _ledger_http_error(error)
    return {"entries": entries}


@app.post("/app/ledger")
async def app_ledger_add(request: Request):
    """Create a user-authored manual ledger entry."""
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="ledger entry must be an object")
    if "evidence" in payload:
        raise HTTPException(status_code=400, detail="evidence can only be attached while settling")
    try:
        with db() as conn:
            entry = add_ledger_entry(
                conn,
                payload,
                author="user",
                source="manual",
                upload_dir=UPLOAD_DIR,
                public_prefix=PUBLIC_PREFIX,
            )
    except LedgerToolError as error:
        raise _ledger_http_error(error)
    return {"entry": entry}


@app.put("/app/ledger/{entry_id}")
async def app_ledger_edit(request: Request, entry_id: str):
    """Edit only a user-authored row, using its last-seen version."""
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="ledger entry must be an object")
    expected_updated_at = payload.get("expected_updated_at")
    if not isinstance(expected_updated_at, str):
        raise HTTPException(status_code=400, detail="expected_updated_at is required")
    fields = {key: payload.get(key) for key in ("direction", "kind", "level", "reason", "action")}
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get_ledger_entry(conn, entry_id)
        if current is None:
            raise HTTPException(status_code=404, detail="ledger entry not found")
        if current["author"] != "user":
            raise HTTPException(status_code=403, detail="only user-authored entries can be edited")
        if current["status"] == "voided":
            raise HTTPException(status_code=409, detail="voided ledger entries cannot be edited")
        if current["updated_at"] != expected_updated_at:
            return JSONResponse(
                status_code=409,
                content={"error": "ledger entry changed", "current": current},
            )
        try:
            entry = update_user_ledger_entry(
                conn, entry_id, fields, expected_updated_at=expected_updated_at
            )
        except LedgerToolError as error:
            raise _ledger_http_error(error)
    return {"entry": entry}


@app.post("/app/ledger/{entry_id}/settle")
async def app_ledger_settle(request: Request, entry_id: str):
    """Settle an open row and optionally attach one uploaded photo."""
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="settlement must be an object")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            entry = settle_ledger_entry(
                conn,
                entry_id,
                payload.get("note", ""),
                evidence=payload.get("evidence"),
                replace_evidence="evidence" in payload,
                upload_dir=UPLOAD_DIR,
                public_prefix=PUBLIC_PREFIX,
            )
        except LedgerToolError as error:
            raise _ledger_http_error(error)
    return {"entry": entry}


@app.post("/app/ledger/{entry_id}/confirm")
async def app_ledger_confirm(request: Request, entry_id: str):
    """Confirm a pending penalty; it becomes open until it is settled."""
    check_auth(request)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            entry = confirm_ledger_entry(conn, entry_id)
        except LedgerToolError as error:
            raise _ledger_http_error(error)
    return {"entry": entry}


@app.post("/app/ledger/{entry_id}/void")
async def app_ledger_void(request: Request, entry_id: str):
    """Void the user's own open row or any row awaiting user confirmation."""
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="void note must be an object")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = get_ledger_entry(conn, entry_id)
        if current is None:
            raise HTTPException(status_code=404, detail="ledger entry not found")
        is_own_open = current["author"] == "user" and current["status"] == "open"
        is_pending = current["status"] == "pending_confirm"
        if not (is_own_open or is_pending):
            raise HTTPException(status_code=403, detail="ledger entry cannot be voided by the user")
        try:
            entry = void_ledger_entry(
                conn,
                entry_id,
                payload.get("note", ""),
                allowed_authors=("model", "user"),
            )
        except LedgerToolError as error:
            raise _ledger_http_error(error)
    return {"entry": entry}


def _command_http_error(error: CommandCardError) -> HTTPException:
    message = str(error)
    if message == "command not found":
        return HTTPException(status_code=404, detail=message)
    if "already ended" in message or "deadline has not passed" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=400, detail=message)


@app.get("/app/command-card")
async def app_command_card(request: Request):
    """Read the current and most recent command card for the authenticated PWA."""
    check_auth(request)
    from backend.demo_visits import database_path
    return command_state_from_db(database_path(DB_PATH))


@app.get("/app/command-card/recent")
async def app_recent_command_cards(request: Request, limit: int = 100):
    """List recent card outcomes so the preview can repair stale cached chat-row meta."""
    check_auth(request)
    from backend.demo_visits import database_path
    return {"commands": recent_command_cards_from_db(database_path(DB_PATH), limit=limit)}


@app.post("/app/command-card/{command_id}/result")
async def app_command_card_result(request: Request, command_id: str):
    """Return a command-card outcome; due time is checked against the server clock."""
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="command result must be an object")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            command = finish_command(conn, command_id, payload.get("outcome"))
        except CommandCardError as error:
            raise _command_http_error(error)
    return {"command": command}


@app.get("/app/notes")
def app_notes(request: Request):
    check_auth(request)
    try:
        return {"notes": notes_store.list_notes()}
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="notes unavailable")


@app.get("/app/notes/{note_id}")
def app_note(request: Request, note_id: str):
    check_auth(request)
    try:
        return notes_store.read_note(note_id)
    except notes_store.NoteNotFound:
        raise HTTPException(status_code=404, detail="note not found")
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail="notes unavailable")


@app.get("/api/letters/due")
@app.get("/app/letters/due")
async def app_letters_due(request: Request):
    check_auth(request)
    with db() as conn:
        return {"letters": due_letters(conn)}


@app.get("/api/letters")
@app.get("/app/letters")
async def app_letters(request: Request, occasion: str | None = None):
    check_auth(request)
    try:
        with db() as conn:
            return {"letters": visible_letters(conn, occasion=occasion)}
    except LetterError as error:
        raise HTTPException(status_code=400, detail=str(error))


@app.post("/api/letters/{letter_id}/{state}")
@app.post("/app/letters/{letter_id}/{state}")
async def app_letter_state(request: Request, letter_id: str, state: str):
    check_auth(request)
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return {"letter": mark_letter(conn, letter_id, state)}
    except LetterError as error:
        raise HTTPException(status_code=404 if str(error) == "letter not found" else 400,
                            detail=str(error))


async def _favorites_proxy(
    method: str, url: str, *, params=None, json=None, transform=None
) -> JSONResponse:
    """favorites 透传共用：带 GATEWAY_KEY 打 OB gateway，状态码+body 原样带回。

    跟 /app/murmur/* 的 200+error 不同：favorites 的 404（turn not found）对前端
    是有效信号，不能吞成通用 error。只有网关连不上/超时才降级 502+error。
    transform：2xx 时对解析后的 JSON 做补齐（GET 塞语音快照用）；transform 挂了
    退回纯透传——旁挂数据是增强，不能把列表本身砸了。
    """
    try:
        async with httpx.AsyncClient(timeout=OB_LUMEN_TIMEOUT) as client:
            resp = await client.request(
                method, url, params=params, json=json,
                headers={"Authorization": "Bearer " + GATEWAY_KEY},
            )
            data = resp.json()
            if transform is not None and 200 <= resp.status_code < 300:
                try:
                    data = transform(data)
                except Exception as exc:
                    print(f"[favorites-voice] 补齐失败（退回纯透传）: "
                          f"{type(exc).__name__}: {str(exc)[:200]}", flush=True)
            return JSONResponse(data, status_code=resp.status_code)
    except Exception as exc:
        return JSONResponse(
            {"error": "favorites unavailable: " + type(exc).__name__}, status_code=502
        )


# ---- 收藏语音快照（旁挂：OB favorites 行不动，relay 自己存语音段 + kept/ 文件）────────
# 数据源只有 relay 有：meta.segments + ./data/valelin/voice/{msg_id}_{seq}.ext。
# 所以在收藏那一刻（POST 拦截）把每个 voice 段音频字节级拷进 kept/（防将来清理逻辑删原件），
# 段清单按原顺序落 favorites_voice；GET 时按 turn_id JOIN 回 items。
# kept/ 由现有 nginx location ^~ /app/voice/ 的 alias 自动覆盖（子路径天然可达），零 nginx 改动。

def _snapshot_favorite_voice(turn_id: int, msg_id: int, saved_by: str) -> None:
    """按 msg_id 读 meta.segments，voice 段音频拷 VOICE_DIR/kept/，段清单落 favorites_voice。

    幂等：INSERT OR REPLACE + kept 文件已存在跳过拷贝。拷失败的段 url 置 null
    （前端降级文字卡）；一条 voice 都没拷成（纯文字回复 / 全失败）不落行——
    收藏照旧纯文字。任何异常只打日志，绝不影响 OB 收藏行本身。
    """
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT meta FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
        try:
            meta = json.loads(row["meta"] or "{}") if row else {}
        except Exception:
            meta = {}
        segs = meta.get("segments") if isinstance(meta.get("segments"), list) else []
        kept_dir = VOICE_DIR / "kept"
        kept_segs: list[dict] = []
        copied = 0
        for sd in segs:
            if not isinstance(sd, dict):
                continue
            out = dict(sd)
            if sd.get("kind") != "voice":
                kept_segs.append(out)          # 文字/贴纸段原样带走（前端按原顺序渲染）
                continue
            base = str(sd.get("url") or "").rsplit("/", 1)[-1]
            src = VOICE_DIR / base if base and "/" not in base and "\\" not in base and base not in (".", "..") else None
            if src is not None and src.is_file():
                kept_dir.mkdir(parents=True, exist_ok=True)
                dst = kept_dir / base
                if not dst.is_file():
                    shutil.copyfile(src, dst)  # 字节级快照，防原件将来被清理
                out["url"] = f"{VOICE_PUBLIC_PREFIX}/kept/{base}"
                copied += 1
            else:
                out["url"] = None              # 原件不在/已失败 → 前端降级文字卡
            kept_segs.append(out)
        if not copied:
            return                             # 纯文字回复（或全失败）→ 不落行，收藏照旧纯文字
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO favorites_voice "
                "(turn_id, msg_id, segments, saved_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (turn_id, msg_id, json.dumps(kept_segs, ensure_ascii=False),
                 saved_by, now_iso()),
            )
            conn.commit()
    except Exception as exc:
        print(f"[favorites-voice] 快照失败 turn={turn_id} msg={msg_id}: "
              f"{type(exc).__name__}: {str(exc)[:200]}", flush=True)


def _attach_voice_snapshots(data):
    """GET 透传结果按 turn_id JOIN favorites_voice，把语音快照塞进 items（就地改）。

    item 补 voice = {msg_id, segments}；没旁挂行的条目不动（老收藏照旧纯文字）。
    """
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return data
    tids: list[int] = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("turn_id"), int):
            tids.append(it["turn_id"])
    if not tids:
        return data
    marks = ",".join("?" for _ in tids)
    with db() as conn:
        rows = conn.execute(
            f"SELECT turn_id, msg_id, segments FROM favorites_voice WHERE turn_id IN ({marks})",
            tids,
        ).fetchall()
    by_turn = {r["turn_id"]: r for r in rows}
    for it in items:
        if not isinstance(it, dict) or it.get("turn_id") not in by_turn:
            continue
        r = by_turn[it["turn_id"]]
        try:
            segs = json.loads(r["segments"])
        except Exception:
            segs = []
        if isinstance(segs, list) and segs:
            it["voice"] = {"msg_id": r["msg_id"], "segments": segs}
    return data


def _drop_favorite_voice(turn_id: int) -> None:
    """取消收藏后清 relay 侧：favorites_voice 行 + kept/ 里该收藏拷的文件。

    文件名以 favorites_voice 行里记录的 kept url 为准（拷了哪些清哪些），
    再兜底按 {msg_id}_*.ext 扫一遍（幂等，防早期行没记全）。
    """
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT msg_id, segments FROM favorites_voice WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if not row:
                return
            conn.execute("DELETE FROM favorites_voice WHERE turn_id = ?", (turn_id,))
            conn.commit()
        names: set[str] = set()
        try:
            segs = json.loads(row["segments"])
            for sd in segs if isinstance(segs, list) else []:
                if isinstance(sd, dict) and sd.get("kind") == "voice" and sd.get("url"):
                    base = str(sd["url"]).rsplit("/", 1)[-1]
                    if base and "/" not in base:
                        names.add(base)
        except Exception:
            pass
        kept_dir = VOICE_DIR / "kept"
        try:
            for p in kept_dir.glob(f"{int(row['msg_id'])}_*"):
                names.add(p.name)
        except (TypeError, ValueError):
            pass
        for name in names:
            try:
                (kept_dir / name).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as exc:
        print(f"[favorites-voice] 清理失败 turn={turn_id}: "
              f"{type(exc).__name__}: {str(exc)[:200]}", flush=True)


# ---------------------------------------------------------------------------
# presence — the PWA POSTs /app/ping every ~60s; read /app/status to decide
# whether the human is around. In-memory only: a relay restart clears last_seen
# (state degrades to 'unknown') until the next ping.
# ---------------------------------------------------------------------------

_last_seen_ts = None


def _presence_state(now):
    if _last_seen_ts is None:
        return "unknown", None
    age = (now - _last_seen_ts).total_seconds()
    if age < PRESENCE_ONLINE_SEC:
        return "online", age
    if age < PRESENCE_RECENT_SEC:
        return "recent", age
    return "away", age


def latest_message():
    """Newest real conversational message (excludes 'thinking' stream)."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE kind != 'thinking' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return rows_to_messages([row])[0]


@app.post("/app/ping")
async def app_ping(request: Request):
    """PWA foreground heartbeat."""
    check_auth(request)
    global _last_seen_ts
    _last_seen_ts = datetime.now(timezone.utc)
    return {"ok": True}


@app.get("/app/status")
async def app_status(request: Request):
    """Presence state + the time/direction of the most recent message. Metadata only, no message text."""
    check_auth(request)
    now = datetime.now(timezone.utc)
    state, seen_age = _presence_state(now)
    last_msg = latest_message()
    last_msg_ts = last_msg["ts"] if last_msg else None
    last_msg_dir = last_msg["direction"] if last_msg else None
    last_msg_age = None
    if last_msg_ts:
        try:
            mt = datetime.fromisoformat(last_msg_ts)
            if mt.tzinfo is None:
                mt = mt.replace(tzinfo=timezone.utc)
            last_msg_age = (now - mt).total_seconds()
        except Exception:
            last_msg_age = None
    return {
        "now": now.isoformat(),
        "last_seen": _last_seen_ts.isoformat() if _last_seen_ts else None,
        "seen_age_sec": seen_age,
        "online": state == "online",
        "state": state,
        "last_msg_ts": last_msg_ts,
        "last_msg_dir": last_msg_dir,
        "last_msg_age_sec": last_msg_age,
    }


@app.get("/app/lumen")
async def app_lumen(request: Request):
    """只读代理 → OB /api/lumen/state。

    前端每次打开 Home 都会调。失败返回 200 + error，别 500 ——
    前端拿到 error 可以显示得体一点，拿到 500 只能显示"读不出来"。
    """
    check_auth(request)
    try:
        async with httpx.AsyncClient(timeout=OB_LUMEN_TIMEOUT) as client:
            resp = await client.get(OB_BASE_URL + "/api/lumen/state")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": "lumen unavailable: " + type(exc).__name__}


@app.get("/app/murmur/blocked")
async def app_murmur_blocked(request: Request):
    """只读代理 → OB /api/murmur/blocked（「心绪」页：想过但没做的念头）。

    跟 /api/lumen/state 不同，OB 侧要 Bearer 鉴权（ARCHIVE_GATEWAY_TOKEN）——
    用 relay.env 里现成的 GATEWAY_KEY（api_loop 打 gateway 同一个 key），
    key 只留在服务端，前端不带。失败返回 200 + error，别 500。
    """
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    try:
        limit = request.query_params.get("limit") or "200"
        params = {"limit": limit}
        # 心绪留三天：前端传 ?days=3，relay 换算成 gateway_tz 的 since 边界——
        # OB gateway 只认 since（ts >= since 字符串比较），不认 days。
        # 按日历天切：since = 今天 00:00（+08:00）往前推 days-1 天，正好 3 张完整卡。
        # ts 全是 +08:00 固定偏移 ISO 串（gateway_tz=Asia/Shanghai，无 DST），
        # 同格式字符串比较可靠。后端 7 天滚动删不动，这里只是过滤展示窗口。
        days_raw = request.query_params.get("days")
        if days_raw:
            try:
                days = int(days_raw)
            except (TypeError, ValueError):
                days = 0
            if days > 0:
                sh = timezone(timedelta(hours=8))
                midnight = datetime.now(sh).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                params["since"] = (
                    midnight - timedelta(days=days - 1)
                ).isoformat(timespec="seconds")
        async with httpx.AsyncClient(timeout=OB_LUMEN_TIMEOUT) as client:
            resp = await client.get(
                OB_BASE_URL + "/api/murmur/blocked",
                params=params,
                headers={"Authorization": "Bearer " + GATEWAY_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": "murmur blocked unavailable: " + type(exc).__name__}


@app.get("/app/murmur/output")
async def app_murmur_output(request: Request):
    """只读代理 → OB /api/murmur/output（「心迹」页：真正去过、做过、说出口的事）。

    跟 /app/murmur/blocked 同一套：OB 侧 Bearer 用 relay.env 的 GATEWAY_KEY，
    key 只留服务端，前端不带。响应是 gateway 包的 {items:[...]}，前端兼容裸数组。
    心迹全保留、无时间过滤，只透传 limit。失败返回 200 + error，别 500。
    """
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    try:
        limit = request.query_params.get("limit") or "100"
        params = {"limit": limit}
        async with httpx.AsyncClient(timeout=OB_LUMEN_TIMEOUT) as client:
            resp = await client.get(
                OB_BASE_URL + "/api/murmur/output",
                params=params,
                headers={"Authorization": "Bearer " + GATEWAY_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": "murmur output unavailable: " + type(exc).__name__}


# ---------------------------------------------------------------------------
# OB gateway 配置代理（站点页「心绪」tab 读/写 mood_llm）
# ---------------------------------------------------------------------------
# OB /api/config 要 Bearer（ARCHIVE_GATEWAY_TOKEN/OB_TOKEN）——跟 /app/murmur/*
# 同一套：用 relay.env 的 GATEWAY_KEY，key 只留服务端，前端用 companion secret
# 访问本路由。GET 返回 gateway 全量 config（前端只取 mood_llm）；POST 前端提交
# { mood_llm: {...} } 原样转发，api_key 非空才覆盖的逻辑在 gateway 那边。
# 上游失败返回 200 + error，别 500。

@app.get("/app/config")
async def app_config(request: Request):
    """只读代理 → OB /api/config。"""
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    try:
        async with httpx.AsyncClient(timeout=OB_LUMEN_TIMEOUT) as client:
            resp = await client.get(
                OB_BASE_URL + "/api/config",
                headers={"Authorization": "Bearer " + GATEWAY_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": "config unavailable: " + type(exc).__name__}


@app.post("/app/config")
async def app_config_post(request: Request):
    """写代理 → OB /api/config。"""
    check_auth(request)
    if not GATEWAY_KEY:
        return {"error": "gateway key not configured"}
    try:
        body = await request.json()
        async with httpx.AsyncClient(timeout=OB_LUMEN_TIMEOUT) as client:
            resp = await client.post(
                OB_BASE_URL + "/api/config",
                json=body,
                headers={"Authorization": "Bearer " + GATEWAY_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": "config unavailable: " + type(exc).__name__}


# ---------------------------------------------------------------------------
# Mind 记忆档 → OB Dashboard 代理
# ---------------------------------------------------------------------------
# OB Dashboard 要 session cookie 才给数据。用一个模块级 AsyncClient 的 cookie
# jar 缓存登录态（进程内存，不落盘）：第一次请求先登一次，之后 cookie 自动带。
# OB 重启会把内存里的 session 全冲掉 → 拿到 401 就重建客户端重登一次再重试，
# 只重试一次。密码没配 / 不对 → archive_* 错误，前端显示空态而不是白屏。
OB_DASH_SESSION = None            # httpx.AsyncClient | None
OB_DASH_LOGGED_IN = False


def _ob_dash_reset() -> None:
    global OB_DASH_SESSION, OB_DASH_LOGGED_IN
    OB_DASH_SESSION = None
    OB_DASH_LOGGED_IN = False


async def _ob_dash_login() -> str:
    """POST /auth/login，把 archive_session cookie 收进模块级客户端。

    返回 'ok' | 'rejected' | 'upstream:<异常类名>'，把「密码不对」和
    「上游连不上」区分开，诊断时别都报成 auth_failed。
    """
    global OB_DASH_SESSION, OB_DASH_LOGGED_IN
    if OB_DASH_SESSION is None:
        OB_DASH_SESSION = httpx.AsyncClient(timeout=OB_DASH_TIMEOUT)
    try:
        r = await OB_DASH_SESSION.post(
            OB_BASE_URL + "/auth/login",
            json={"password": OB_DASHBOARD_PASSWORD},
        )
    except Exception as exc:
        OB_DASH_LOGGED_IN = False
        return "upstream:" + type(exc).__name__
    if r.status_code == 200:
        OB_DASH_LOGGED_IN = True
        return "ok"
    if r.status_code == 401:
        OB_DASH_LOGGED_IN = False
        return "rejected"
    # 其他非 200（5xx / 网关 502 等）也是上游问题，不是密码不对
    OB_DASH_LOGGED_IN = False
    return "upstream:http_" + str(r.status_code)


async def _ob_dash_request(method: str, path: str, params=None):
    """带登录态缓存的 OB Dashboard 请求。

    返回 (ok, data)。失败时 data 是 {"error": ...}，调用方照常包 200 返回，
    前端统一看 error 字段。401 只重登重试一次。
    """
    if not OB_DASHBOARD_PASSWORD:
        return False, {"error": "archive_not_configured"}
    for attempt in range(2):
        if OB_DASH_SESSION is None or not OB_DASH_LOGGED_IN:
            st = await _ob_dash_login()
            if st != "ok":
                if st == "rejected":
                    return False, {"error": "archive_auth_failed"}
                return False, {"error": "archive_upstream: " + st.split(":", 1)[1]}
        try:
            resp = await OB_DASH_SESSION.request(
                method, OB_BASE_URL + path, params=params
            )
        except Exception as exc:
            _ob_dash_reset()  # 上游挂了，登录态也别留
            return False, {"error": "archive_upstream: " + type(exc).__name__}
        if resp.status_code == 401:
            if attempt == 1:
                return False, {"error": "archive_auth_failed"}
            _ob_dash_reset()  # session 失效（OB 重启过）→ 重登再试一次
            continue
        try:
            data = resp.json()
        except Exception:
            return False, {"error": "archive_upstream_not_json"}
        if resp.status_code >= 400:
            # 上游业务错误（如桶不存在）——包 200 透传 error，别当成功数据归一化
            if isinstance(data, dict) and data.get("error"):
                return False, data
            return False, {"error": "archive_upstream_http_" + str(resp.status_code)}
        return True, data
    return False, {"error": "archive_auth_failed"}


async def _ob_dash_write(method: str, path: str, payload):
    """OB Dashboard 写操作（加年轮 / 改名）。

    跟 GET 的 _ob_dash_request 不同：**写操作失败必须让前端知道**，
    用户以为存上了、其实没有是最糟的。返回 (status_code, data)：
      - 密码没配        → 200 + {"error": "archive_not_configured"}（跟 GET 一致）
      - 上游 4xx        → 原样透传状态码和错误体（前端要显示「空的」「太长了」）
      - 上游 5xx/超时/连不上 → 502 + {"error": "archive_unavailable"}
      - 登录被拒        → 200 + {"error": "archive_auth_failed"}（配置问题，不是上游挂了）
    401 重登重试逻辑跟 _ob_dash_request 一样，只重试一次。
    """
    if not OB_DASHBOARD_PASSWORD:
        return 200, {"error": "archive_not_configured"}
    for attempt in range(2):
        if OB_DASH_SESSION is None or not OB_DASH_LOGGED_IN:
            st = await _ob_dash_login()
            if st != "ok":
                if st == "rejected":
                    return 200, {"error": "archive_auth_failed"}
                return 502, {"error": "archive_unavailable"}
        try:
            resp = await OB_DASH_SESSION.request(
                method, OB_BASE_URL + path, json=payload
            )
        except Exception:
            _ob_dash_reset()  # 上游挂了，登录态也别留
            return 502, {"error": "archive_unavailable"}
        if resp.status_code == 401:
            if attempt == 1:
                return 200, {"error": "archive_auth_failed"}
            _ob_dash_reset()  # session 失效（OB 重启过）→ 重登再试一次
            continue
        if resp.status_code >= 500:
            return 502, {"error": "archive_unavailable"}
        try:
            data = resp.json()
        except Exception:
            # 2xx 空体算成功（前端会回拉详情补结构）；4xx 非 JSON 保状态码
            data = {} if resp.status_code < 300 else {"error": "archive_upstream_not_json"}
        return resp.status_code, data
    return 200, {"error": "archive_auth_failed"}


def _ob_meta(bucket: dict) -> dict:
    m = bucket.get("meta") or bucket.get("metadata")
    return m if isinstance(m, dict) else {}


def _ob_pick(bucket: dict, *keys):
    """按顺序在 bucket 和它的 meta 里找第一个有值的字段。"""
    meta = _ob_meta(bucket)
    for k in keys:
        v = bucket.get(k)
        if v not in (None, ""):
            return v
    for k in keys:
        v = meta.get(k)
        if v not in (None, ""):
            return v
    return None


def _ob_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _ob_norm_comments(raw) -> list:
    """年轮：统一成 [{created, kind, body}]，按 created 升序（老的在上）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        created = _ob_pick(c, "created_at", "created", "time") or ""
        kind = _ob_pick(c, "kind", "type") or "年轮"
        body = _ob_pick(c, "body", "content", "text") or ""
        if body or created:
            out.append({"created": str(created), "kind": str(kind), "body": str(body)})
    out.sort(key=lambda c: c["created"])
    return out


def _ob_norm_bucket(bucket):
    """一个桶 → Valelin 前端要的字段。OB 字段名各版本不一样，逐个兜底。"""
    if not isinstance(bucket, dict):
        return None
    b_id = _ob_pick(bucket, "id", "bucket_id")
    name = _ob_pick(bucket, "name", "title")
    if not name:
        name = bucket.get("id") or b_id or ""
    if not b_id:
        b_id = bucket.get("name") or name or ""
    text = _ob_pick(bucket, "content", "text", "body") or ""
    domain = _ob_pick(bucket, "domain", "domains")
    if isinstance(domain, list):
        domain = domain[0] if domain else ""
    if not domain:
        domain = "未分类"
    tags = _ob_pick(bucket, "tags")
    if not isinstance(tags, list):
        tags = [tags] if tags else []
    importance = _ob_pick(bucket, "importance")
    try:
        importance = int(importance)
    except (TypeError, ValueError):
        importance = 5
    created = _ob_pick(bucket, "created_at", "created") or ""
    last_active = _ob_pick(bucket, "last_active_at", "last_active") or created
    comments = _ob_norm_comments(_ob_pick(bucket, "comments", "annotations", "rings"))
    # 列表端点不返回 comments（详情才拉），但 metadata 里有 comment_count ——
    # 前端「有年轮」筛选和「N 圈年轮」都靠这个计数，别把整本年轮塞进列表。
    comment_count = _ob_pick(bucket, "comment_count")
    try:
        comment_count = int(comment_count)
    except (TypeError, ValueError):
        comment_count = 0
    if not comment_count:
        comment_count = len(comments)
    return {
        "id": str(b_id),
        "name": str(name),
        "domain": str(domain),
        "tags": tags,
        "text": str(text),
        "type": str(_ob_pick(bucket, "type") or "dynamic"),
        "importance": importance,
        "pinned": _ob_bool(_ob_pick(bucket, "pinned")),
        # resting = resolved || digested，两个独立判断——
        # resolved 为 False 时不能短路掉 digested。
        "resting": _ob_bool(_ob_pick(bucket, "resolved")) or _ob_bool(_ob_pick(bucket, "digested")),
        "created": created,
        "last_active": last_active,
        "comment_count": comment_count,
        "comments": comments,
    }


def _ob_extract_buckets(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("buckets", "results", "items", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _ob_norm_bucket_list(data) -> list:
    return [b for b in (_ob_norm_bucket(x) for x in _ob_extract_buckets(data)) if b]


# ── Persona State 归一化（Mind「此刻」档）────────────────────────────────
# 字段以 OB /api/persona 实测为准（TASK_mind_persona.md 第二节），不要自己
# 发明字段名。state.affect 八个数值键，state.relationship 四个，personality
# 五个。events 精简成渲染要用的，error 字段一定要留 —— 那是判断评估失败的
# 唯一依据，前端靠它把失败和真实内心话区分开。
PERSONA_AFFECT_KEYS = ("valence", "arousal", "tenderness", "possessiveness",
                       "longing", "security", "protective_drive", "libido")
PERSONA_REL_KEYS = ("affinity", "dominance", "defensiveness", "trust")
PERSONA_PERSONALITY_KEYS = ("openness", "conscientiousness", "extraversion",
                            "agreeableness", "neuroticism")
PERSONA_EVENT_KEYS = ("id", "created_at", "mood_label", "inner_thought",
                      "user_excerpt", "affect_delta", "relationship_delta",
                      "confidence", "error")


def _ob_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _ob_pick_keys(src, keys):
    """只挑 keys 里存在的字段，别的键一概不带。"""
    d = _ob_dict(src)
    return {k: d[k] for k in keys if k in d}


def _ob_norm_persona(data):
    """OB /api/persona → Valelin「此刻」要的结构。下划线命名，不驼峰。

    解析失败 / 不是对象 → 返回 None，调用方按「OB 数据读不出来」处理，
    绝不归一化成一个所有字段都是默认值的对象 —— 前端会渲染假记录。
    """
    if not isinstance(data, dict):
        return None
    state = _ob_dict(data.get("state"))
    affect = _ob_dict(state.get("affect"))
    rel = _ob_dict(state.get("relationship"))
    pers = _ob_dict(state.get("personality"))

    session_id = data.get("active_session_id") or ""
    # updated_at 优先取当前 session 的：events 很稀疏，session 时间才跟聊天走
    updated_at = state.get("updated_at") or ""
    if not updated_at:
        for s in (data.get("sessions") or []):
            if _ob_dict(s).get("session_id") == session_id and _ob_dict(s).get("updated_at"):
                updated_at = _ob_dict(s).get("updated_at")
                break
        if not updated_at:
            for s in (data.get("sessions") or []):
                if _ob_dict(s).get("updated_at"):
                    updated_at = _ob_dict(s).get("updated_at")
                    break

    events = []
    for e in (data.get("events") or []):
        ev = _ob_pick_keys(e, PERSONA_EVENT_KEYS)
        if ev:
            events.append(ev)
    # 新在前：前端拿 events[0] 当「最新一条」（顶部卡片的漂移），
    # OB 返回顺序不保证，这里统一排好。created_at 是 ISO 串，字典序即时间序。
    events.sort(key=lambda e: e.get("created_at") or "", reverse=True)

    return {
        "session_id": session_id,
        "mood_label": affect.get("mood_label") or "",
        "inner_thought": affect.get("inner_thought") or "",
        "residue": affect.get("residue") or "",
        "updated_at": updated_at,
        "affect": _ob_pick_keys(affect, PERSONA_AFFECT_KEYS),
        "relationship": _ob_pick_keys(rel, PERSONA_REL_KEYS),
        "personality": _ob_pick_keys(pers, PERSONA_PERSONALITY_KEYS),
        "events": events,
    }


@app.get("/app/ob/status")
async def app_ob_status(request: Request):
    """OB Dashboard 桶统计。只读代理，失败包 200 + error。"""
    check_auth(request)
    _, data = await _ob_dash_request("GET", "/api/status")
    return data


@app.get("/app/ob/buckets")
async def app_ob_buckets(request: Request):
    check_auth(request)
    ok, data = await _ob_dash_request("GET", "/api/buckets")
    return _ob_norm_bucket_list(data) if ok else data


@app.get("/app/ob/search")
async def app_ob_search(request: Request, q: str = ""):
    check_auth(request)
    if not q.strip():
        # 空查询前端不会打过来（清空走本地 BUCKETS），这里兜底成完整列表
        ok, data = await _ob_dash_request("GET", "/api/buckets")
        return _ob_norm_bucket_list(data) if ok else data
    ok, data = await _ob_dash_request("GET", "/api/search", params={"q": q})
    return _ob_norm_bucket_list(data) if ok else data


@app.get("/app/ob/bucket/{bucket_id}")
async def app_ob_bucket(request: Request, bucket_id: str):
    check_auth(request)
    ok, data = await _ob_dash_request(
        "GET", "/api/bucket/" + urllib.parse.quote(bucket_id, safe="")
    )
    if not ok:
        return data
    b = _ob_norm_bucket(data)
    if b is None:
        return {"error": "archive_bucket_not_found"}
    return b


@app.get("/app/persona")
async def app_persona(request: Request, events_limit: int = 20,
                      sessions_limit: int = 20, session_id: str = ""):
    """Mind「此刻」→ OB /api/persona。只读代理，失败包 200 + error。

    query 透传 events_limit / sessions_limit / session_id（OB 那边
    evaluation_interval_rounds=3，事件很稀疏，别拿它判断接口好坏）。
    登录态管理完全复用 _ob_dash_request —— 401 自动重登重试一次。
    """
    check_auth(request)
    params = {"events_limit": events_limit, "sessions_limit": sessions_limit}
    if session_id:
        params["session_id"] = session_id
    ok, data = await _ob_dash_request("GET", "/api/persona", params=params)
    if not ok:
        return data
    norm = _ob_norm_persona(data)
    if norm is None:
        # OB 200 但结构不是 persona —— 透传明确错误，别归一化成空对象
        return {"error": "archive_persona_unreadable"}
    return norm


@app.post("/app/ob/bucket/{bucket_id}/comments")
async def app_ob_add_comment(request: Request, bucket_id: str):
    """加一条年轮 → OB。写操作，失败要让前端知道，不能静默。

    请求体原样透传（{"content": ..., "kind": ...}），代理层不加字段不改名。
    """
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})
    status, data = await _ob_dash_write(
        "POST",
        "/api/bucket/" + urllib.parse.quote(bucket_id, safe="") + "/comments",
        payload,
    )
    return JSONResponse(status_code=status, content=data)


@app.patch("/app/ob/bucket/{bucket_id}")
async def app_ob_patch_bucket(request: Request, bucket_id: str):
    """改名 / 改正文 / 改日期 → OB。请求体原样透传，三个字段单独传都行。"""
    check_auth(request)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid json"})
    status, data = await _ob_dash_write(
        "PATCH",
        "/api/bucket/" + urllib.parse.quote(bucket_id, safe=""),
        payload,
    )
    return JSONResponse(status_code=status, content=data)


@app.get("/app/history")
async def app_history(request: Request, since: int = 0, before_id: int = 0, limit: int = 200, session_id: str = "", latest: bool = False):
    check_auth(request)
    lim = min(limit, 500)
    if session_id:
        rows = history_for_session(session_id, since, lim, latest, before_id)
    elif before_id:
        with db() as conn:
            older = conn.execute(
                "SELECT * FROM messages WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before_id, lim),
            ).fetchall()
        rows = rows_to_messages(reversed(older))
    else:
        rows = history(since, lim, latest)
    show_keep_activity = request.query_params.get("keep_activity") == "1"
    payloads = [app_payload(m) for m in rows if show_keep_activity or m["kind"] != "keep_activity"]
    tail = max((m["id"] for m in rows), default=0)
    attach_turn_ids(payloads, session_id, tail)
    return {"messages": payloads}


@app.get("/api/runs/{run_id}/events")
async def api_run_events(run_id: str, request: Request):
    """Replay one normalized Agent SDK timeline in its authoritative order."""
    check_auth(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT payload FROM agent_events WHERE run_id = ? ORDER BY sequence ASC",
            (run_id,),
        ).fetchall()
    events = []
    for row in rows:
        try:
            events.append(json.loads(row["payload"]))
        except (TypeError, json.JSONDecodeError):
            # A corrupt historical row must not make read-only replay unavailable.
            print(f"[agent-event] WARNING invalid stored payload run={run_id}", flush=True)
    return {"run_id": run_id, "events": events}


@app.post("/app/visibility")
async def app_visibility(request: Request):
    check_auth(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid visibility state")
    state = app_visibility_state(body.get("client_id"), body.get("visible"), body.get("sequence"))
    updated = 0
    for q in list(app_subs):
        current = app_sub_visibility.get(q)
        if (current and current["client_id"] == state["client_id"]
                and state["sequence"] > current["sequence"]):
            app_sub_visibility[q] = dict(state)
            updated += 1
    return {"ok": True, "updated": updated}


@app.get("/app/stream")
async def app_stream(request: Request):
    """SSE stream, with optional per-page visibility (old clients default visible)."""
    check_auth(request)
    visibility = None
    if "client_id" in request.query_params:
        try:
            sequence = int(request.query_params.get("visibility_seq", "0"))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid visibility sequence")
        visible = request.query_params.get("visible", "1")
        if visible not in ("0", "1"):
            raise HTTPException(status_code=400, detail="invalid visibility state")
        visibility = app_visibility_state(request.query_params["client_id"], visible == "1", sequence)
    return StreamingResponse(
        sse_stream(
            app_subs,
            request,
            shutdown_event=getattr(app.state, "shutdown_requested", None),
            extra_subs=keep_activity_subs if request.query_params.get("keep_activity") == "1" else None,
            visibility=visibility,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# stations — the More → 连接 page's upstream route table. chat / image both
# live in this one table (same shape, different purpose); `active` is a
# single-select within each kind. api_key never leaves as plaintext.
# ---------------------------------------------------------------------------

def mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 8:
        return "sk-***"
    return f"{k[:4]}***...{k[-4:]}"


def _looks_like_api_key(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if s.startswith("sk-"):
        return True
    return len(s) >= 20 and re.fullmatch(r"[A-Za-z0-9_\-]+", s) is not None


def station_dict(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "base_url": row["base_url"],
        "key_masked": mask_key(row["api_key"]),
        "models": row["models"], "default_model": row["default_model"],
        "protocol": row["protocol"], "active_model": row["active_model"],
        "kind": row["kind"], "active": bool(row["active"]),
    }


def stations_list() -> list:
    with db() as conn:
        rows = conn.execute("SELECT * FROM stations ORDER BY created_at ASC").fetchall()
    return [station_dict(r) for r in rows]


def _station_base_fields(body: dict) -> dict:
    """Validate the shared create/update fields. Returns a clean dict or 400s."""
    name = str(body.get("name") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url required")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http:// or https://")
    kind = str(body.get("kind") or "chat").strip() or "chat"
    if kind not in ("chat", "image"):
        raise HTTPException(status_code=400, detail="kind must be 'chat' or 'image'")
    protocol = str(body.get("protocol") or "openai").strip().lower() or "openai"
    if protocol not in ("openai", "anthropic"):
        raise HTTPException(status_code=400, detail="protocol must be 'openai' or 'anthropic'")
    models = [m.strip() for m in str(body.get("models") or "").split(",") if m.strip()]
    active_model = str(body.get("active_model") or "").strip()
    if active_model and models and active_model not in models:
        active_model = ""   # 不在 models 里就当没选，回落 default_model
    return {
        "name": name,
        "base_url": base_url,
        "models": ", ".join(models),
        "default_model": str(body.get("default_model") or "").strip(),
        "kind": kind,
        "protocol": protocol,
        "active_model": active_model,
    }


@app.get("/app/stations")
async def app_stations_list(request: Request):
    check_auth(request)
    return {"stations": stations_list()}


@app.post("/app/stations")
async def app_stations_upsert(request: Request):
    check_auth(request)
    body = await request.json()
    fields = _station_base_fields(body)
    now = now_iso()
    sid = str(body.get("id") or "").strip()
    # api_key 形状校验 —— 兜底拦截被浏览器 autofill 塞进来的假值。
    # 一旦写进去原来的 key 就找不回来了，所以要在覆盖之前拦住。
    new_key = str(body.get("api_key") or "").strip() if "api_key" in body else ""
    if new_key and not _looks_like_api_key(new_key):
        raise HTTPException(status_code=400, detail="api_key 格式不对，像是被自动填充的")
    with db() as conn:
        # 同名挡在保存前：gateway 的 upstream name 必须唯一，重名推送会被 400。
        dup = conn.execute(
            "SELECT id FROM stations WHERE name = ? AND id != ?", (fields["name"], sid or "")
        ).fetchone()
        if dup:
            raise HTTPException(status_code=400, detail=f"已有同名站点「{fields['name']}」，先改个名字")
        if sid:
            row = conn.execute("SELECT id FROM stations WHERE id = ?", (sid,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="station not found")
            set_clause = (
                "name = :name, base_url = :base_url, models = :models, "
                "default_model = :default_model, kind = :kind, "
                "protocol = :protocol, active_model = :active_model, updated_at = :updated_at"
            )
            params = {**fields, "updated_at": now}
            # api_key 留空或不传 = 不改动现有的（前端的约定，见 keyHint）
            if new_key:
                set_clause += ", api_key = :api_key"
                params["api_key"] = new_key
            conn.execute(
                f"UPDATE stations SET {set_clause} WHERE id = :sid",
                {**params, "sid": sid},
            )
            conn.commit()
        else:
            # 服务端生成 id："s" + 8 位随机 hex
            sid = "s" + secrets.token_hex(4)
            conn.execute(
                "INSERT INTO stations (id, name, base_url, api_key, models, default_model, kind, protocol, active_model, active, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
                (sid, fields["name"], fields["base_url"], new_key,
                 fields["models"], fields["default_model"], fields["kind"],
                 fields["protocol"], fields["active_model"], now, now),
            )
            conn.commit()
    return {"stations": stations_list()}


@app.post("/app/stations/activate")
async def app_stations_activate(request: Request):
    check_auth(request)
    body = await request.json()
    sid = str(body.get("id") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="id required")
    with db() as conn:
        row = conn.execute("SELECT kind FROM stations WHERE id = ?", (sid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="station not found")
        kind = row["kind"]
    with db() as conn:
        # 同 kind 内单选：先清空该 kind 的全部 active，再置当前站
        conn.execute("UPDATE stations SET active = 0 WHERE kind = ?", (kind,))
        conn.execute("UPDATE stations SET active = 1, updated_at = ? WHERE id = ?", (now_iso(), sid))
        conn.commit()
    return {"stations": stations_list()}


@app.delete("/app/stations/{station_id}")
async def app_stations_delete(station_id: str, request: Request):
    check_auth(request)
    with db() as conn:
        row = conn.execute("SELECT active FROM stations WHERE id = ?", (station_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="station not found")
        if row["active"]:
            raise HTTPException(status_code=409, detail="这是在用的站，先切到别的再删")
        conn.execute("DELETE FROM stations WHERE id = ?", (station_id,))
        conn.commit()
    return {"stations": stations_list()}


@app.post("/app/stations/test")
async def app_stations_test(request: Request):
    check_auth(request)
    body = await request.json()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not base_url:
        return {"ok": False, "error": "base_url required"}
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        return {"ok": False, "error": "base_url must start with http:// or https://"}
    # api_key 为空但传了 id → 用库里那个 station 的 key
    if not api_key:
        sid = str(body.get("id") or "").strip()
        if sid:
            with db() as conn:
                row = conn.execute("SELECT api_key FROM stations WHERE id = ?", (sid,)).fetchone()
            if row and row["api_key"]:
                api_key = row["api_key"]
    if not api_key:
        return {"ok": False, "error": "api_key required"}
    started = datetime.now(timezone.utc).timestamp()
    try:
        r = await asyncio.to_thread(
            httpx.get,
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8.0,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    latency_ms = int((datetime.now(timezone.utc).timestamp() - started) * 1000)
    if r.status_code >= 400:
        return {"ok": False, "error": f"{r.status_code} {r.reason_phrase}".strip()}
    try:
        data = r.json()
        models_count = len(data.get("data") or []) if isinstance(data, dict) else 0
    except Exception:
        models_count = 0
    return {"ok": True, "latency_ms": latency_ms, "models_count": models_count}


# ---------------------------------------------------------------------------
# MCP 服务器管理 —— More → MCP 页。两张表（mcp_servers / mcp_tools）+ 六个
# /app/mcp 端点。MCP 客户端用官方 SDK（mcp==1.29.0，requirements.txt 已 pin），
# streamable-http 和 sse 共用同一套 initialize → tools/list 握手（SDK 的
# initialize() 自动补发 notifications/initialized）。token / headers 值只进
# 不出：GET 一律掩码，test 不保存。headers 里的 Authorization 覆盖 token。
# ---------------------------------------------------------------------------

MCP_TRANSPORTS = ("streamable-http", "sse")
MCP_CONNECT_TIMEOUT = 10      # 连接超时（任务 §5.4）
MCP_LIST_TIMEOUT = 15         # streamable-http 单次读超时 = tools/list 15s
MCP_SSE_READ_TIMEOUT = 40     # sse 长连接的读超时：冷启动慢的服务器实测 >15s
MCP_CLIENT_INFO = Implementation(name="valelin", version="1.0")


def _mcp_request_headers(token: str, headers: dict) -> dict:
    """发给 MCP 服务器的请求头。headers 里的 Authorization 优先于 token。"""
    out = dict(headers or {})
    if token:
        out.setdefault("Authorization", f"Bearer {token}")
    return out


def _mcp_mask_headers(raw: str) -> dict:
    """headers 存的是 JSON 字符串，取出来逐值掩码（值里可能是 API key）。"""
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): mask_key(str(v)) for k, v in data.items() if str(v)}


def mcp_server_dict(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "url": row["url"], "transport": row["transport"],
        "token": mask_key(row["token"]),
        "headers": _mcp_mask_headers(row["headers"]),
        "enabled": bool(row["enabled"]),
        "tools_count": row["tools_count"],
        "tools_synced_at": row["tools_synced_at"],
        "last_error": row["last_error"],
    }


def mcp_tool_dict(row) -> dict:
    try:
        schema = json.loads(row["input_schema"]) if row["input_schema"] else {}
    except Exception:
        schema = {}
    return {
        "server_id": row["server_id"], "tool_name": row["tool_name"],
        "description": row["description"], "input_schema": schema,
        "enabled": bool(row["enabled"]),
    }


def _mcp_bridge_servers() -> tuple[dict, dict, list[str]]:
    """Enabled database servers and their tool counts; never expose credentials to the UI."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_servers WHERE enabled = 1 AND id != 'ob' ORDER BY created_at ASC"
        ).fetchall()
    servers = {}
    tool_counts = {}
    auto_approve_ids = []
    for row in rows:
        try:
            headers = json.loads(row["headers"]) if row["headers"] else {}
        except Exception:
            headers = {}
        if not isinstance(headers, dict):
            headers = {}
        headers = _mcp_request_headers(str(row["token"] or ""), headers)
        servers[str(row["id"])] = {
            "type": "sse" if row["transport"] == "sse" else "http",
            "url": str(row["url"]),
            "headers": {str(k): str(v) for k, v in headers.items()},
        }
        tool_counts[str(row["id"])] = int(row["tools_count"] or 0)
        if row["auto_approve"]:
            auto_approve_ids.append(str(row["id"]))
    return servers, tool_counts, auto_approve_ids


def mcp_payload() -> dict:
    """GET /app/mcp 返回形状：servers + tools（按 server_id 分组）。"""
    with db() as conn:
        servers = conn.execute("SELECT * FROM mcp_servers WHERE id != ? ORDER BY created_at ASC", (KEEPS_MCP_ID,)).fetchall()
        tools = conn.execute("SELECT * FROM mcp_tools WHERE server_id != ? ORDER BY tool_name ASC", (KEEPS_MCP_ID,)).fetchall()
    tool_map: dict = {}
    for t in tools:
        tool_map.setdefault(t["server_id"], []).append(mcp_tool_dict(t))
    return {
        "servers": [mcp_server_dict(r) for r in servers],
        "tools": tool_map,
    }


def _mcp_base_fields(body: dict) -> dict:
    """校验创建 / 更新共有字段。返回干净 dict 或 400。"""
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    transport = str(body.get("transport") or "streamable-http").strip()
    if transport not in MCP_TRANSPORTS:
        raise HTTPException(status_code=400, detail="transport must be 'streamable-http' or 'sse'")
    headers_raw = body.get("headers")
    headers = {}
    if headers_raw is not None:
        if not isinstance(headers_raw, dict):
            raise HTTPException(status_code=400, detail="headers must be a JSON object")
        headers = {str(k): str(v) for k, v in headers_raw.items()}
    return {
        "name": name, "url": url, "transport": transport,
        "description": str(body.get("description") or "").strip(),
        "headers_json": json.dumps(headers, ensure_ascii=False),
    }


async def mcp_fetch_tools(url: str, transport: str, token: str, headers: dict) -> list:
    """握手 + tools/list，返回 [{tool_name, description, input_schema}, ...]。

    失败抛异常，由调用方用 _classify_mcp_error 归类。注意 sse 的 context
    只产出 (read, write) 两个值，streamable-http 是三个 —— 别改错。
    """
    hdrs = _mcp_request_headers(token, headers)
    if transport == "sse":
        async with sse_client(
            url, headers=hdrs, timeout=MCP_CONNECT_TIMEOUT, sse_read_timeout=MCP_SSE_READ_TIMEOUT,
        ) as (read, write):
            return await _mcp_session_tools(read, write)
    async with streamablehttp_client(
        url, headers=hdrs, timeout=MCP_CONNECT_TIMEOUT, sse_read_timeout=MCP_LIST_TIMEOUT,
    ) as (read, write, _get_session_id):
        return await _mcp_session_tools(read, write)


async def _mcp_session_tools(read, write) -> list:
    async with ClientSession(read, write, client_info=MCP_CLIENT_INFO) as session:
        await session.initialize()
        result = await session.list_tools()
    return [
        {
            "tool_name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema if isinstance(t.inputSchema, dict) else {},
        }
        for t in result.tools
    ]


def _classify_mcp_error(exc) -> str:
    """错误要具体，别把所有失败都归成「连接失败」。"""
    # SDK 内部用 anyio TaskGroup，底层错误会被包进 ExceptionGroup —— 先拆到最里层
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    if isinstance(exc, httpx.ConnectTimeout):
        return f"连接超时（{MCP_CONNECT_TIMEOUT}s）：{exc}"
    if isinstance(exc, httpx.ReadTimeout):
        return f"读取超时（{MCP_LIST_TIMEOUT}s）：{exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"连不上：{exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}：{exc}"
    if isinstance(exc, McpError):
        return f"MCP 协议错误：{exc}"
    msg = str(exc)
    if "protocol version" in msg.lower():
        return f"协议版本不匹配：{exc}"
    if isinstance(exc, (ValueError, TypeError)) or exc.__class__.__name__ == "ValidationError":
        return f"返回格式不对：{exc}"
    return f"{type(exc).__name__}: {exc}"


@app.get("/app/mcp")
async def app_mcp_list(request: Request):
    check_auth(request)
    # The bridge calls this over loopback with a second secret-bearing header.
    # The normal authenticated UI continues to receive only masked credentials.
    if request.headers.get("X-Valelin-Bridge", "") == SECRET:
        host = request.client.host if request.client else ""
        if host in {"127.0.0.1", "::1"}:
            servers, tool_counts, auto_approve_ids = _mcp_bridge_servers()
            return {
                "servers": servers,
                "tool_counts": tool_counts,
                "auto_approve": auto_approve_ids,
            }
    return mcp_payload()


async def _reload_mcp_bridges() -> None:
    """Best-effort hot reload; cold bridges read the same table at spawn."""
    try:
        servers, _, auto_approve_ids = _mcp_bridge_servers()
        results = await _sdk_provider.apply_mcp_servers(servers, auto_approve_ids)
        print(f"[sdk:mcp] hot reload sessions={len(results)}", flush=True)
    except Exception as exc:
        print(f"[sdk:mcp] WARNING hot reload failed: {type(exc).__name__}: {exc}", flush=True)


@app.post("/app/mcp")
async def app_mcp_upsert(request: Request):
    check_auth(request)
    body = await request.json()
    fields = _mcp_base_fields(body)
    now = now_iso()
    sid = str(body.get("id") or "").strip()
    if sid == KEEPS_MCP_ID:
        raise HTTPException(status_code=409, detail="built-in keeps tools are managed by the relay")
    token = str(body.get("token") or "").strip()
    has_headers = isinstance(body.get("headers"), dict) and bool(body["headers"])
    headers_json = fields["headers_json"] if has_headers else "{}"
    enabled = int(bool(body.get("enabled", True)))
    with db() as conn:
        if sid:
            row = conn.execute("SELECT id FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="mcp server not found")
            set_clause = "name = :name, url = :url, transport = :transport, updated_at = :updated_at"
            params = {**fields, "updated_at": now}
            # description / token / headers 缺失或为空 → 保留现有的（行内启停只发
            # {id,name,url,transport,enabled}，不能把描述和密钥清掉）
            if "description" in body:
                set_clause += ", description = :description"
            if token:
                set_clause += ", token = :token"
                params["token"] = token
            if has_headers:
                set_clause += ", headers = :headers_json"
            if "enabled" in body:
                set_clause += ", enabled = :enabled"
                params["enabled"] = enabled
            conn.execute(
                f"UPDATE mcp_servers SET {set_clause} WHERE id = :sid",
                {**params, "sid": sid},
            )
            conn.commit()
        else:
            sid = "m" + secrets.token_hex(4)
            conn.execute(
                "INSERT INTO mcp_servers (id, name, description, url, transport, token, headers, enabled, tools_count, tools_synced_at, last_error, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,NULL,'',?,?)",
                (sid, fields["name"], fields["description"], fields["url"],
                 fields["transport"], token, headers_json, enabled, now, now),
            )
            conn.commit()
    await _reload_mcp_bridges()
    return mcp_payload()


@app.delete("/app/mcp/{server_id}")
async def app_mcp_delete(server_id: str, request: Request):
    check_auth(request)
    if server_id == KEEPS_MCP_ID:
        raise HTTPException(status_code=409, detail="built-in keeps tools cannot be deleted")
    with db() as conn:
        row = conn.execute("SELECT id FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="mcp server not found")
        # 级联删工具
        conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (server_id,))
        conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        conn.commit()
    await _reload_mcp_bridges()
    return mcp_payload()


def _mcp_write_tools(server_id: str, tools: list) -> None:
    """把工具写库：upsert 元数据（不碰 enabled，用户关掉的选择要保留）
    + 删服务器上已消失的工具 + 刷 tools_count / tools_synced_at。

    sync 和 test（带 id）共用。调用方保证 server_id 已存在。
    """
    now = now_iso()
    with db() as conn:
        for t in tools:
            conn.execute(
                "INSERT INTO mcp_tools (server_id, tool_name, description, input_schema, enabled) "
                "VALUES (?,?,?,?,1) "
                "ON CONFLICT(server_id, tool_name) DO UPDATE SET "
                "description = excluded.description, input_schema = excluded.input_schema",
                (server_id, t["tool_name"], t["description"],
                 json.dumps(t["input_schema"], ensure_ascii=False)),
            )
        # 服务器上消失的工具要删掉——留着会让工具循环调到不存在的东西
        if tools:
            placeholders = ",".join("?" * len(tools))
            conn.execute(
                f"DELETE FROM mcp_tools WHERE server_id = ? AND tool_name NOT IN ({placeholders})",
                (server_id, *[t["tool_name"] for t in tools]),
            )
        else:
            conn.execute("DELETE FROM mcp_tools WHERE server_id = ?", (server_id,))
        conn.execute(
            "UPDATE mcp_servers SET tools_count = ?, tools_synced_at = ?, last_error = '', updated_at = ? "
            "WHERE id = ?",
            (len(tools), now, now, server_id),
        )
        conn.commit()


@app.post("/app/mcp/test")
async def app_mcp_test(request: Request):
    check_auth(request)
    body = await request.json()
    if str(body.get("id") or "").strip() == KEEPS_MCP_ID:
        raise HTTPException(status_code=409, detail="built-in keeps tools do not need testing")
    url = str(body.get("url") or "").strip()
    transport = str(body.get("transport") or "streamable-http").strip()
    if not url:
        return {"ok": False, "error": "url required", "elapsed_ms": 0}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "url must start with http:// or https://", "elapsed_ms": 0}
    if transport not in MCP_TRANSPORTS:
        return {"ok": False, "error": "transport must be 'streamable-http' or 'sse'", "elapsed_ms": 0}
    token = str(body.get("token") or "").strip()
    headers = body.get("headers") if isinstance(body.get("headers"), dict) else {}
    # 编辑已有服务器时测试：留空 → 借库里现有的 token / headers
    sid = str(body.get("id") or "").strip()
    server_exists = False
    if sid:
        with db() as conn:
            row = conn.execute("SELECT token, headers FROM mcp_servers WHERE id = ?", (sid,)).fetchone()
        if row:
            server_exists = True
            if not token:
                token = row["token"]
            if not headers:
                try:
                    headers = json.loads(row["headers"]) if row["headers"] else {}
                except Exception:
                    headers = {}
    started = datetime.now(timezone.utc).timestamp()
    try:
        tools = await mcp_fetch_tools(url, transport, token, headers)
    except Exception as exc:
        elapsed_ms = int((datetime.now(timezone.utc).timestamp() - started) * 1000)
        return {"ok": False, "error": _classify_mcp_error(exc), "elapsed_ms": elapsed_ms}
    elapsed_ms = int((datetime.now(timezone.utc).timestamp() - started) * 1000)
    # 带 id 且服务器还在 → 测试成功顺手写库（等于做了一次 sync），响应标 synced
    synced = False
    if server_exists:
        _mcp_write_tools(sid, tools)
        synced = True
    return {
        "ok": True,
        "tools_count": len(tools),
        "tools": [t["tool_name"] for t in tools],
        "synced": synced,
        "elapsed_ms": elapsed_ms,
    }


@app.post("/app/mcp/{server_id}/sync")
async def app_mcp_sync(server_id: str, request: Request):
    check_auth(request)
    if server_id == KEEPS_MCP_ID:
        raise HTTPException(status_code=409, detail="built-in keeps tools do not need syncing")
    with db() as conn:
        row = conn.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="mcp server not found")
        url, transport, token = row["url"], row["transport"], row["token"]
        try:
            headers = json.loads(row["headers"]) if row["headers"] else {}
        except Exception:
            headers = {}
    try:
        tools = await mcp_fetch_tools(url, transport, token, headers)
    except Exception as exc:
        with db() as conn:
            conn.execute(
                "UPDATE mcp_servers SET last_error = ?, updated_at = ? WHERE id = ?",
                (_classify_mcp_error(exc), now_iso(), server_id),
            )
            conn.commit()
        return mcp_payload()
    _mcp_write_tools(server_id, tools)
    return mcp_payload()


@app.patch("/app/mcp/{server_id}/tools/{tool_name}")
async def app_mcp_tool_toggle(server_id: str, tool_name: str, request: Request):
    check_auth(request)
    if server_id == KEEPS_MCP_ID:
        raise HTTPException(status_code=409, detail="built-in keeps tools are managed by the relay")
    body = await request.json()
    if "enabled" not in body:
        raise HTTPException(status_code=400, detail="enabled required")
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM mcp_tools WHERE server_id = ? AND tool_name = ?",
            (server_id, tool_name),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="tool not found")
        conn.execute(
            "UPDATE mcp_tools SET enabled = ? WHERE server_id = ? AND tool_name = ?",
            (int(bool(body["enabled"])), server_id, tool_name),
        )
        conn.commit()
    return {"ok": True}


# ---- web push subscription management --------------------------------------

@app.get("/app/vapid_public")
async def app_vapid_public(request: Request):
    """Public key the PWA needs to subscribe (not a secret — safe to expose)."""
    check_auth(request)
    return {"key": VAPID_PUBLIC_KEY}


@app.post("/app/subscribe")
async def app_subscribe(request: Request):
    """PWA turns on lock-screen notifications: store the subscription."""
    check_auth(request)
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="endpoint + keys.p256dh + keys.auth required")
    ua = request.headers.get("user-agent", "")[:200]
    save_subscription(endpoint, p256dh, auth, ua)
    return {"ok": True, "count": len(list_subscriptions())}


@app.post("/app/unsubscribe")
async def app_unsubscribe(request: Request):
    """PWA turns off lock-screen notifications: drop the subscription."""
    check_auth(request)
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    if endpoint:
        delete_subscription(endpoint)
    return {"ok": True}


@app.post("/app/push_test")
async def app_push_test(request: Request):
    """Self-test: push one test notification to every subscription."""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("text") if isinstance(body, dict) else None) or f"测试通知 · {AI_NAME}在这儿"
    res = await push_to_all({"title": AI_NAME, "body": text, "url": APP_PATH, "id": 0})
    return {"ok": True, **res}


# ---- optional API loop control --------------------------------------------

def brain_display_model(target: str | None = None) -> str:
    """Return the model name that is actually selected for the active brain.

    Desktop's selected model lives in the unified ``sdk_config.json`` store and
    reaches each SDK worker via spawn env + the runtime_settings callback (immediate
    apply), not a per-worker snapshot file. The API path has no duplicate model
    setting: it reads the active chat station, whose row is the routing source of
    truth for api_loop as well.
    """
    if (target or brain_target()) == "desktop":
        model = sdk_config["model"]
        return model.title() if model in SDK_MODELS else model
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT active_model, default_model, models FROM stations "
                "WHERE kind = 'chat' AND active = 1 LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return "API"
    for value in (row["active_model"], row["default_model"]):
        model = str(value or "").strip()
        if model:
            return model
    models = str(row["models"] or "")
    return next((part.strip() for part in models.split(",") if part.strip()), "API")


@app.get("/app/brain")
async def get_brain(request: Request):
    check_auth(request)
    target = brain_target()
    return {"target": target, "model": brain_display_model(target)}


@app.post("/app/brain")
async def set_brain(request: Request):
    check_auth(request)
    body = await request.json()
    target = str(body.get("target") or "").strip()
    try:
        target = brain_router().set_target(target)
    except ValueError:
        raise HTTPException(status_code=400, detail="target must be 'desktop' or 'loop'")
    return {"target": target, "model": brain_display_model(target)}


@app.post("/app/sdk/permission")
async def sdk_permission(request: Request):
    check_auth(request)
    body = await request.json()
    permission_id = str(body.get("permission_id") or "").strip()
    session_key = str(body.get("session_key") or "").strip()
    if not permission_id or not session_key or not isinstance(body.get("allow"), bool):
        raise HTTPException(status_code=400, detail="permission_id, session_key and allow required")
    from backend.demo_visits import visitor
    if visitor.get():
        source = permission_id.removeprefix("replay-")
        with db() as conn:
            own = conn.execute("SELECT id FROM messages WHERE id=? AND direction='in'",(source,)).fetchone()
        if not own:raise HTTPException(status_code=404, detail="permission request expired")
    updated_input = body.get("updated_input")
    if updated_input is not None and not isinstance(updated_input, dict):
        raise HTTPException(status_code=400, detail="updated_input must be an object")
    accepted = await _sdk_provider.decide_permission(
        session_key=session_key,
        permission_id=permission_id,
        allow=body["allow"],
        updated_input=updated_input,
    )
    if not accepted:
        raise HTTPException(status_code=404, detail="permission request expired")
    return {"ok": True}


@app.get("/app/sdk/usage")
async def sdk_usage(request: Request):
    check_auth(request)
    return _load_sdk_rate_limit()


@app.get("/app/sdk/context")
async def sdk_context(request: Request):
    """MORE 页主动读取活 SDK 会话的上下文；不依赖下一轮对话的 SSE。"""
    check_auth(request)
    try:
        usage = await _sdk_provider.get_context_usage()
    except Exception:
        usage = None
    return {"usage": usage}


def _sdk_live_resolved_model() -> str:
    for session in _sdk_provider.status()["sessions"].values():
        if session.get("resolved_model"):
            return session["resolved_model"]
    return ""


def _sdk_model_window(model: str) -> int:
    """静态窗口表（VPS 实测 2026-09-06，getContextUsage rawMaxTokens）：
    [1m] 后缀 = 选 1M 窗口，优先判定（对 4.x 也生效：opus-4-6[1m]/sonnet-4-6[1m] 探针 = 1M，
    其基名 200K，故必须先于子名匹配）；Opus 4.7/4.8 与 5 系原生 1M；
    Opus 4.5/4.6、Sonnet 4.x、Haiku 4.5 基名 200K。"""
    if "[1m]" in model:
        return 1_000_000
    if "opus-4-7" in model or "opus-4-8" in model:
        return 1_000_000
    if "haiku" in model or "sonnet-4" in model or "opus-4-5" in model or "opus-4-6" in model:
        return 200_000
    return 1_000_000


async def _autocompact_tokens(pct: int | None) -> int | None:
    """pct → tokens。优先 live rawMaxTokens（已解析 autocompact 窗口），否则静态表。
    字段名按 CLI 2.1.258 运行时实况是 camelCase（.d.ts 写 snake_case，实测不符）。"""
    if pct is None:
        return None
    raw_max = 0
    try:
        usage = await _sdk_provider.get_context_usage()
        if isinstance(usage, dict):
            raw_max = int(usage.get("rawMaxTokens") or usage.get("raw_max_tokens") or 0)
    except Exception:
        raw_max = 0
    if raw_max <= 0:
        raw_max = _sdk_model_window(sdk_config["model"])
    return max(1, round(raw_max * pct / 100))


async def _sdk_model_whitelist() -> set:
    """别名 ∪ 静态 ID ∪ live supportedModels() 的 value（有活会话时）。"""
    values = set(SDK_MODEL_ALIASES) | set(SDK_STATIC_MODEL_IDS)
    try:
        models = await _sdk_provider.list_models()
    except Exception:
        models = None
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict) and item.get("value"):
                values.add(str(item["value"]))
    return values


@app.get("/app/sdk/config")
async def sdk_config_get(request: Request):
    """MORE 页模型/思考/上下文三卡的数据源；thinking 来自 bridge_ready。"""
    check_auth(request)
    status = _sdk_provider.status()
    resolved = _sdk_live_resolved_model()
    return {
        "model": sdk_config["model"],
        "effort": sdk_config["effort"],
        "auto_compact_pct": sdk_config["auto_compact_pct"],
        "thinking": status.get("thinking"),
        "resolved_model": resolved or sdk_config["resolved_model"],
        "updated_at": sdk_config["updated_at"],
        "sessions": {
            key: {
                "model": session["model"],
                "effort": session["effort"],
                "resolved_model": session["resolved_model"],
            }
            for key, session in status["sessions"].items()
        },
    }


@app.post("/app/sdk/config")
async def sdk_config_post(request: Request):
    """三卡写入口。effort 只存不发（新会话 spawn 生效）；model 暖会话 setModel
    直换（历史保留）；auto_compact_pct 立即下发（CC 侧策略，不打穿缓存）。
    fable/best/claude-fable* 必须 confirm_usage_credits===true（SDK 无确认直接扣）。
    """
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    model = str(body.get("model") or "").strip()
    effort = str(body.get("effort") or "").strip()
    pct = body.get("auto_compact_pct", "__missing__")
    if pct != "__missing__":
        if pct is not None and pct not in SDK_AUTOCOMPACT_PRESETS:
            raise HTTPException(status_code=400, detail="auto_compact_pct must be 60, 75, 90 or null")
    if effort and effort not in SDK_EFFORT_TIERS:
        raise HTTPException(status_code=400, detail=f"effort must be one of {list(SDK_EFFORT_TIERS)}")
    if model:
        if _usage_credits_model(model) and body.get("confirm_usage_credits") is not True:
            raise HTTPException(
                status_code=400,
                detail="confirm_usage_credits=true required for fable/best (usage credits)",
            )
        whitelist = await _sdk_model_whitelist()
        if model not in whitelist:
            raise HTTPException(status_code=400, detail="unknown model")

    async with _sdk_config_lock:
        model_changed = bool(model) and model != sdk_config["model"]
        if model:
            sdk_config["model"] = model
        if effort:
            sdk_config["effort"] = effort
        if pct != "__missing__":
            sdk_config["auto_compact_pct"] = pct
        sdk_config["updated_at"] = now_iso()
        if model_changed:
            resolved = _sdk_live_resolved_model()
            if resolved:
                sdk_config["resolved_model"] = resolved
        _save_sdk_config()

    applied = {}
    if model_changed:
        try:
            applied = await _sdk_provider.apply_model(sdk_config["model"])
        except Exception as exc:
            applied = {"error": f"{type(exc).__name__}: {exc}"}
        await broadcast(
            app_subs,
            {"type": "sdk_model_applied", "model": sdk_config["model"], "applied": applied},
        )
    autocompact_applied = {}
    if pct != "__missing__":
        tokens = await _autocompact_tokens(sdk_config["auto_compact_pct"])
        try:
            autocompact_applied = await _sdk_provider.apply_autocompact(tokens)
        except Exception as exc:
            autocompact_applied = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "model": sdk_config["model"],
        "effort": sdk_config["effort"],
        "auto_compact_pct": sdk_config["auto_compact_pct"],
        "model_applied": applied,
        "autocompact_applied": autocompact_applied,
        "thinking": _sdk_provider.status().get("thinking"),
    }


@app.get("/app/sdk/models")
async def sdk_models(request: Request):
    """模型目录。静态完整版本表为基线，live 只补充当前 CLI 的别名元数据。"""
    check_auth(request)
    try:
        models = await _sdk_provider.list_models()
    except Exception:
        models = None
    if isinstance(models, list):
        return {
            "source": "static+live",
            "models": _model_catalog_with_live(models),
            "catalog_note": "列表为静态维护，可能滞后",
        }
    return {
        "source": "static",
        "models": _static_model_catalog(),
        "catalog_note": "列表为静态维护，可能滞后",
    }


@app.post("/app/sdk/session/reset")
async def sdk_session_reset(request: Request):
    """结束指定/全部 SDK 会话 = 清空上下文重来（销毁性；前端已二次确认）。"""
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_key = str(body.get("session_key") or "").strip() or None
    stopped = await _sdk_provider.reset_sessions(session_key)
    print(f"[sdk:lifecycle] session reset requested key={session_key or '(all)'} "
          f"stopped={stopped}", flush=True)
    await broadcast(app_subs, {"type": "sdk_sessions_reset", "stopped": stopped})
    return {"ok": True, "stopped": stopped}


@app.get("/app/loop_config")
async def get_loop_config(request: Request):
    check_auth(request)
    return loop_json("/loop/config")


@app.post("/app/loop_config")
async def set_loop_config(request: Request):
    check_auth(request)
    return loop_json("/loop/config", method="POST", body=await request.json())


# 关于页「可见窗口」五档 —— 白名单，只认这五个值，别的不收
HISTORY_N_PRESETS = (24, 48, 80, 120, 200)


@app.post("/app/history-n")
async def set_history_n(request: Request):
    """设置 loop 可见窗口。转发到 api_loop /loop/config（它原子写 config.json，
    重启不丢）。/loop/config 本身无鉴权、不走公网，这里只在 relay 一层暴露。
    """
    check_auth(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    try:
        n = int(body.get("n"))
    except (TypeError, ValueError):
        n = -1
    if n not in HISTORY_N_PRESETS:
        raise HTTPException(status_code=400, detail="n must be one of 24,48,80,120,200")
    res = loop_json("/loop/config", method="POST", body={"history_n": n})
    if not isinstance(res, dict) or "history_n" not in res:
        raise HTTPException(status_code=502, detail="api_loop returned unexpected shape")
    return {"ok": True, "history_n": res["history_n"]}


def _loop_history_n() -> int:
    """可见窗口：loop 当前生效的 history_n（本会话内可见条数）。

    口径与 api_loop.history_n() 一致：config 的 history_n → HISTORY_N env → 24，
    钳到 [0,200]。config 读不到就退回 env/默认。
    """
    cfg_path = os.environ.get("LOOP_CONFIG", "")
    try:
        if cfg_path:
            cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
            n = cfg.get("history_n")
            if n is None:
                n = os.environ.get("HISTORY_N", "24")
            return max(0, min(int(n), 200))
    except Exception:
        pass
    try:
        return max(0, min(int(os.environ.get("HISTORY_N", "24")), 200))
    except Exception:
        return 24


@app.get("/app/session-stats")
async def app_session_stats(request: Request):
    """关于页「会话流」卡片四数。全部读 relay.db / loop config，不新增表。

    可见窗口   loop 当前 history_n（本会话，口径同 api_loop.history_n）
    本机记录   MIN(ts) + COUNT(*) WHERE direction='in'（一轮 = 一条 in）
    上轮开销   最后一条 reply 的 meta.api.usage.prompt_tokens
    存量       COUNT(*) 全表
    """
    check_auth(request)
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        rounds = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE direction = 'in'").fetchone()["n"]
        start_ts = conn.execute("SELECT MIN(ts) AS t FROM messages").fetchone()["t"]
        last_row = conn.execute(
            "SELECT meta FROM messages WHERE kind = 'reply' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    last_prompt = None
    last_sdk_usage = None
    if last_row:
        try:
            meta = json.loads(last_row["meta"])
            raw_sdk_usage = meta.get("sdk_usage")
            if isinstance(raw_sdk_usage, dict):
                last_sdk_usage = {
                    "input_tokens": raw_sdk_usage.get("input_tokens"),
                    "output_tokens": raw_sdk_usage.get("output_tokens"),
                    "cache_read_input_tokens": raw_sdk_usage.get("cache_read_input_tokens"),
                    "cache_creation_input_tokens": raw_sdk_usage.get("cache_creation_input_tokens"),
                    "request_steps": raw_sdk_usage.get("request_steps"),
                }
            last_prompt = (meta.get("api") or {}).get("usage") or {}
            last_prompt = last_prompt.get("prompt_tokens")
        except Exception:
            last_prompt = None
    return {
        "visible_window": _loop_history_n(),
        "start_ts": start_ts,
        "rounds": int(rounds or 0),
        "last_prompt_tokens": last_prompt,
        "last_sdk_usage": last_sdk_usage,
        "provider_target": brain_target(),
        "total": int(total or 0),
    }


@app.get("/app/sessions")
async def app_sessions(request: Request):
    check_auth(request)
    return loop_json("/loop/sessions")


@app.post("/app/sessions")
async def app_sessions_create(request: Request):
    check_auth(request)
    body = await request.json()
    if "since_id" not in body:
        try:
            with db() as conn:
                row = conn.execute("SELECT MAX(id) AS id FROM messages").fetchone()
                body["since_id"] = int(row["id"] or 0)
        except Exception:
            body["since_id"] = 0
    return loop_json("/loop/sessions", method="POST", body=body)


@app.patch("/app/sessions/{session_id}")
async def app_sessions_patch(session_id: str, request: Request):
    check_auth(request)
    return loop_json(f"/loop/sessions/{urllib.parse.quote(session_id)}", method="PATCH", body=await request.json())


@app.delete("/app/session/{session_id}")
async def app_session_delete(session_id: str, request: Request):
    """物理删除一个会话窗口（真删，不是归档）。

    1. relay.db 里该 api_session 的全部 messages 删干净
    2. 转发 loop 从 config sessions 数组移除该条（active_session 恰好是它 → 落到最后一个剩余窗口）
    loop 里 config 没有该条时（如历史遗留）messages 已删，照常返回最新会话列表。
    """
    check_auth(request)
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    with db() as conn:
        conn.execute("DELETE FROM messages WHERE json_extract(meta, '$.api_session') = ?", (session_id,))
        conn.commit()
    try:
        loop_json(f"/loop/sessions/{urllib.parse.quote(session_id)}", method="DELETE")
    except HTTPException:
        pass  # config 无该条：messages 已删，忽略并返回列表
    return loop_json("/loop/sessions")


# Local demo intercepts disabled integrations before API routing.
from backend.demo_local import install
install(app)

# Mount static files LAST — after all API routes, so /app/* and /healthz take priority.
from backend.demo_visits import VisitPath
UPLOAD_DIR=VisitPath(UPLOAD_DIR,"uploads")
KEEP_MEDIA_DIR=VisitPath(KEEP_MEDIA_DIR,"uploads/keeps")
VOICE_DIR=VisitPath(VOICE_DIR,"voice")
STATIC_DIR = VisitPath(Path(os.environ.get("RELAY_STATIC_DIR", str(Path(__file__).resolve().parent.parent / "static"))),"static")
if STATIC_DIR.is_dir():

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

