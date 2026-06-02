from __future__ import annotations

import json
import hashlib
import importlib.util
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from backend.agent_runtime import AgentDecision, build_agent_step_prompt
from backend.db_migrations import LATEST_SCHEMA_REVISION, ensure_migration_ledger, migration_status
from backend.logging_config import (
    diagnostics_config,
    export_log_bundle,
    get_logger,
    log_event,
    log_exception,
    recent_logs,
    redact,
    reset_request_id,
    set_request_id,
)
from backend.model_gateway import GatewayResult, ModelGateway, ProviderRuntimeConfig
from backend.observability import configure_optional_telemetry
from backend.runtime_health import (
    callable_check,
    database_check,
    directory_check,
    summarize_readiness,
    telemetry_check,
    worker_check,
)
from backend.source_registry import CANONICAL_CHANNEL_NAMES, source_catalog, tool_catalog
from backend.subprocess_utils import hidden_window_creationflags
from backend.wechat_rss import (
    add_werss_subscription,
    delete_werss_subscription,
    fetch_werss_qr_image,
    managed_werss_status,
    managed_werss_start_available,
    normalize_werss_config,
    public_werss_config,
    search_werss_public_accounts,
    start_managed_werss,
    start_werss_wechat_login,
    werss_wechat_login_status,
)
from backend.worker import CollectionWorker

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = ROOT / "VERSION"
APP_VERSION = (
    os.environ.get("ALPHADESK_VERSION", "").strip()
    or (VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "0.2.0")
)
DATA_DIR = Path(os.environ.get("ALPHADESK_DATA_DIR", str(ROOT / "data"))).expanduser().resolve()
DB_PATH = DATA_DIR / "workbench.db"
KEY_PATH = DATA_DIR / "local.key"
SKILLS_DIR = ROOT / "skills"
CODEX_POLICY_PATH = ROOT / "config" / "codex-policy.toml"
RED_LINES_PATH = ROOT / "config" / "research-red-lines.toml"
MIN_COLLECTION_INTERVAL = timedelta(minutes=15)
REPORT_DEDUP_INTERVAL = timedelta(minutes=2)
MASKED_SECRET = "****************"
BROWSER_WORKSPACE_PUBLIC_URL = os.environ.get("ALPHADESK_BROWSER_PUBLIC_URL", "").strip()
CHANNEL_LOGIN_PROCESSES: dict[str, subprocess.Popen] = {}
CHANNEL_LOGIN_PROCESSES_LOCK = threading.Lock()
MAX_MX_HAR_BYTES = 32 * 1024 * 1024
MAX_MX_HAR_UPLOAD_BYTES = 60 * 1024 * 1024
MARKET_DATA_DEFAULT_CONFIG = {
    "adapter": "market_data_aggregate",
    "enable_akshare": True,
    "enable_baostock": True,
    "enable_tushare": True,
    "tushare_token": "",
    "component_timeout_seconds": 35,
}
DATA_DIR.mkdir(parents=True, exist_ok=True)
logger = get_logger("api")
frontend_logger = get_logger("frontend")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    log_event(logger, "INFO", "application.startup.worker")
    collection_worker.start()
    log_event(logger, "INFO", "application.startup.channel_check_scheduled")
    threading.Thread(target=refresh_browser_channel_states, daemon=True, name="channel-state-refresh").start()
    try:
        yield
    finally:
        log_event(logger, "INFO", "application.shutdown.worker")
        collection_worker.stop()


app = FastAPI(title="A股成长猎手本地服务", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "null"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
TELEMETRY_STATUS = configure_optional_telemetry(app)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]
    token = set_request_id(request_id)
    started_at = time.perf_counter()
    quiet_poll = request.method == "GET" and request.url.path in {"/health", "/api/dashboard", "/api/audit", "/api/diagnostics/logs"}
    if not quiet_poll:
        log_event(
            logger,
            "INFO",
            "http.request.started",
            method=request.method,
            path=request.url.path,
            query=request.url.query,
        )
    try:
        response = await call_next(request)
    except Exception as exc:
        log_exception(
            logger,
            "http.request.failed",
            exc,
            method=request.method,
            path=request.url.path,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
        )
        reset_request_id(token)
        raise
    response.headers["X-Request-ID"] = request_id
    if not quiet_poll or response.status_code >= 400:
        log_event(
            logger,
            "WARNING" if response.status_code >= 400 else "INFO",
            "http.request.completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
        )
    reset_request_id(token)
    return response


class ProviderInput(BaseModel):
    name: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = Field(default="", repr=False)
    protocol: Literal["openai_chat_completions", "openai_responses"] = "openai_chat_completions"
    enabled: bool = True
    extra_body: dict = Field(default_factory=dict)


class TaskInput(BaseModel):
    title: str
    target: str
    objective: str
    skill_name: str = "a-share-growth-hunter"
    lookback_days: int = Field(default=30, ge=1, le=30)


class ChannelInput(BaseModel):
    name: str
    type: str
    url: str = ""
    collection_mode: Literal["akshare", "industry_news", "wechat_rss", "requests", "playwright", "manual"] = "playwright"
    status: Literal["online", "pending", "offline"] = "pending"
    notes: str = ""
    validation_url: str = ""
    success_url_contains: str = ""
    success_selector: str = ""
    group_ids: list[str] = Field(default_factory=list)
    parsing_strategy: Literal["fixed", "hybrid", "ai"] = "hybrid"
    normalization_quality_threshold: int = Field(default=60, ge=0, le=100)
    max_scrolls: int = Field(default=8, ge=1, le=30)
    research_enabled: bool = False


class SourceJobInput(BaseModel):
    action: Literal["collect", "collect_report", "report"]
    channel_ids: list[str] = Field(min_length=1)
    lookback_days: int = Field(default=30, ge=1, le=30)
    report_title: str = "信源数据聚合报告"
    skill_name: str = "a-share-growth-hunter"
    parent_task_id: str = ""
    query: str = ""
    evidence_layer: str = ""


class SourceSnapshotInput(BaseModel):
    channel_id: str
    occurred_at: datetime
    source_url: str
    content: str


class NormalizedSourceItem(BaseModel):
    item_key: str = ""
    occurred_at: str = ""
    author: str = ""
    title: str = ""
    content: str
    source_url: str = ""
    attachments: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    quality_score: int = Field(default=0, ge=0, le=100)


class NormalizationResult(BaseModel):
    items: list[NormalizedSourceItem]
    quality_score: int = Field(default=0, ge=0, le=100)
    notes: str = ""


class CompleteSourceJobInput(BaseModel):
    snapshots: list[SourceSnapshotInput] = Field(default_factory=list)


class MxHarImportInput(BaseModel):
    har_text: str = Field(min_length=1)


class MarketDataConfigInput(BaseModel):
    enable_akshare: bool = True
    enable_baostock: bool = True
    enable_tushare: bool = True
    tushare_token: str = Field(default="", repr=False)
    clear_tushare_token: bool = False
    component_timeout_seconds: int = Field(default=35, ge=5, le=120)


class WechatRssConfigInput(BaseModel):
    base_url: str = "http://127.0.0.1:8001"
    feed_ids: list[str] = Field(default_factory=lambda: ["all"])
    access_key: str = Field(default="", repr=False)
    secret_key: str = Field(default="", repr=False)
    admin_username: str = "admin"
    admin_password: str = Field(default="", repr=False)
    clear_credentials: bool = False
    timeout_seconds: int = Field(default=20, ge=3, le=120)
    max_items_per_feed: int = Field(default=100, ge=1, le=500)


class WechatRssSubscriptionInput(BaseModel):
    id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    avatar: str = Field(default="", max_length=1_000)
    intro: str = Field(default="", max_length=1_000)


class FrontendLogInput(BaseModel):
    timestamp: str = ""
    level: Literal["debug", "info", "warning", "error"] = "info"
    event: str = Field(min_length=1, max_length=120)
    message: str = Field(default="", max_length=2_000)
    context: dict[str, Any] = Field(default_factory=dict)


class FrontendLogBatchInput(BaseModel):
    entries: list[FrontendLogInput] = Field(min_length=1, max_length=100)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def canonical_scope_key(query: str) -> str:
    value = str(query or "").strip()
    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", value)
    return code_match.group(1) if code_match else value.casefold()


def ensure_scope_aware_storage(conn: sqlite3.Connection) -> None:
    snapshot_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='source_snapshots'"
    ).fetchone()["sql"]
    if "UNIQUE(channel_id, source_url, occurred_at, scope_type, scope_key)" not in snapshot_sql:
        conn.executescript(
            """
            ALTER TABLE source_snapshots RENAME TO source_snapshots_legacy;
            CREATE TABLE source_snapshots (
              id TEXT PRIMARY KEY,
              channel_id TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              collected_at TEXT NOT NULL,
              source_url TEXT NOT NULL,
              content TEXT NOT NULL,
              normalization_status TEXT NOT NULL DEFAULT 'pending',
              normalized_at TEXT NOT NULL DEFAULT '',
              normalization_error TEXT NOT NULL DEFAULT '',
              normalized_item_count INTEGER NOT NULL DEFAULT 0,
              scope_type TEXT NOT NULL DEFAULT 'general',
              scope_key TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id, source_url, occurred_at, scope_type, scope_key)
            );
            INSERT INTO source_snapshots(
              id,channel_id,occurred_at,collected_at,source_url,content,normalization_status,
              normalized_at,normalization_error,normalized_item_count,scope_type,scope_key
            )
            SELECT id,channel_id,occurred_at,collected_at,source_url,content,normalization_status,
                   normalized_at,normalization_error,normalized_item_count,scope_type,scope_key
            FROM source_snapshots_legacy;
            DROP TABLE source_snapshots_legacy;
            """
        )
    normalized_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='normalized_source_items'"
    ).fetchone()["sql"]
    if "UNIQUE(channel_id,item_key,scope_type,scope_key)" not in normalized_sql:
        conn.executescript(
            """
            ALTER TABLE normalized_source_items RENAME TO normalized_source_items_legacy;
            CREATE TABLE normalized_source_items (
              id TEXT PRIMARY KEY,
              snapshot_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              item_key TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              author TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL,
              source_url TEXT NOT NULL DEFAULT '',
              attachments TEXT NOT NULL DEFAULT '[]',
              metadata TEXT NOT NULL DEFAULT '{}',
              quality_score INTEGER NOT NULL DEFAULT 0,
              normalization_mode TEXT NOT NULL,
              created_at TEXT NOT NULL,
              scope_type TEXT NOT NULL DEFAULT 'general',
              scope_key TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id,item_key,scope_type,scope_key)
            );
            INSERT INTO normalized_source_items(
              id,snapshot_id,channel_id,item_key,occurred_at,author,title,content,source_url,
              attachments,metadata,quality_score,normalization_mode,created_at,scope_type,scope_key
            )
            SELECT n.id,n.snapshot_id,n.channel_id,n.item_key,n.occurred_at,n.author,n.title,n.content,n.source_url,
                   n.attachments,n.metadata,n.quality_score,n.normalization_mode,n.created_at,
                   COALESCE(s.scope_type,'general'),COALESCE(s.scope_key,'')
            FROM normalized_source_items_legacy n
            LEFT JOIN source_snapshots s ON s.id=n.snapshot_id;
            DROP TABLE normalized_source_items_legacy;
            """
        )
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_source_snapshots_channel_scope_time
          ON source_snapshots(channel_id,scope_type,scope_key,occurred_at);
        CREATE INDEX IF NOT EXISTS idx_normalized_items_channel_scope_time
          ON normalized_source_items(channel_id,scope_type,scope_key,occurred_at);
        CREATE INDEX IF NOT EXISTS idx_source_jobs_status_created
          ON source_collection_jobs(status,created_at);
        """
    )


def cipher() -> Fernet:
    if not KEY_PATH.exists():
        KEY_PATH.write_bytes(Fernet.generate_key())
    return Fernet(KEY_PATH.read_bytes())


def channel_request_config(channel_id: str) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT encrypted_config FROM channel_request_configs WHERE channel_id=?",
            (channel_id,),
        ).fetchone()
    if not row or not row["encrypted_config"]:
        return {}
    try:
        return json.loads(cipher().decrypt(row["encrypted_config"].encode()).decode())
    except Exception:
        return {}


def save_channel_request_config(channel_id: str, config: dict) -> None:
    encrypted = cipher().encrypt(json.dumps(config, ensure_ascii=False).encode()).decode()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO channel_request_configs(channel_id,encrypted_config,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET encrypted_config=excluded.encrypted_config,updated_at=excluded.updated_at
            """,
            (channel_id, encrypted, now()),
        )


def market_data_config() -> dict:
    return {**MARKET_DATA_DEFAULT_CONFIG, **channel_request_config("akshare"), "adapter": "market_data_aggregate"}


def market_data_config_public() -> dict:
    config = market_data_config()
    token_configured = bool(str(config.get("tushare_token") or "").strip())
    config["tushare_token_configured"] = token_configured
    config["tushare_token"] = MASKED_SECRET if token_configured else ""
    return config


def wechat_rss_config() -> dict:
    return normalize_werss_config(channel_request_config("wechat-mp-rss"))


def wechat_rss_config_public() -> dict:
    return public_werss_config(wechat_rss_config())


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_request_configs (
              channel_id TEXT PRIMARY KEY,
              encrypted_config TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_providers (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              base_url TEXT NOT NULL,
              model TEXT NOT NULL,
              protocol TEXT NOT NULL DEFAULT 'openai_chat_completions',
              encrypted_api_key TEXT NOT NULL DEFAULT '',
              extra_body TEXT NOT NULL DEFAULT '{}',
              enabled INTEGER NOT NULL DEFAULT 1,
              is_default INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'untested',
              latency_ms INTEGER NOT NULL DEFAULT 0,
              last_test_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_call_logs (
              id TEXT PRIMARY KEY,
              provider_id TEXT NOT NULL,
              purpose TEXT NOT NULL,
              status TEXT NOT NULL,
              latency_ms INTEGER NOT NULL DEFAULT 0,
              input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0,
              request_count INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              target TEXT NOT NULL,
              objective TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              report TEXT,
              skill_name TEXT NOT NULL DEFAULT 'a-share-growth-hunter',
              lookback_days INTEGER NOT NULL DEFAULT 30,
              agent_state TEXT NOT NULL DEFAULT '{}',
              agent_error TEXT NOT NULL DEFAULT '',
              report_anchor TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS channels (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              type TEXT NOT NULL,
              url TEXT NOT NULL DEFAULT '',
              collection_mode TEXT NOT NULL,
              status TEXT NOT NULL,
              notes TEXT NOT NULL DEFAULT '',
              validation_url TEXT NOT NULL DEFAULT '',
              success_url_contains TEXT NOT NULL DEFAULT '',
              success_selector TEXT NOT NULL DEFAULT '',
              group_ids TEXT NOT NULL DEFAULT '[]',
              parsing_strategy TEXT NOT NULL DEFAULT 'fixed',
              normalization_quality_threshold INTEGER NOT NULL DEFAULT 60,
              max_scrolls INTEGER NOT NULL DEFAULT 8,
              research_enabled INTEGER NOT NULL DEFAULT 0,
              builtin INTEGER NOT NULL DEFAULT 0,
              last_check TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_collection_jobs (
              id TEXT PRIMARY KEY,
              action TEXT NOT NULL,
              channel_ids TEXT NOT NULL,
              windows TEXT NOT NULL,
              lookback_days INTEGER NOT NULL,
              skill_name TEXT NOT NULL,
              report_title TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              report TEXT,
              report_anchor TEXT NOT NULL DEFAULT '',
              started_at TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL DEFAULT '',
              snapshot_count INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              parent_task_id TEXT NOT NULL DEFAULT '',
              query TEXT NOT NULL DEFAULT '',
              evidence_layer TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS source_collection_watermarks (
              channel_id TEXT PRIMARY KEY,
              last_success_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_collection_watermarks_v2 (
              channel_id TEXT NOT NULL,
              scope_key TEXT NOT NULL DEFAULT '',
              last_success_at TEXT NOT NULL,
              PRIMARY KEY(channel_id,scope_key)
            );
            CREATE TABLE IF NOT EXISTS source_snapshots (
              id TEXT PRIMARY KEY,
              channel_id TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              collected_at TEXT NOT NULL,
              source_url TEXT NOT NULL,
              content TEXT NOT NULL,
              normalization_status TEXT NOT NULL DEFAULT 'pending',
              normalized_at TEXT NOT NULL DEFAULT '',
              normalization_error TEXT NOT NULL DEFAULT '',
              normalized_item_count INTEGER NOT NULL DEFAULT 0,
              scope_type TEXT NOT NULL DEFAULT 'general',
              scope_key TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id, source_url, occurred_at, scope_type, scope_key)
            );
            CREATE TABLE IF NOT EXISTS normalized_source_items (
              id TEXT PRIMARY KEY,
              snapshot_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              item_key TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              author TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL,
              source_url TEXT NOT NULL DEFAULT '',
              attachments TEXT NOT NULL DEFAULT '[]',
              metadata TEXT NOT NULL DEFAULT '{}',
              quality_score INTEGER NOT NULL DEFAULT 0,
              normalization_mode TEXT NOT NULL,
              created_at TEXT NOT NULL,
              scope_type TEXT NOT NULL DEFAULT 'general',
              scope_key TEXT NOT NULL DEFAULT '',
              UNIQUE(channel_id,item_key,scope_type,scope_key)
            );
            CREATE TABLE IF NOT EXISTS source_job_snapshots (
              job_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              PRIMARY KEY(job_id,snapshot_id)
            );
            CREATE TABLE IF NOT EXISTS source_collection_runs (
              job_id TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL DEFAULT '',
              completed_at TEXT NOT NULL DEFAULT '',
              snapshot_count INTEGER NOT NULL DEFAULT 0,
              duplicate_count INTEGER NOT NULL DEFAULT 0,
              error TEXT NOT NULL DEFAULT '',
              coverage_complete INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(job_id,channel_id)
            );
            CREATE TABLE IF NOT EXISTS agent_events (
              id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "skill_name" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN skill_name TEXT NOT NULL DEFAULT 'a-share-growth-hunter'")
        if "lookback_days" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN lookback_days INTEGER NOT NULL DEFAULT 30")
        for column, ddl in (
            ("agent_state", "TEXT NOT NULL DEFAULT '{}'"),
            ("agent_error", "TEXT NOT NULL DEFAULT ''"),
            ("report_anchor", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(channels)")}
        for column in ("validation_url", "success_url_contains", "success_selector", "last_check"):
            if column not in columns:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        if "group_ids" not in columns:
            conn.execute("ALTER TABLE channels ADD COLUMN group_ids TEXT NOT NULL DEFAULT '[]'")
        parsing_strategy_added = "parsing_strategy" not in columns
        research_enabled_added = "research_enabled" not in columns
        for column, ddl in (
            ("parsing_strategy", "TEXT NOT NULL DEFAULT 'fixed'"),
            ("normalization_quality_threshold", "INTEGER NOT NULL DEFAULT 60"),
            ("max_scrolls", "INTEGER NOT NULL DEFAULT 8"),
            ("research_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE channels ADD COLUMN {column} {ddl}")
        if parsing_strategy_added:
            conn.execute("UPDATE channels SET parsing_strategy='hybrid' WHERE id IN ('zsxq','web-rumors')")
        if research_enabled_added:
            conn.execute("UPDATE channels SET research_enabled=1 WHERE id='akshare'")
        snapshot_columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_snapshots)")}
        snapshot_scope_migration_required = "scope_type" not in snapshot_columns or "scope_key" not in snapshot_columns
        for column, ddl in (
            ("normalization_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("normalized_at", "TEXT NOT NULL DEFAULT ''"),
            ("normalization_error", "TEXT NOT NULL DEFAULT ''"),
            ("normalized_item_count", "INTEGER NOT NULL DEFAULT 0"),
            ("scope_type", "TEXT NOT NULL DEFAULT 'general'"),
            ("scope_key", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in snapshot_columns:
                conn.execute(f"ALTER TABLE source_snapshots ADD COLUMN {column} {ddl}")
        job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_collection_jobs)")}
        for column, ddl in (
            ("started_at", "TEXT NOT NULL DEFAULT ''"),
            ("completed_at", "TEXT NOT NULL DEFAULT ''"),
            ("snapshot_count", "INTEGER NOT NULL DEFAULT 0"),
            ("error", "TEXT NOT NULL DEFAULT ''"),
            ("parent_task_id", "TEXT NOT NULL DEFAULT ''"),
            ("query", "TEXT NOT NULL DEFAULT ''"),
            ("evidence_layer", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in job_columns:
                conn.execute(f"ALTER TABLE source_collection_jobs ADD COLUMN {column} {ddl}")
        if snapshot_scope_migration_required:
            conn.execute(
                """
                UPDATE source_snapshots
                SET scope_type='research',
                    scope_key=COALESCE(
                      (
                        SELECT j.query
                        FROM source_job_snapshots js
                        JOIN source_collection_jobs j ON j.id=js.job_id
                        WHERE js.snapshot_id=source_snapshots.id
                          AND (j.parent_task_id<>'' OR j.query<>'' OR j.evidence_layer<>'')
                        ORDER BY j.created_at DESC
                        LIMIT 1
                      ),
                      ''
                    )
                WHERE EXISTS (
                  SELECT 1
                  FROM source_job_snapshots js
                  JOIN source_collection_jobs j ON j.id=js.job_id
                  WHERE js.snapshot_id=source_snapshots.id
                    AND (j.parent_task_id<>'' OR j.query<>'' OR j.evidence_layer<>'')
                )
                """
            )
        ensure_scope_aware_storage(conn)
        conn.execute("UPDATE source_collection_jobs SET status='generating_report' WHERE status='generating'")
        channel_count = conn.execute("SELECT COUNT(*) AS count FROM channels").fetchone()["count"]
        if not channel_count:
            conn.executemany(
                """
                INSERT INTO channels(id,name,type,url,collection_mode,status,notes,parsing_strategy,normalization_quality_threshold,max_scrolls,research_enabled,builtin,updated_at)
                VALUES(:id,:name,:type,:url,:collection_mode,:status,:notes,:parsing_strategy,:normalization_quality_threshold,:max_scrolls,:research_enabled,:builtin,:updated_at)
                """,
                [
                    {"id": "akshare", "name": "AkShare", "type": "结构化行情", "url": "", "collection_mode": "akshare", "status": "online", "notes": "本地模块待首次调用", "parsing_strategy": "fixed", "normalization_quality_threshold": 60, "max_scrolls": 1, "research_enabled": 1, "builtin": 1, "updated_at": now()},
                    {"id": "zsxq", "name": "知识星球", "type": "登录态信息差", "url": "https://wx.zsxq.com", "collection_mode": "playwright", "status": "pending", "notes": "等待浏览器登录配置", "parsing_strategy": "hybrid", "normalization_quality_threshold": 60, "max_scrolls": 12, "research_enabled": 0, "builtin": 0, "updated_at": now()},
                    {"id": "web-rumors", "name": "网页小作文渠道", "type": "浏览器采集", "url": "", "collection_mode": "playwright", "status": "pending", "notes": "等待渠道规则配置", "parsing_strategy": "hybrid", "normalization_quality_threshold": 60, "max_scrolls": 8, "research_enabled": 0, "builtin": 0, "updated_at": now()},
                ],
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO channels(
              id,name,type,url,collection_mode,status,notes,parsing_strategy,
              normalization_quality_threshold,max_scrolls,research_enabled,builtin,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "industry-news",
                "产业趋势公开资讯",
                "行业资讯、公司资料与公告补证",
                "",
                "industry_news",
                "pending",
                "东方财富行业排名与资讯、个股资料和巨潮公告；按时间窗采集并保留证据来源",
                "fixed",
                70,
                1,
                1,
                1,
                now(),
            ),
        )
        conn.execute(
            """
            UPDATE channels
            SET name=?,type=?,collection_mode='industry_news',notes=?,
                parsing_strategy='fixed',normalization_quality_threshold=70,max_scrolls=1,
                research_enabled=1,builtin=1
            WHERE id='industry-news'
            """,
            (
                "产业趋势公开资讯",
                "行业资讯、公司资料与公告补证",
                "东方财富行业排名与资讯、个股资料和巨潮公告；按时间窗采集并保留证据来源",
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO channels(
              id,name,type,url,collection_mode,status,notes,parsing_strategy,
              normalization_quality_threshold,max_scrolls,research_enabled,builtin,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "wechat-mp-rss",
                "微信公众号（WeRSS）",
                "外部 RSS 聚合",
                "http://127.0.0.1:8001",
                "wechat_rss",
                "pending",
                "微信扫码登录后自动同步已订阅公众号，并按严格时间窗读取文章快照",
                "fixed",
                85,
                1,
                1,
                1,
                now(),
            ),
        )
        conn.execute(
            """
            UPDATE channels
            SET name='微信公众号（WeRSS）',type='外部 RSS 聚合',collection_mode='wechat_rss',
                notes='微信扫码登录后自动同步已订阅公众号，并按严格时间窗读取文章快照',
                parsing_strategy='fixed',normalization_quality_threshold=85,max_scrolls=1,
                research_enabled=1,builtin=1
            WHERE id='wechat-mp-rss'
            """
        )
        provider_count = conn.execute("SELECT COUNT(*) AS count FROM model_providers").fetchone()["count"]
        legacy_provider = conn.execute("SELECT value FROM settings WHERE key='provider'").fetchone()
        if not provider_count and legacy_provider:
            legacy = json.loads(legacy_provider["value"])
            conn.execute(
                """
                INSERT INTO model_providers(
                  id,name,base_url,model,protocol,encrypted_api_key,extra_body,enabled,is_default,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid4().hex[:10],
                    legacy.get("name", "DeepSeek"),
                    legacy.get("base_url", "https://api.deepseek.com"),
                    legacy.get("model", "deepseek-chat"),
                    "openai_chat_completions",
                    legacy.get("encrypted_api_key", ""),
                    "{}",
                    1,
                    1,
                    "untested",
                    now(),
                    now(),
                ),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at)
            SELECT channel_id,'',last_success_at FROM source_collection_watermarks
            """
        )
        for channel_id, display_name in CANONICAL_CHANNEL_NAMES.items():
            conn.execute("UPDATE channels SET name=? WHERE id=?", (display_name, channel_id))
        conn.execute(
            "UPDATE channels SET name=?,type=? WHERE id='akshare'",
            ("A股市场数据（AkShare / BaoStock / TuShare）", "结构化市场数据聚合"),
        )
        conn.execute("UPDATE channels SET name=? WHERE url=?", ("TG 小作文频道", "https://t.me/s/hejrb2333"))
        ensure_migration_ledger(conn)


init_db()
log_event(
    logger,
    "INFO",
    "application.initialized",
    database=str(DB_PATH),
    version=app.version,
    schema_revision=LATEST_SCHEMA_REVISION,
)

TOOLS = tool_catalog()

MASKED_API_KEY = MASKED_SECRET


def decode_provider(row: sqlite3.Row | dict) -> dict:
    provider = dict(row)
    provider["extra_body"] = json.loads(provider.get("extra_body") or "{}")
    provider["enabled"] = bool(provider.get("enabled"))
    provider["is_default"] = bool(provider.get("is_default"))
    return provider


def provider_rows() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM model_providers ORDER BY is_default DESC,created_at").fetchall()
    return [decode_provider(row) for row in rows]


def provider_row(provider_id: str = "") -> dict | None:
    with db() as conn:
        if provider_id:
            row = conn.execute("SELECT * FROM model_providers WHERE id=?", (provider_id,)).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM model_providers
                ORDER BY CASE WHEN is_default=1 AND enabled=1 THEN 0 WHEN enabled=1 THEN 1 ELSE 2 END,created_at
                LIMIT 1
                """
            ).fetchone()
    return decode_provider(row) if row else None


def provider_public(provider: dict | None) -> dict | None:
    if not provider:
        return None
    public = {key: value for key, value in provider.items() if key != "encrypted_api_key"}
    public["configured"] = bool(provider.get("encrypted_api_key"))
    public["api_key"] = MASKED_API_KEY if public["configured"] else ""
    return public


def providers_public() -> list[dict]:
    return [provider_public(provider) for provider in provider_rows()]


def codex_policy() -> dict:
    if not CODEX_POLICY_PATH.exists():
        return {"configured": False}
    with CODEX_POLICY_PATH.open("rb") as handle:
        policy = tomllib.load(handle)
    defaults = policy.get("model_defaults", {})
    plugins = policy.get("plugins", {})
    windows = policy.get("windows", {})
    tooling = policy.get("tooling", {})
    return {
        "configured": True,
        "model": defaults.get("model"),
        "reasoning_effort": defaults.get("reasoning_effort"),
        "browser_enabled": plugins.get("browser_enabled", False),
        "sandbox_preference": windows.get("sandbox_preference"),
        "python_toolbox": tooling.get("python_toolbox"),
        "market_data_priority": tooling.get("market_data_priority", []),
    }


def research_red_lines() -> dict:
    if not RED_LINES_PATH.exists():
        raise RuntimeError("Missing required research red-line policy")
    with RED_LINES_PATH.open("rb") as handle:
        policy = tomllib.load(handle)
    analysis = policy["analysis"]
    if analysis.get("local_analysis_enabled") or not analysis.get("aggregation_only") or not analysis.get("ai_analysis_required"):
        raise RuntimeError("Invalid red-line policy: local analysis must remain disabled")
    report_output = policy.get("report_output", {})
    if (
        report_output.get("format") != "html"
        or report_output.get("markdown_allowed")
        or not report_output.get("require_complete_document")
    ):
        raise RuntimeError("Invalid red-line policy: reports must remain complete HTML documents and Markdown must remain disabled")
    report_focus = policy.get("report_focus", {})
    forbidden_focus = set(report_focus.get("forbidden", []))
    if (
        report_focus.get("max_lookback_days") != 30
        or forbidden_focus != {"technical_analysis", "trading_strategy", "buy_sell_points"}
    ):
        raise RuntimeError("Invalid red-line policy: report focus must exclude technical analysis, trading strategies and buy/sell points")
    normalization = policy.get("collection_normalization", {})
    allowed_operations = set(normalization.get("allowed_operations", []))
    if (
        not normalization.get("ai_assistance_allowed")
        or not normalization.get("analysis_forbidden")
        or not normalization.get("preserve_raw_snapshot")
        or allowed_operations != {"extract", "split", "deduplicate", "label", "quality_score"}
    ):
        raise RuntimeError("Invalid red-line policy: AI normalization must remain extraction-only and raw snapshots must be preserved")
    isolation = policy.get("workflow_isolation", {})
    if not all(
        isolation.get(key)
        for key in (
            "source_reports_use_general_snapshots_only",
            "source_reports_forbid_research_task_context",
            "source_reports_forbid_stock_skill_injection",
            "source_reports_selected_channels_only",
            "research_tasks_may_read_general_snapshots",
            "research_tasks_may_read_all_source_snapshots",
            "research_tasks_may_read_source_reports",
            "research_tasks_may_collect_scoped_evidence",
            "research_tasks_refresh_stale_general_sources",
        )
    ):
        raise RuntimeError("Invalid red-line policy: source reports and stock research workflows must remain isolated")
    return policy


def html_report_shape(report: str) -> dict[str, object]:
    text = (report or "").strip()
    return {
        "chars": len(text),
        "starts_with_fence": text.startswith("```"),
        "has_html": bool(re.search(r"<html(?:\s|>)", text, flags=re.IGNORECASE)),
        "has_head": bool(re.search(r"<head(?:\s|>)", text, flags=re.IGNORECASE)),
        "has_body": bool(re.search(r"<body(?:\s|>)", text, flags=re.IGNORECASE)),
        "closes_body": bool(re.search(r"</body\s*>", text, flags=re.IGNORECASE)),
        "closes_html": bool(re.search(r"</html\s*>", text, flags=re.IGNORECASE)),
    }


def is_complete_html_report(report: str) -> bool:
    shape = html_report_shape(report)
    return bool(
        not shape["starts_with_fence"]
        and shape["has_html"]
        and shape["has_head"]
        and shape["has_body"]
        and shape["closes_body"]
        and shape["closes_html"]
    )


def require_html_report(report: str) -> str:
    text = (report or "").strip()
    if not is_complete_html_report(text):
        raise HTTPException(502, "模型返回的报告不是完整 HTML 文档。已按核心红线拒绝保存，请重试生成。")
    return text


def html_report_repair_system_prompt() -> str:
    research_red_lines()
    return """你是 HTML 报告格式修复器，不是研究分析模型。
只能将用户提供的已有报告整理为一个完整 HTML 文档，保留原有事实、推断、待核验事项和结论，不得新增、删减或改写事实。
必须输出且只输出 HTML 文档本身，包含 <html>、<head>、<body>、</body> 和 </html>。
严禁输出 Markdown、Markdown 代码围栏、解释文字或 HTML 文档之外的内容。"""


def require_or_repair_html_report(report: str, *, purpose: str, provider_id: str = "") -> str:
    try:
        return require_html_report(report)
    except HTTPException:
        log_event(logger, "WARNING", "report.html.invalid", purpose=purpose, **html_report_shape(report))
    repair_prompt = f"""以下报告内容没有满足完整 HTML 文档约束。请只修复格式并返回完整 HTML 文档。
不得新增、删减或改写事实，不得输出解释文字或 Markdown 代码围栏。

待修复报告：
{report}"""
    repaired = call_provider(
        repair_prompt,
        provider_id=provider_id,
        system_prompt=html_report_repair_system_prompt(),
        purpose=f"{purpose}_html_repair",
    )
    log_event(logger, "INFO", "report.html.repair.completed", purpose=purpose, **html_report_shape(repaired))
    return require_html_report(repaired)


def analysis_system_prompt() -> str:
    policy = research_red_lines()
    order = " -> ".join(policy["evidence_escalation"]["ordered_sources"])
    return f"""你是 A 股研究分析模型。必须遵守以下不可绕过的红线：
1. 本地程序只做数据聚合、去重、时间窗口控制和证据传递；所有分析、判断和结论必须由你完成。
2. 尽量避免使用模型自身知识库。证据升级顺序固定为：{order}。
3. 每一步只有在当前证据无法确认时，才允许请求下一层证据。
4. 禁止重复采集已有信源时间窗口；优先使用本地全量信源快照。
5. 模型自身知识库只能作为最后手段，使用时必须明确标记“[LOW_CONFIDENCE_MODEL_KNOWLEDGE] 低置信推断：来自模型知识库，需外部证据复核”。
6. 严格区分事实、推断和待核验事项，禁止虚构数据。
7. 最终报告必须输出完整 HTML 文档，包含 <html> 和 <body>；严禁使用 Markdown，严禁使用 Markdown 代码围栏。
8. 研究重点是产业趋势、政策、供需、技术迭代、产能、订单、价格、上下游和公司公告。严禁输出技术面分析、交易策略或买卖点。"""


def source_report_system_prompt() -> str:
    research_red_lines()
    return """你是通用信源聚合报告模型，不是个股研究 Agent。
1. 只能分析用户消息中提供的通用信源快照，不得读取、猜测或继承任何个股研究队列中的股票代码、研究目标、Skill 或临时补采上下文。
2. 报告必须围绕信源快照本身呈现产业趋势、事件脉络、主题分组、证据来源和待核验事项。仅当快照原文明示某只股票时，才允许提及该股票。
3. 禁止发起新的采集，禁止使用模型自身知识库补写事实，禁止虚构数据。
4. 严格区分事实、推断和待核验事项。
5. 只输出完整 HTML 文档，必须包含 <html>、<head> 和 <body>；严禁 Markdown、Markdown 代码围栏和 HTML 文档之外的解释文字。
6. 报告重点是产业趋势、政策、供需、技术迭代、产能、订单、价格、上下游和公司公告。严禁输出技术面分析、交易策略或买卖点。
7. 每项事实和事件都要尽量标注具体信源。对于微信公众号文章，当输入提供 source_account 或 author 时，必须标注具体公众号名称，不得退化为笼统的“微信公众号”。"""


def normalization_system_prompt() -> str:
    research_red_lines()
    return """你是信源数据整理器，不是研究分析模型。你只能对已采集的原始快照执行以下操作：
1. 提取原文中明确存在的字段。
2. 将混合页面拆分为独立内容条目。
3. 根据稳定键辅助去重。
4. 标注作者、发布时间、标题、来源 URL、附件和元数据。
5. 对提取完整度给出 0-100 的质量评分。
严禁生成摘要、观点、评级、推断、投资结论或补写原文中不存在的事实。
严禁请求或执行新的网络访问。只允许使用用户消息中提供的原始快照。
必须只输出 JSON 对象，不要输出 Markdown 代码围栏。"""


model_gateway = ModelGateway()


def provider_runtime_config(provider: dict) -> ProviderRuntimeConfig:
    return ProviderRuntimeConfig(
        id=provider["id"],
        base_url=provider["base_url"],
        model=provider["model"],
        protocol=provider["protocol"],
        api_key=cipher().decrypt(provider["encrypted_api_key"].encode()).decode(),
        extra_body=provider.get("extra_body") or {},
    )


def record_model_call(provider_id: str, purpose: str, status: str, latency_ms: int, result: GatewayResult | None = None, error: str = "") -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO model_call_logs(id,provider_id,purpose,status,latency_ms,input_tokens,output_tokens,request_count,error,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                uuid4().hex[:12],
                provider_id,
                purpose,
                status,
                latency_ms,
                result.input_tokens if result else 0,
                result.output_tokens if result else 0,
                result.requests if result else 0,
                error[:1200],
                now(),
            ),
        )
    log_event(
        logger,
        "ERROR" if status == "failed" else "INFO",
        "model.call.recorded",
        provider_id=provider_id,
        purpose=purpose,
        status=status,
        latency_ms=latency_ms,
        input_tokens=result.input_tokens if result else 0,
        output_tokens=result.output_tokens if result else 0,
        request_count=result.requests if result else 0,
        error=error,
    )


def call_provider(prompt: str, provider_id: str = "", system_prompt: str = "", purpose: str = "text") -> str:
    research_red_lines()
    provider = provider_row(provider_id)
    if not provider or not provider.get("encrypted_api_key"):
        raise HTTPException(409, "请先在设置中配置模型供应商")
    if not provider["enabled"]:
        raise HTTPException(409, "模型通道已停用")
    config = provider_runtime_config(provider)
    log_event(logger, "INFO", "model.call.started", provider_id=provider["id"], purpose=purpose, model=provider["model"], protocol=provider["protocol"], prompt_chars=len(prompt))
    started_at = time.perf_counter()
    try:
        result = model_gateway.run_text(config, prompt, instructions=system_prompt or analysis_system_prompt())
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        detail = str(exc).replace(config.api_key, "[REDACTED]")
        record_model_call(provider["id"], purpose, "failed", latency_ms, error=f"{type(exc).__name__}: {detail}")
        raise HTTPException(502, f"模型供应商请求失败: {type(exc).__name__}: {detail}") from exc
    latency_ms = int((time.perf_counter() - started_at) * 1000)
    record_model_call(provider["id"], purpose, "completed", latency_ms, result)
    return str(result.output)


def call_provider_structured(prompt: str, output_type: type[BaseModel], provider_id: str = "", system_prompt: str = "", purpose: str = "structured") -> BaseModel:
    research_red_lines()
    provider = provider_row(provider_id)
    if not provider or not provider.get("encrypted_api_key"):
        raise HTTPException(409, "请先在设置中配置模型供应商")
    if not provider["enabled"]:
        raise HTTPException(409, "模型通道已停用")
    config = provider_runtime_config(provider)
    log_event(
        logger,
        "INFO",
        "model.call.started",
        provider_id=provider["id"],
        purpose=purpose,
        model=provider["model"],
        protocol=provider["protocol"],
        prompt_chars=len(prompt),
        output_type=output_type.__name__,
    )
    started_at = time.perf_counter()
    try:
        result = model_gateway.run_structured(
            config,
            prompt,
            instructions=system_prompt or analysis_system_prompt(),
            output_type=output_type,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        detail = str(exc).replace(config.api_key, "[REDACTED]")
        record_model_call(provider["id"], purpose, "failed", latency_ms, error=f"{type(exc).__name__}: {detail}")
        raise HTTPException(502, f"模型供应商请求失败: {type(exc).__name__}: {detail}") from exc
    latency_ms = int((time.perf_counter() - started_at) * 1000)
    record_model_call(provider["id"], purpose, "completed", latency_ms, result)
    return result.output


def clamp_quality_score(value: object, default: int = 0) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return default


def stable_item_key(channel_id: str, source_url: str, occurred_at: str, title: str, content: str) -> str:
    raw = "\n".join((channel_id, source_url, occurred_at, title, content))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalized_occurred_at(value: object, fallback: str) -> str:
    occurred_at = str(value or "").strip()
    if not occurred_at:
        return fallback
    try:
        datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return occurred_at


def fixed_normalized_items(snapshot: sqlite3.Row, mode: str = "fixed") -> tuple[list[dict], str]:
    content = str(snapshot["content"] or "").strip()
    if not content:
        return [], ""
    source_url = str(snapshot["source_url"] or "")
    occurred_at = str(snapshot["occurred_at"] or snapshot["collected_at"])
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and payload.get("platform") in {"eastmoney_public", "cninfo_public", "public_industry_news"}:
        title = str(payload.get("title") or "").strip()
        item_content = str(payload.get("content") or title).strip()
        item_source_url = str(payload.get("source_url") or source_url).strip()
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = {
            **metadata,
            "platform": payload.get("platform"),
            "category": payload.get("category"),
            "source": payload.get("source"),
            "query": payload.get("query", ""),
            "collection_window": payload.get("collection_window", {}),
        }
        return [
            {
                "item_key": stable_item_key(snapshot["channel_id"], item_source_url, occurred_at, title, item_content),
                "occurred_at": occurred_at,
                "author": str(payload.get("source") or "")[:255],
                "title": title[:500],
                "content": item_content,
                "source_url": item_source_url,
                "attachments": [],
                "metadata": metadata,
                "quality_score": 72 if payload.get("category") == "collector_diagnostics" else 88,
                "normalization_mode": mode,
            }
        ], ""
    if isinstance(payload, dict) and payload.get("platform") == "mx_authorized_request_replay":
        room = payload.get("room") if isinstance(payload.get("room"), dict) else {}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        texts = [
            str(part.get("msg") or "").strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("msg") or "").strip()
        ]
        attachments = [
            str(part.get("url") or "").strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("url") or "").strip()
        ]
        item_content = "\n".join(texts).strip() or json.dumps(parts, ensure_ascii=False)
        title = str(room.get("title") or "MX channel message").strip()
        return [
            {
                "item_key": stable_item_key(snapshot["channel_id"], source_url, occurred_at, title, item_content),
                "occurred_at": occurred_at,
                "author": str(message.get("author") or "")[:255],
                "title": title[:500],
                "content": item_content,
                "source_url": source_url,
                "attachments": attachments,
                "metadata": {"platform": payload["platform"], "room": room, "message_id": message.get("id", "")},
                "quality_score": 90,
                "normalization_mode": mode,
            }
        ], ""
    if isinstance(payload, dict) and payload.get("platform") == "telegram_public_preview":
        item_content = str(payload.get("text") or "").strip()
        links = payload.get("links") if isinstance(payload.get("links"), list) else []
        media = payload.get("media") if isinstance(payload.get("media"), list) else []
        attachments = [str(item) for item in [*links, *media] if str(item).strip()]
        return [
            {
                "item_key": stable_item_key(snapshot["channel_id"], source_url, occurred_at, "", item_content),
                "occurred_at": occurred_at,
                "author": str(payload.get("channel") or "")[:255],
                "title": str(payload.get("post_id") or "")[:500],
                "content": item_content,
                "source_url": source_url,
                "attachments": attachments,
                "metadata": {"platform": payload["platform"], "post_id": payload.get("post_id", "")},
                "quality_score": 88,
                "normalization_mode": mode,
            }
        ], ""
    if isinstance(payload, dict) and payload.get("platform") == "werss_external_rss":
        article = payload.get("article") if isinstance(payload.get("article"), dict) else {}
        source_account = payload.get("source_account") if isinstance(payload.get("source_account"), dict) else {}
        account_id = str(source_account.get("id") or article.get("source_account_id") or "").strip()
        account_name = str(source_account.get("name") or article.get("source_account_name") or "").strip()
        title = str(article.get("title") or "").strip()
        item_content = str(article.get("content") or article.get("description") or title).strip()
        item_source_url = str(article.get("link") or source_url).strip()
        if not item_content:
            return [], "WeRSS article content is empty"
        return [
            {
                "item_key": stable_item_key(snapshot["channel_id"], item_source_url, occurred_at, title, item_content),
                "occurred_at": occurred_at,
                "author": str(account_name or article.get("author") or "")[:255],
                "title": title[:500],
                "content": item_content,
                "source_url": item_source_url,
                "attachments": [],
                "metadata": {
                    "platform": payload["platform"],
                    "adapter": payload.get("adapter", ""),
                    "feed_id": payload.get("feed_id", ""),
                    "source_account": {"id": account_id, "name": account_name},
                    "article_id": article.get("id", ""),
                    "query": payload.get("query", ""),
                    "collection_window": payload.get("collection_window", {}),
                },
                "quality_score": 90,
                "normalization_mode": mode,
            }
        ], ""
    if isinstance(payload, dict) and payload.get("adapter") == "market_data_aggregate":
        return [
            {
                "item_key": stable_item_key(snapshot["channel_id"], source_url, occurred_at, "Market data aggregate", content),
                "occurred_at": occurred_at,
                "author": "local_market_data",
                "title": "Market data aggregate",
                "content": content,
                "source_url": source_url,
                "attachments": [],
                "metadata": {
                    "adapter": payload["adapter"],
                    "query": payload.get("query", ""),
                    "component_diagnostics": payload.get("component_diagnostics", {}),
                },
                "quality_score": 86 if payload.get("components") else 60,
                "normalization_mode": mode,
            }
        ], ""
    if isinstance(payload, dict) and payload.get("platform") in {"zsxq", "web"}:
        item_content = str(payload.get("visible_text") or "").strip()
        if item_content:
            return [
                {
                    "item_key": stable_item_key(snapshot["channel_id"], source_url, occurred_at, "", item_content),
                    "occurred_at": occurred_at,
                    "author": "",
                    "title": f"{payload['platform']} page snapshot",
                    "content": item_content,
                    "source_url": source_url,
                    "attachments": [],
                    "metadata": {
                        "platform": payload["platform"],
                        "group_id": payload.get("group_id", ""),
                        "captured_at": payload.get("captured_at", ""),
                    },
                    "quality_score": 68,
                    "normalization_mode": mode,
                }
            ], ""
    return [
        {
            "item_key": stable_item_key(snapshot["channel_id"], source_url, occurred_at, "", content),
            "occurred_at": occurred_at,
            "author": "",
            "title": "",
            "content": content,
            "source_url": source_url,
            "attachments": [],
            "metadata": {"raw_snapshot": True},
            "quality_score": 35,
            "normalization_mode": mode,
        }
    ], ""


def ai_normalized_items(snapshot: sqlite3.Row, channel: sqlite3.Row, mode: str) -> tuple[list[dict], str]:
    contract = {
        "items": [
            {
                "item_key": "stable ID if explicitly present, otherwise empty",
                "occurred_at": "explicit ISO-8601 timestamp, otherwise empty",
                "author": "explicit author, otherwise empty",
                "title": "explicit title, otherwise empty",
                "content": "verbatim source content, never summarize or rewrite",
                "source_url": "explicit item URL, otherwise empty",
                "attachments": ["explicit attachment URL or name"],
                "metadata": {"explicit_field_name": "explicit value"},
                "quality_score": 0,
            }
        ],
        "quality_score": 0,
        "notes": "extraction quality issues only, never analysis",
    }
    prompt = (
        f"Normalize one raw snapshot from channel {channel['channel_name']}.\n"
        "Split mixed page content into independent items, at most 100 items. Preserve content verbatim. "
        "Never summarize, infer, or analyze. Leave fields empty when they are not explicit in the snapshot. "
        "Leave item_key empty when the page has no stable explicit ID; the local program will generate it.\n"
        f"Return exactly one JSON object matching this contract: {json.dumps(contract, ensure_ascii=False)}\n\n"
        f"Snapshot URL: {snapshot['source_url']}\n"
        f"Collected at: {snapshot['collected_at']}\n"
        f"Raw snapshot content:\n{str(snapshot['content'] or '')[:120_000]}"
    )
    payload = call_provider_structured(
        prompt,
        NormalizationResult,
        system_prompt=normalization_system_prompt(),
        purpose="normalization",
    )
    raw_items = payload.items
    top_score = clamp_quality_score(payload.quality_score, 0)
    notes = str(payload.notes or "")[:1_000]
    items: list[dict] = []
    for raw_item in raw_items[:100]:
        content = str(raw_item.content or "").strip()
        if not content:
            continue
        occurred_at = normalized_occurred_at(raw_item.occurred_at, snapshot["occurred_at"] or snapshot["collected_at"])
        source_url = str(raw_item.source_url or snapshot["source_url"] or "").strip()
        title = str(raw_item.title or "").strip()
        item_key = str(raw_item.item_key or "").strip()
        if not item_key:
            item_key = stable_item_key(snapshot["channel_id"], source_url, occurred_at, title, content)
        items.append(
            {
                "item_key": item_key[:255],
                "occurred_at": occurred_at,
                "author": str(raw_item.author or "")[:255],
                "title": title[:500],
                "content": content,
                "source_url": source_url[:2_000],
                "attachments": raw_item.attachments,
                "metadata": raw_item.metadata,
                "quality_score": clamp_quality_score(raw_item.quality_score, top_score),
                "normalization_mode": mode,
            }
        )
    if not items:
        raise ValueError("Model did not extract any content items")
    return items, notes


def normalize_snapshot_record(snapshot_id: str, force: bool = False) -> dict:
    log_event(logger, "INFO", "normalization.started", snapshot_id=snapshot_id, force=force)
    with db() as conn:
        snapshot = conn.execute(
            """
            SELECT s.*, c.name AS channel_name, c.parsing_strategy, c.normalization_quality_threshold
            FROM source_snapshots s
            JOIN channels c ON c.id=s.channel_id
            WHERE s.id=?
            """,
            (snapshot_id,),
        ).fetchone()
        if not snapshot:
            raise HTTPException(404, "Snapshot not found")
        if snapshot["normalization_status"] in {"complete", "low_quality", "fallback"} and not force:
            return {
                "snapshot_id": snapshot_id,
                "status": snapshot["normalization_status"],
                "stored_item_count": snapshot["normalized_item_count"],
                "deduplicated": True,
            }
    strategy = snapshot["parsing_strategy"] or "fixed"
    status = "complete"
    error = ""
    try:
        fixed_items, fixed_notes = fixed_normalized_items(snapshot)
        fixed_parser_matched = bool(fixed_items) and not any(item.get("metadata", {}).get("raw_snapshot") for item in fixed_items)
        if strategy == "fixed" or fixed_parser_matched:
            items, notes = fixed_items, fixed_notes
        else:
            items, notes = ai_normalized_items(snapshot, snapshot, f"{strategy}_ai")
    except Exception as exc:
        if strategy != "hybrid":
            with db() as conn:
                conn.execute(
                    """
                    UPDATE source_snapshots
                    SET normalization_status='failed', normalized_at=?, normalization_error=?, normalized_item_count=0
                    WHERE id=?
                    """,
                    (now(), str(exc)[:2_000], snapshot_id),
                )
            return {"snapshot_id": snapshot_id, "status": "failed", "stored_item_count": 0, "error": str(exc)}
        items, notes = fixed_normalized_items(snapshot, "hybrid_fallback")
        status = "fallback"
        error = str(exc)[:2_000]
    created_at = now()
    with db() as conn:
        if force:
            conn.execute("DELETE FROM normalized_source_items WHERE snapshot_id=?", (snapshot_id,))
        for item in items:
            conn.execute(
                """
                INSERT OR IGNORE INTO normalized_source_items(
                  id,snapshot_id,channel_id,item_key,occurred_at,author,title,content,source_url,
                  attachments,metadata,quality_score,normalization_mode,created_at,scope_type,scope_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid4().hex,
                    snapshot_id,
                    snapshot["channel_id"],
                    item["item_key"],
                    item["occurred_at"],
                    item["author"],
                    item["title"],
                    item["content"],
                    item["source_url"],
                    json.dumps(item["attachments"], ensure_ascii=False),
                    json.dumps(item["metadata"], ensure_ascii=False),
                    item["quality_score"],
                    item["normalization_mode"],
                    created_at,
                    snapshot["scope_type"],
                    snapshot["scope_key"],
                ),
            )
        stored_count = conn.execute(
            "SELECT COUNT(*) AS count FROM normalized_source_items WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()["count"]
        average_quality = conn.execute(
            "SELECT AVG(quality_score) AS score FROM normalized_source_items WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()["score"]
        if status == "complete" and average_quality is not None and average_quality < snapshot["normalization_quality_threshold"]:
            status = "low_quality"
        normalization_error = "\n".join(part for part in (error, notes[:1_000]) if part)
        conn.execute(
            """
            UPDATE source_snapshots
            SET normalization_status=?, normalized_at=?, normalization_error=?, normalized_item_count=?
            WHERE id=?
            """,
            (status, created_at, normalization_error, stored_count, snapshot_id),
        )
    result = {
        "snapshot_id": snapshot_id,
        "status": status,
        "parsed_item_count": len(items),
        "stored_item_count": stored_count,
        "average_quality": average_quality,
    }
    log_event(logger, "WARNING" if status in {"failed", "low_quality"} else "INFO", "normalization.completed", **result)
    return result


@app.get("/health/live")
def health_live() -> dict:
    return {"status": "ok", "service": "alphadesk-local-api", "version": app.version, "time": now()}


@app.get("/health")
def health() -> dict:
    return health_live()


@app.get("/health/ready")
def health_ready(response: Response) -> dict:
    readiness = summarize_readiness(
        [
            database_check(DB_PATH),
            callable_check("research_red_lines", research_red_lines, "Research red-line policy is valid."),
            directory_check("skills_directory", SKILLS_DIR),
            worker_check(collection_worker),
            telemetry_check(TELEMETRY_STATUS),
        ]
    )
    if readiness["status"] != "ready":
        response.status_code = 503
    return {
        **readiness,
        "service": "alphadesk-local-api",
        "version": app.version,
        "time": now(),
    }


@app.post("/api/diagnostics/frontend-logs", status_code=202)
def receive_frontend_logs(payload: FrontendLogBatchInput) -> dict:
    for entry in payload.entries:
        log_event(
            frontend_logger,
            entry.level,
            entry.event,
            client_timestamp=entry.timestamp,
            message=entry.message,
            **entry.context,
        )
    return {"status": "accepted", "count": len(payload.entries)}


@app.get("/api/diagnostics/logs")
def diagnostics_logs(limit: int = 200, level: str = "", component: str = "", search: str = "") -> dict:
    return {
        "logs": recent_logs(limit=limit, level=level, component=component, search=search),
        "config": diagnostics_config(),
    }


@app.get("/api/diagnostics/logs/export")
def export_diagnostics_logs():
    filename = f"alphadesk-diagnostics-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.zip"
    log_event(logger, "INFO", "diagnostics.logs.exported", filename=filename)
    return StreamingResponse(
        export_log_bundle(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def channel_names_for_ids(channel_ids: list[str], names: dict[str, str] | None = None) -> list[str]:
    if names is None:
        with db() as conn:
            names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM channels")}
    return [names.get(channel_id, CANONICAL_CHANNEL_NAMES.get(channel_id, channel_id)) for channel_id in channel_ids]


@app.get("/api/dashboard")
def dashboard() -> dict:
    with db() as conn:
        tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 8")]
        source_jobs = source_job_list_response(
            conn,
            [dict(row) for row in conn.execute("SELECT * FROM source_collection_jobs ORDER BY created_at DESC LIMIT 80")],
        )
        channels = [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY builtin DESC, updated_at")]
        schema = migration_status(conn)
    for channel in channels:
        channel["group_ids"] = json.loads(channel.get("group_ids") or "[]")
        channel["profile_exists"] = browser_profile(channel["id"]).exists()
        if channel["id"] == "akshare":
            channel["market_data_config"] = market_data_config_public()
        if channel["id"] == "wechat-mp-rss":
            channel["wechat_rss_config"] = wechat_rss_config_public()
    skills = [
        {"name": item.name, "path": str(item.relative_to(ROOT)), "status": "loaded"}
        for item in SKILLS_DIR.iterdir()
        if item.is_dir()
    ] if SKILLS_DIR.exists() else []
    return {
        "provider": provider_public(provider_row()),
        "providers": providers_public(),
        "codex_policy": codex_policy(),
        "research_red_lines": research_red_lines(),
        "tools": TOOLS,
        "source_catalog": source_catalog(),
        "schema": schema,
        "channels": channels,
        "skills": skills,
        "tasks": tasks,
        "source_jobs": source_jobs,
    }


def inventory_summary(conn: sqlite3.Connection) -> dict:
    return {
        "snapshot_count": conn.execute("SELECT COUNT(*) AS count FROM source_snapshots").fetchone()["count"],
        "normalized_item_count": conn.execute("SELECT COUNT(*) AS count FROM normalized_source_items").fetchone()["count"],
        "source_report_count": conn.execute(
            "SELECT COUNT(*) AS count FROM source_collection_jobs WHERE report IS NOT NULL"
        ).fetchone()["count"],
        "research_report_count": conn.execute("SELECT COUNT(*) AS count FROM tasks WHERE report IS NOT NULL").fetchone()["count"],
    }


def ensure_inventory_cleanup_idle(conn: sqlite3.Connection) -> None:
    active_source_job = conn.execute(
        """
        SELECT id FROM source_collection_jobs
        WHERE status IN ('queued','running','generating','generating_report')
        LIMIT 1
        """
    ).fetchone()
    active_research_task = conn.execute(
        "SELECT id FROM tasks WHERE status IN ('analyzing','evidence_queued') LIMIT 1"
    ).fetchone()
    if active_source_job or active_research_task:
        raise HTTPException(409, "存在执行中的采集、报告或研究任务，请等待任务完成后再清理库存")


@app.get("/api/audit")
def audit() -> dict:
    with db() as conn:
        jobs = source_job_list_response(
            conn,
            [dict(row) for row in conn.execute("SELECT * FROM source_collection_jobs ORDER BY created_at DESC LIMIT 80")],
        )
        watermarks = [
            dict(row)
            for row in conn.execute(
                """
                SELECT w.channel_id,w.scope_key,c.name,w.last_success_at
                FROM source_collection_watermarks_v2 w
                LEFT JOIN channels c ON c.id=w.channel_id
                ORDER BY w.last_success_at DESC
                """
            )
        ]
        snapshots = [
            dict(row)
            for row in conn.execute(
                """
                SELECT s.channel_id,c.name,COUNT(*) AS snapshot_count,
                       SUM(s.normalized_item_count) AS normalized_item_count,
                       MAX(s.collected_at) AS last_collected_at
                FROM source_snapshots s
                LEFT JOIN channels c ON c.id=s.channel_id
                GROUP BY s.channel_id,c.name
                ORDER BY last_collected_at DESC
                """
            )
        ]
        normalized = [
            dict(row)
            for row in conn.execute(
                """
                SELECT n.channel_id,c.name,COUNT(*) AS item_count,
                       ROUND(AVG(n.quality_score), 1) AS average_quality,
                       MAX(n.created_at) AS last_normalized_at
                FROM normalized_source_items n
                LEFT JOIN channels c ON c.id=n.channel_id
                GROUP BY n.channel_id,c.name
                ORDER BY last_normalized_at DESC
                """
            )
        ]
        events = [dict(row) for row in conn.execute("SELECT * FROM agent_events ORDER BY created_at DESC LIMIT 80")]
        inventory = inventory_summary(conn)
    return {"jobs": jobs, "watermarks": watermarks, "snapshots": snapshots, "normalized": normalized, "events": events, "inventory": inventory}


@app.delete("/api/audit/inventory/{scope}")
def clear_audit_inventory(scope: Literal["snapshots", "reports", "all"]) -> dict:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_inventory_cleanup_idle(conn)
        before = inventory_summary(conn)
        if scope in ("snapshots", "all"):
            conn.execute("DELETE FROM source_job_snapshots")
            conn.execute("DELETE FROM normalized_source_items")
            conn.execute("DELETE FROM source_snapshots")
            conn.execute("DELETE FROM source_collection_watermarks")
            conn.execute("DELETE FROM source_collection_watermarks_v2")
            conn.execute(
                """
                UPDATE source_collection_jobs
                SET snapshot_count=0,
                    status=CASE
                      WHEN action='collect' AND snapshot_count>0 THEN 'snapshot_deleted'
                      WHEN action='collect_report' AND report IS NULL THEN 'snapshot_deleted'
                      ELSE status
                    END
                WHERE snapshot_count>0
                """
            )
            conn.execute(
                """
                UPDATE tasks
                SET agent_state='{}',agent_error='',
                    status=CASE WHEN report IS NULL THEN 'queued' ELSE status END
                """
            )
        if scope in ("reports", "all"):
            conn.execute(
                """
                UPDATE source_collection_jobs
                SET report=NULL,report_anchor='',
                    status=CASE WHEN report IS NOT NULL THEN 'report_deleted' ELSE status END
                WHERE report IS NOT NULL
                """
            )
            conn.execute(
                """
                UPDATE tasks
                SET report=NULL,report_anchor='',status='queued',agent_state='{}',agent_error=''
                WHERE report IS NOT NULL
                """
            )
        after = inventory_summary(conn)
    result = {"scope": scope, "deleted": {key: before[key] - after[key] for key in before}, "inventory": after}
    log_event(logger, "WARNING", "audit.inventory.cleared", **result)
    return result


@app.delete("/api/task-lists/{scope}")
def clear_task_list(scope: Literal["research", "source-jobs", "all"]) -> dict:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ensure_inventory_cleanup_idle(conn)
        deleted_research_tasks = 0
        deleted_source_jobs = 0
        if scope in ("research", "all"):
            task_ids = [row["id"] for row in conn.execute("SELECT id FROM tasks")]
            if task_ids:
                task_marks = ",".join("?" for _ in task_ids)
                child_job_ids = [
                    row["id"]
                    for row in conn.execute(
                        f"SELECT id FROM source_collection_jobs WHERE parent_task_id IN ({task_marks})",
                        task_ids,
                    )
                ]
                if child_job_ids:
                    child_marks = ",".join("?" for _ in child_job_ids)
                    conn.execute(f"DELETE FROM source_job_snapshots WHERE job_id IN ({child_marks})", child_job_ids)
                    conn.execute(f"DELETE FROM source_collection_runs WHERE job_id IN ({child_marks})", child_job_ids)
                    cursor = conn.execute(f"DELETE FROM source_collection_jobs WHERE id IN ({child_marks})", child_job_ids)
                    deleted_source_jobs += cursor.rowcount
                conn.execute(f"DELETE FROM agent_events WHERE task_id IN ({task_marks})", task_ids)
                cursor = conn.execute(f"DELETE FROM tasks WHERE id IN ({task_marks})", task_ids)
                deleted_research_tasks = cursor.rowcount
        if scope in ("source-jobs", "all"):
            conn.execute("DELETE FROM source_job_snapshots")
            conn.execute("DELETE FROM source_collection_runs")
            cursor = conn.execute("DELETE FROM source_collection_jobs")
            deleted_source_jobs += cursor.rowcount
    result = {
        "scope": scope,
        "deleted_research_tasks": deleted_research_tasks,
        "deleted_source_jobs": deleted_source_jobs,
    }
    log_event(logger, "WARNING", "audit.task_list.cleared", **result)
    return result


def market_data_component_status() -> dict:
    config = market_data_config()
    token_configured = bool(str(config.get("tushare_token") or "").strip())
    components = []
    for component, package in (("akshare", "akshare"), ("baostock", "baostock"), ("tushare", "tushare")):
        enabled = bool(config.get(f"enable_{component}", True))
        installed = importlib.util.find_spec(package) is not None
        if not enabled:
            status = "disabled"
        elif not installed:
            status = "missing"
        elif component == "tushare" and not token_configured:
            status = "needs_token"
        else:
            status = "ready"
        components.append({"id": component, "enabled": enabled, "installed": installed, "status": status})
    ready_count = sum(component["status"] == "ready" for component in components)
    return {
        "status": "online" if ready_count else "offline",
        "message": f"市场数据组件可用 {ready_count}/3；单个组件采集异常时会自动保留其他组件结果",
        "checked_at": now(),
        "components": components,
        "config": market_data_config_public(),
    }


@app.put("/api/channels/akshare/market-data-config")
def update_market_data_config(payload: MarketDataConfigInput) -> dict:
    if not any((payload.enable_akshare, payload.enable_baostock, payload.enable_tushare)):
        raise HTTPException(409, "至少启用一个市场数据组件")
    existing = market_data_config()
    supplied_token = payload.tushare_token.strip()
    if payload.clear_tushare_token:
        tushare_token = ""
    elif supplied_token and supplied_token != MASKED_SECRET:
        tushare_token = supplied_token
    else:
        tushare_token = str(existing.get("tushare_token") or "")
    save_channel_request_config(
        "akshare",
        {
            "adapter": "market_data_aggregate",
            "enable_akshare": payload.enable_akshare,
            "enable_baostock": payload.enable_baostock,
            "enable_tushare": payload.enable_tushare,
            "tushare_token": tushare_token,
            "component_timeout_seconds": payload.component_timeout_seconds,
        },
    )
    return {"status": "saved", "config": market_data_config_public()}


@app.put("/api/channels/wechat-mp-rss/config")
def update_wechat_rss_config(payload: WechatRssConfigInput) -> dict:
    existing = wechat_rss_config()
    supplied_access_key = payload.access_key.strip()
    supplied_secret_key = payload.secret_key.strip()
    supplied_admin_password = payload.admin_password.strip()
    if payload.clear_credentials:
        access_key = ""
        secret_key = ""
    else:
        access_key = supplied_access_key if supplied_access_key and supplied_access_key != MASKED_SECRET else str(existing.get("access_key") or "")
        secret_key = supplied_secret_key if supplied_secret_key and supplied_secret_key != MASKED_SECRET else str(existing.get("secret_key") or "")
    admin_password = supplied_admin_password if supplied_admin_password and supplied_admin_password != MASKED_SECRET else str(existing.get("admin_password") or "admin@123")
    try:
        config = normalize_werss_config(
            {
                "base_url": payload.base_url.strip() or str(existing.get("base_url") or ""),
                "feed_ids": payload.feed_ids,
                "access_key": access_key,
                "secret_key": secret_key,
                "admin_username": payload.admin_username,
                "admin_password": admin_password,
                "timeout_seconds": payload.timeout_seconds,
                "max_items_per_feed": payload.max_items_per_feed,
            }
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    save_channel_request_config("wechat-mp-rss", config)
    with db() as conn:
        conn.execute(
            "UPDATE channels SET url=?,status='pending',updated_at=? WHERE id='wechat-mp-rss'",
            (config["base_url"], now()),
        )
    log_event(
        logger,
        "INFO",
        "channel.wechat_rss.config.saved",
        channel_id="wechat-mp-rss",
        feed_count=len(config["feed_ids"]),
        credentials_configured=bool(config["access_key"]),
    )
    return {"status": "saved", "config": public_werss_config(config)}


@app.get("/api/channels/wechat-mp-rss/component-status")
def wechat_rss_component_status() -> dict:
    return persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))


def persist_wechat_rss_status(result: dict) -> dict:
    status = "online" if result.get("ready") else ("pending" if result.get("service_online") else "offline")
    checked_at = str(result.get("checked_at") or now())
    result["status"] = status
    with db() as conn:
        conn.execute(
            "UPDATE channels SET status=?,last_check=?,updated_at=? WHERE id='wechat-mp-rss'",
            (status, checked_at, checked_at),
        )
    return result


@app.post("/api/channels/wechat-mp-rss/wechat-login")
def start_wechat_rss_login() -> dict:
    config = wechat_rss_config()
    status = managed_werss_status(config)
    if not status["service_online"]:
        if not managed_werss_start_available():
            raise HTTPException(409, "WeRSS 组件尚未就绪。请在项目目录执行启动脚本，并确认 werss 容器健康。")
        hostname = str(urlsplit(config["base_url"]).hostname or "").lower()
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(409, "WeRSS 服务不可用，请检查高级配置中的服务地址")
        log_event(logger, "INFO", "channel.wechat_rss.sidecar.autostarting", channel_id="wechat-mp-rss")
        try:
            start_managed_werss()
        except Exception as exc:
            log_exception(logger, "channel.wechat_rss.sidecar.failed", exc, channel_id="wechat-mp-rss")
            raise HTTPException(409, str(exc)) from exc
        for _ in range(30):
            time.sleep(0.5)
            status = managed_werss_status(config)
            if status["service_online"]:
                break
        if not status["service_online"]:
            raise HTTPException(409, "WeRSS 本地组件尚未就绪，请稍后重试")
    try:
        result = start_werss_wechat_login(config)
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.login.failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    if result.get("qr_image_url"):
        result["qr_image_url"] = "/api/channels/wechat-mp-rss/qr-image"
    log_event(logger, "INFO", "channel.wechat_rss.login.qr_created", channel_id="wechat-mp-rss")
    return result


@app.get("/api/channels/wechat-mp-rss/qr-image")
def wechat_rss_qr_image() -> Response:
    try:
        content, content_type = fetch_werss_qr_image(wechat_rss_config())
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.login.qr_image_failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    return Response(content=content, media_type=content_type, headers={"Cache-Control": "no-store"})


@app.get("/api/channels/wechat-mp-rss/wechat-login/status")
def wechat_rss_login_status() -> dict:
    try:
        result = werss_wechat_login_status(wechat_rss_config())
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.login.status_failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    if result["authorized"]:
        status = persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))
        result.update(
            {
                "ready": status["ready"],
                "subscriptions": status["subscriptions"],
                "subscription_count": status["subscription_count"],
            }
        )
        log_event(
            logger,
            "INFO",
            "channel.wechat_rss.login.authorized",
            channel_id="wechat-mp-rss",
            subscription_count=status["subscription_count"],
            ready=status["ready"],
        )
    return result


@app.get("/api/channels/wechat-mp-rss/subscriptions")
def wechat_rss_subscriptions() -> dict:
    try:
        status = persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.subscriptions.failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    return {
        "status": status["status"],
        "ready": status["ready"],
        "subscriptions": status["subscriptions"],
        "subscription_count": status["subscription_count"],
        "message": status["message"],
    }


@app.get("/api/channels/wechat-mp-rss/subscriptions/search")
def search_wechat_rss_subscriptions(q: str = Query(min_length=1, max_length=100)) -> dict:
    try:
        items = search_werss_public_accounts(wechat_rss_config(), q)
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.subscriptions.search_failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    log_event(logger, "INFO", "channel.wechat_rss.subscriptions.searched", channel_id="wechat-mp-rss", result_count=len(items))
    return {"items": items, "count": len(items)}


@app.post("/api/channels/wechat-mp-rss/subscriptions")
def add_wechat_rss_subscription(payload: WechatRssSubscriptionInput) -> dict:
    try:
        subscription = add_werss_subscription(wechat_rss_config(), payload.model_dump())
        status = persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.subscription.add_failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    log_event(logger, "INFO", "channel.wechat_rss.subscription.added", channel_id="wechat-mp-rss", subscription_id=subscription["id"])
    return {
        "subscription": subscription,
        "ready": status["ready"],
        "subscriptions": status["subscriptions"],
        "subscription_count": status["subscription_count"],
    }


@app.delete("/api/channels/wechat-mp-rss/subscriptions/{subscription_id}")
def remove_wechat_rss_subscription(subscription_id: str) -> dict:
    try:
        subscription = delete_werss_subscription(wechat_rss_config(), subscription_id)
        status = persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))
    except Exception as exc:
        log_exception(logger, "channel.wechat_rss.subscription.remove_failed", exc, channel_id="wechat-mp-rss", subscription_id=subscription_id)
        raise HTTPException(409, str(exc)) from exc
    log_event(logger, "INFO", "channel.wechat_rss.subscription.removed", channel_id="wechat-mp-rss", subscription_id=subscription_id)
    return {
        "subscription": subscription,
        "ready": status["ready"],
        "subscriptions": status["subscriptions"],
        "subscription_count": status["subscription_count"],
    }


@app.post("/api/channels/wechat-mp-rss/start-sidecar")
def start_wechat_rss_sidecar() -> dict:
    log_event(logger, "INFO", "channel.wechat_rss.sidecar.starting", channel_id="wechat-mp-rss")
    try:
        start_managed_werss()
    except RuntimeError as exc:
        log_exception(logger, "channel.wechat_rss.sidecar.failed", exc, channel_id="wechat-mp-rss")
        raise HTTPException(409, str(exc)) from exc
    for _ in range(30):
        status = managed_werss_status(wechat_rss_config())
        if status["service_online"]:
            log_event(logger, "INFO", "channel.wechat_rss.sidecar.started", channel_id="wechat-mp-rss", rss_online=status["rss_online"])
            return persist_wechat_rss_status(status)
        time.sleep(0.5)
    status = persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))
    log_event(logger, "WARNING", "channel.wechat_rss.sidecar.start_timeout", channel_id="wechat-mp-rss")
    return status


@app.post("/api/channels")
def create_channel(payload: ChannelInput) -> dict:
    channel = {"id": uuid4().hex[:10], **payload.model_dump(), "builtin": 0, "last_check": "", "updated_at": now()}
    channel["group_ids"] = json.dumps(channel["group_ids"], ensure_ascii=False)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO channels(
              id,name,type,url,collection_mode,status,notes,validation_url,success_url_contains,success_selector,
              group_ids,parsing_strategy,normalization_quality_threshold,max_scrolls,research_enabled,builtin,last_check,updated_at
            )
            VALUES(
              :id,:name,:type,:url,:collection_mode,:status,:notes,:validation_url,:success_url_contains,:success_selector,
              :group_ids,:parsing_strategy,:normalization_quality_threshold,:max_scrolls,:research_enabled,:builtin,:last_check,:updated_at
            )
            """,
            channel,
        )
    return {**channel, "group_ids": json.loads(channel["group_ids"])}


@app.put("/api/channels/{channel_id}")
def update_channel(channel_id: str, payload: ChannelInput) -> dict:
    channel = {"id": channel_id, **payload.model_dump(), "updated_at": now()}
    channel["group_ids"] = json.dumps(channel["group_ids"], ensure_ascii=False)
    with db() as conn:
        existing = conn.execute("SELECT builtin FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "渠道不存在")
        channel["builtin"] = existing["builtin"]
        conn.execute(
            """
            UPDATE channels SET name=:name,type=:type,url=:url,collection_mode=:collection_mode,
            status=:status,notes=:notes,validation_url=:validation_url,
            success_url_contains=:success_url_contains,success_selector=:success_selector,group_ids=:group_ids,
            parsing_strategy=:parsing_strategy,normalization_quality_threshold=:normalization_quality_threshold,max_scrolls=:max_scrolls,
            research_enabled=:research_enabled,updated_at=:updated_at WHERE id=:id
            """,
            channel,
        )
    return {**channel, "group_ids": json.loads(channel["group_ids"])}


def mx_har_text_from_upload(content_type: str, body: bytes) -> str:
    if len(body) > MAX_MX_HAR_UPLOAD_BYTES:
        raise HTTPException(413, "HAR 上传内容过大，请只保留 MX 登录和消息请求")
    try:
        if content_type.partition(";")[0].strip().lower() == "application/json":
            har_text = MxHarImportInput.model_validate_json(body).har_text
        else:
            har_text = body.decode("utf-8-sig")
    except (UnicodeDecodeError, ValidationError) as exc:
        raise HTTPException(422, "HAR 上传格式无法识别，请选择浏览器导出的 HAR 文件") from exc
    if len(har_text.encode("utf-8")) > MAX_MX_HAR_BYTES:
        raise HTTPException(413, "HAR 文件过大，请只保留 MX 登录和消息请求")
    return har_text


def import_mx_har_text(channel_id: str, har_text: str) -> dict:
    if channel_id != "web-rumors":
        raise HTTPException(409, "HAR import is only available for the MX source channel")
    try:
        from backend.import_mx_har import import_har_text

        result = import_har_text(har_text)
        log_event(logger, "INFO", "channel.mx_har.imported", channel_id=channel_id, validated_snapshot_count=result.get("validated_snapshot_count", 0))
        return result
    except Exception as exc:
        log_exception(logger, "channel.mx_har.failed", exc, channel_id=channel_id)
        detail = str(redact(str(exc)))[:600]
        raise HTTPException(409, f"MX HAR 验证失败：{detail}") from exc


@app.post("/api/channels/{channel_id}/import-mx-har")
async def import_mx_har(channel_id: str, request: Request) -> dict:
    return import_mx_har_text(
        channel_id,
        mx_har_text_from_upload(request.headers.get("content-type", ""), await request.body()),
    )


@app.delete("/api/channels/{channel_id}")
def delete_channel(channel_id: str) -> dict:
    with db() as conn:
        existing = conn.execute("SELECT builtin FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "渠道不存在")
        if existing["builtin"]:
            raise HTTPException(409, "内置渠道不能删除")
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        conn.execute("DELETE FROM channel_request_configs WHERE channel_id=?", (channel_id,))
    return {"status": "ok"}


def browser_profile(channel_id: str) -> Path:
    return DATA_DIR / "browser-profile" / channel_id


def channel_validation_url(channel: sqlite3.Row | dict) -> str:
    validation_url = str(channel["validation_url"] or "").strip()
    if validation_url:
        return validation_url
    if channel["id"] == "zsxq":
        try:
            group_ids = json.loads(channel["group_ids"] or "[]")
        except json.JSONDecodeError:
            group_ids = []
        for value in group_ids:
            group_id = str(value).strip()
            if re.fullmatch(r"\d+", group_id):
                return f"https://wx.zsxq.com/group/{group_id}"
    return str(channel["url"] or "").strip()


def check_mx_channel(channel_id: str, request_config: dict) -> dict:
    checked_at = now()
    try:
        from backend.collectors import collect_mx

        current = datetime.now(timezone.utc).astimezone().replace(microsecond=0)
        collect_mx(
            {
                "id": channel_id,
                "collection_mode": "requests",
                "request_config": {
                    **request_config,
                    "room_ids": [20099],
                    "page_size": 1,
                    "max_pages_per_room": 1,
                    "request_delay_seconds": 0,
                    "allow_partial_window": True,
                },
            },
            {
                "window_start": iso(current - timedelta(days=30)),
                "window_end": iso(current),
            },
        )
        status = "online"
        message = "MX 授权会话可用"
    except Exception:
        status = "offline"
        message = "MX 授权会话已失效，请重新登录并导入新的 HAR"
    with db() as conn:
        conn.execute("UPDATE channels SET status=?,last_check=?,updated_at=? WHERE id=?", (status, checked_at, checked_at, channel_id))
    return {"status": status, "message": message, "checked_at": checked_at}


@app.post("/api/channels/{channel_id}/login")
def launch_channel_login(channel_id: str) -> dict:
    with db() as conn:
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if not channel:
        raise HTTPException(404, "渠道不存在")
    if channel["collection_mode"] != "playwright":
        raise HTTPException(409, "仅 Playwright 渠道需要浏览器登录")
    if not channel["url"]:
        raise HTTPException(409, "请先填写渠道入口 URL")
    profile = browser_profile(channel_id)
    profile.mkdir(parents=True, exist_ok=True)
    login_url = channel_validation_url(channel) if channel_id == "zsxq" else channel["url"]
    try:
        with CHANNEL_LOGIN_PROCESSES_LOCK:
            process = CHANNEL_LOGIN_PROCESSES.get(channel_id)
            if process is None or process.poll() is not None:
                process = subprocess.Popen(
                    [sys.executable, str(ROOT / "backend" / "browser_session.py"), "login", "--profile", str(profile), "--url", login_url],
                    cwd=ROOT,
                    creationflags=hidden_window_creationflags(),
                )
                CHANNEL_LOGIN_PROCESSES[channel_id] = process
    except OSError as exc:
        log_exception(logger, "channel.login.launch_failed", exc, channel_id=channel_id)
        raise HTTPException(502, f"登录工作区启动失败：{type(exc).__name__}") from exc
    message = "登录浏览器已启动。完成登录后可直接点击检查状态；开始采集前请关闭登录浏览器页签。"
    return {"status": "opened", "message": message, "login_url": BROWSER_WORKSPACE_PUBLIC_URL}


@app.post("/api/channels/{channel_id}/check")
def check_channel(channel_id: str) -> dict:
    log_event(logger, "INFO", "channel.check.started", channel_id=channel_id)
    with db() as conn:
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if channel_id == "akshare" and channel:
        result = market_data_component_status()
        with db() as conn:
            conn.execute(
                "UPDATE channels SET status=?,last_check=?,updated_at=? WHERE id=?",
                (result["status"], result["checked_at"], result["checked_at"], channel_id),
            )
        log_event(logger, "INFO" if result["status"] == "online" else "WARNING", "channel.check.completed", channel_id=channel_id, status=result["status"], message=result["message"])
        return result
    if channel_id == "industry-news" and channel:
        from backend.industry_news_sources import check_public_industry_news

        result = check_public_industry_news()
        with db() as conn:
            conn.execute(
                "UPDATE channels SET status=?,last_check=?,updated_at=? WHERE id=?",
                (result["status"], result["checked_at"], result["checked_at"], channel_id),
            )
        log_event(logger, "INFO" if result["status"] == "online" else "WARNING", "channel.check.completed", channel_id=channel_id, status=result["status"], message=result["message"])
        return result
    if channel_id == "wechat-mp-rss" and channel:
        result = persist_wechat_rss_status(managed_werss_status(wechat_rss_config()))
        log_event(logger, "INFO" if result["status"] == "online" else "WARNING", "channel.check.completed", channel_id=channel_id, status=result["status"], message=result["message"])
        return result
    if channel:
        request_config = channel_request_config(channel_id)
        if request_config.get("adapter") == "mx_authorized_request_replay":
            result = check_mx_channel(channel_id, request_config)
            log_event(logger, "INFO" if result["status"] == "online" else "WARNING", "channel.check.completed", channel_id=channel_id, status=result["status"], message=result["message"])
            return result
    if not channel:
        raise HTTPException(404, "渠道不存在")
    if channel["collection_mode"] != "playwright":
        result = {"status": channel["status"], "message": "该渠道无需浏览器登录检查"}
        log_event(logger, "INFO", "channel.check.completed", channel_id=channel_id, **result)
        return result
    validation_url = channel_validation_url(channel)
    if not validation_url:
        raise HTTPException(409, "请先填写检查 URL 或渠道入口 URL")
    profile = browser_profile(channel_id)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "backend" / "browser_session.py"), "check",
                "--profile", str(profile), "--url", validation_url,
                "--channel-id", channel_id,
                "--success-url-contains", channel["success_url_contains"],
                "--success-selector", channel["success_selector"],
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=hidden_window_creationflags(),
            check=True,
        )
        detail = json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "渠道检查超时，请关闭登录窗口后重试") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
        raise HTTPException(502, f"浏览器状态检查失败: {detail[-1200:]}") from exc
    status = "online" if detail["available"] else "offline"
    checked_at = now()
    with db() as conn:
        conn.execute("UPDATE channels SET status=?,last_check=?,updated_at=? WHERE id=?", (status, checked_at, checked_at, channel_id))
    result = {"status": status, "message": detail["message"], "checked_at": checked_at, "final_url": detail["final_url"]}
    log_event(logger, "INFO" if status == "online" else "WARNING", "channel.check.completed", channel_id=channel_id, **result)
    return result


def refresh_browser_channel_states() -> None:
    with db() as conn:
        channels = [
            dict(row)
            for row in conn.execute(
                "SELECT id FROM channels WHERE collection_mode='playwright' OR id IN ('web-rumors','akshare','industry-news','wechat-mp-rss')"
            )
        ]
    for channel in channels:
        if channel["id"] in ("web-rumors", "akshare", "industry-news", "wechat-mp-rss") or browser_profile(channel["id"]).exists():
            try:
                check_channel(channel["id"])
            except Exception as exc:
                log_exception(logger, "channel.startup_check.failed", exc, channel_id=channel["id"], detail=getattr(exc, "detail", str(exc)))
                checked_at = now()
                with db() as conn:
                    conn.execute("UPDATE channels SET status='offline',last_check=?,updated_at=? WHERE id=?", (checked_at, checked_at, channel["id"]))


@app.post("/api/channels/check-all")
def check_all_channels() -> dict:
    refresh_browser_channel_states()
    return {"status": "ok", "message": "已完成信源渠道巡检"}


def iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def latest_reserved_at(conn: sqlite3.Connection, channel_id: str, scope_key: str = "") -> datetime | None:
    scope_key = canonical_scope_key(scope_key)
    values: list[str] = []
    watermark = conn.execute(
        "SELECT last_success_at FROM source_collection_watermarks_v2 WHERE channel_id=? AND scope_key=?",
        (channel_id, scope_key),
    ).fetchone()
    if watermark:
        values.append(watermark["last_success_at"])
    current = datetime.now(timezone.utc).astimezone()
    for row in conn.execute(
        "SELECT windows,status,started_at,completed_at,query FROM source_collection_jobs WHERE status IN ('queued','running','failed','report_failed')"
    ):
        if canonical_scope_key(row["query"] or "") != scope_key:
            continue
        if row["status"] in ("failed", "report_failed"):
            attempted_at = row["completed_at"] or row["started_at"]
            if not attempted_at or current - datetime.fromisoformat(attempted_at) >= MIN_COLLECTION_INTERVAL:
                continue
        for window in json.loads(row["windows"]):
            if window["channel_id"] == channel_id and window.get("window_end"):
                values.append(window["window_end"])
    return max((datetime.fromisoformat(value) for value in values), default=None)


def normalized_source_label(item: dict[str, Any]) -> str:
    metadata = item.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    metadata = metadata if isinstance(metadata, dict) else {}
    source_account = metadata.get("source_account") if isinstance(metadata.get("source_account"), dict) else {}
    account_name = str(source_account.get("name") or "").strip()
    if account_name:
        return f"微信公众号：{account_name}"
    return str(item.get("author") or item.get("channel_id") or "").strip()


def local_snapshot_context(
    channel_ids: list[str],
    lookback_days: int,
    *,
    general_snapshots_only: bool = False,
    research_scope_key: str = "",
    include_source_reports: bool = False,
) -> tuple[str, str, str]:
    placeholders = ",".join("?" for _ in channel_ids)
    scoped_key = canonical_scope_key(research_scope_key)
    if general_snapshots_only:
        general_filter = " AND scope_type='general'"
        normalized_general_filter = " AND s.scope_type='general'"
        scope_params: list[str] = []
    elif scoped_key:
        general_filter = " AND (scope_type='general' OR (scope_type='research' AND scope_key=?))"
        normalized_general_filter = " AND (s.scope_type='general' OR (s.scope_type='research' AND s.scope_key=?))"
        scope_params = [scoped_key]
    else:
        general_filter = " AND scope_type='general'"
        normalized_general_filter = " AND s.scope_type='general'"
        scope_params = []
    with db() as conn:
        anchor_row = conn.execute(
            f"SELECT MAX(collected_at) AS anchor FROM source_snapshots WHERE channel_id IN ({placeholders}){general_filter}",
            [*channel_ids, *scope_params],
        ).fetchone()
        anchor = anchor_row["anchor"]
        if not anchor:
            raise HTTPException(409, "本地尚无所选信源快照。请先创建采集任务并等待采集器完成。")
        anchor_dt = datetime.fromisoformat(anchor)
        window_start = iso(anchor_dt - timedelta(days=lookback_days))
        normalized_items = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT n.channel_id,n.occurred_at,n.author,n.title,n.content,n.source_url,n.metadata,n.quality_score,n.normalization_mode
                FROM normalized_source_items n
                JOIN source_snapshots s ON s.id=n.snapshot_id
                WHERE n.channel_id IN ({placeholders}) AND n.occurred_at BETWEEN ? AND ?{normalized_general_filter}
                ORDER BY n.occurred_at DESC LIMIT 500
                """,
                [*channel_ids, window_start, anchor, *scope_params],
            )
        ]
        snapshots = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT channel_id,occurred_at,collected_at,source_url,content,normalization_status
                FROM source_snapshots
                WHERE channel_id IN ({placeholders}) AND occurred_at BETWEEN ? AND ?{general_filter}
                ORDER BY occurred_at DESC LIMIT 200
                """,
                [*channel_ids, window_start, anchor, *scope_params],
            )
        ]
        source_reports = [
            dict(row)
            for row in conn.execute(
                """
                SELECT report_title,report_anchor,report
                FROM source_collection_jobs
                WHERE report IS NOT NULL AND parent_task_id='' AND query='' AND evidence_layer=''
                ORDER BY report_anchor DESC,created_at DESC
                LIMIT 12
                """
            )
        ] if include_source_reports else []
    if not normalized_items and not snapshots:
        raise HTTPException(409, "本地快照存在，但所选时间窗口内没有可用于报告的内容。")
    normalized_chunks = [
        (
            f"[normalized:{item['channel_id']}] {item['occurred_at']} quality={item['quality_score']} "
            f"mode={item['normalization_mode']} source={normalized_source_label(item)} title={item['title']} {item['source_url']}\n"
            f"{item['content'][:8_000]}"
        )
        for item in normalized_items
    ]
    normalized_channels = {item["channel_id"] for item in normalized_items}
    raw_fallbacks = [
        item
        for item in snapshots
        if item["channel_id"] not in normalized_channels or item["normalization_status"] in {"pending", "failed"}
    ]
    raw_chunks = [
        f"[raw:{item['channel_id']}] {item['occurred_at']} {item['source_url']}\n{item['content'][:12_000]}"
        for item in raw_fallbacks
    ]
    report_chunks = [
        f"[source-report] {item['report_anchor']} {item['report_title']}\n{item['report'][:20_000]}"
        for item in source_reports
    ]
    if normalized_chunks:
        chunks = [
            (
                f"{chunk}"
            )
            for chunk in [*normalized_chunks, *raw_chunks, *report_chunks]
        ]
    else:
        chunks = [*raw_chunks, *report_chunks]
    selected: list[str] = []
    length = 0
    for chunk in chunks:
        if length + len(chunk) > 120_000:
            break
        selected.append(chunk)
        length += len(chunk)
    context = "\n\n".join(selected)
    return anchor, window_start, context


def ensure_general_source_report(payload: SourceJobInput) -> None:
    if payload.parent_task_id or payload.query or payload.evidence_layer:
        raise HTTPException(409, "信源聚合报告禁止继承个股研究任务上下文")


def generate_source_report(payload: SourceJobInput) -> tuple[str, str]:
    ensure_general_source_report(payload)
    anchor, window_start, context = local_snapshot_context(
        payload.channel_ids,
        payload.lookback_days,
        general_snapshots_only=True,
    )
    source_names = channel_names_for_ids(payload.channel_ids)
    prompt = f"""请基于以下通用信源快照生成独立的信源聚合报告，不要发起新的数据采集，不要补写不存在的事实。
不得读取或继承个股研究任务中的股票代码、研究目标、Skill 和临时补采证据。
报告应围绕快照中明确出现的产业趋势、事件脉络、主题分组、来源和待核验事项展开。
每项事实和事件尽量标注具体信源名称。微信公众号文章必须优先使用输入中的 source_account 或 source 字段标注具体公众号，不得只写“微信公众号”。
仅输出完整 HTML 文档，必须包含 <html>、<head> 和 <body>。可以使用内联 CSS 优化排版。
严禁输出 Markdown，严禁使用 Markdown 代码围栏，严禁输出 HTML 文档以外的解释文字。
报告名称：{payload.report_title}
数据锚点：{anchor}
报告窗口：{window_start} 至 {anchor}
信源：{", ".join(source_names)}

通用信源快照：
{context}"""
    return require_or_repair_html_report(
        call_provider(prompt, system_prompt=source_report_system_prompt(), purpose="source_report"),
        purpose="source_report",
    ), anchor


def report_after_collection(job: dict) -> tuple[str, str]:
    return generate_source_report(
        SourceJobInput(
            action="report",
            channel_ids=json.loads(job["channel_ids"]),
            lookback_days=job["lookback_days"],
            report_title=job["report_title"],
            skill_name=job["skill_name"],
            query=job.get("query", ""),
            evidence_layer=job.get("evidence_layer", ""),
        )
    )


collection_worker = CollectionWorker(
    db_path=DB_PATH,
    profile_for=browser_profile,
    report_after_collection=report_after_collection,
    normalize_snapshot=normalize_snapshot_record,
    request_config_for=channel_request_config,
)


def insert_source_job(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        """
        INSERT INTO source_collection_jobs(id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,report,report_anchor,parent_task_id,query,evidence_layer)
        VALUES(:id,:action,:channel_ids,:windows,:lookback_days,:skill_name,:report_title,:status,:created_at,:report,:report_anchor,:parent_task_id,:query,:evidence_layer)
        """,
        job,
    )


def source_job_response(job: dict, *, channel_names: dict[str, str] | None = None, **extra: object) -> dict:
    result = {**job}
    if isinstance(result.get("channel_ids"), str):
        result["channel_ids"] = json.loads(result["channel_ids"])
    if isinstance(result.get("windows"), str):
        result["windows"] = json.loads(result["windows"])
    result["channel_names"] = channel_names_for_ids(result.get("channel_ids") or [], channel_names)
    result["has_report"] = bool(result.get("report"))
    return {**result, **extra}


def source_job_list_response(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    job_ids = [row["id"] for row in rows]
    job_marks = ",".join("?" for _ in job_ids)
    run_rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM source_collection_runs WHERE job_id IN ({job_marks}) ORDER BY started_at,channel_id",
            job_ids,
        )
    ]
    channel_names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM channels")}
    runs_by_job: dict[str, list[dict]] = {}
    for run in run_rows:
        runs_by_job.setdefault(run["job_id"], []).append(run)
    results = []
    for row in rows:
        item = source_job_response(row, channel_names=channel_names)
        item["runs"] = runs_by_job.get(item["id"], [])
        item["report"] = None
        results.append(item)
    return results


@app.post("/api/source-jobs")
def create_source_job(payload: SourceJobInput) -> dict:
    if payload.action in ("collect_report", "report"):
        ensure_general_source_report(payload)
    scope_key = canonical_scope_key(payload.query)
    if payload.action == "report":
        channel_ids = sorted(set(payload.channel_ids))
        channel_ids_json = json.dumps(channel_ids, ensure_ascii=False)
        created_at = now()
        job = {
            "id": uuid4().hex[:10], "action": payload.action, "channel_ids": channel_ids_json,
            "windows": "[]", "lookback_days": payload.lookback_days, "skill_name": payload.skill_name,
            "report_title": payload.report_title, "status": "generating_report", "created_at": created_at,
            "report": None, "report_anchor": "", "parent_task_id": payload.parent_task_id,
            "query": scope_key, "evidence_layer": payload.evidence_layer,
        }
        cutoff = iso(datetime.fromisoformat(created_at) - REPORT_DEDUP_INTERVAL)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM source_collection_jobs
                WHERE action='report' AND channel_ids=? AND lookback_days=? AND skill_name=? AND report_title=?
                  AND parent_task_id=? AND query=? AND evidence_layer=? AND created_at>=?
                  AND status IN ('generating_report','review','partial_review')
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    channel_ids_json,
                    payload.lookback_days,
                    payload.skill_name,
                    payload.report_title,
                    payload.parent_task_id,
                    scope_key,
                    payload.evidence_layer,
                    cutoff,
                ),
            ).fetchone()
            if existing:
                log_event(
                    logger,
                    "INFO",
                    "source_job.deduplicated",
                    job_id=existing["id"],
                    action=payload.action,
                    channel_ids=channel_ids,
                    lookback_days=payload.lookback_days,
                    status=existing["status"],
                )
                return source_job_response(dict(existing), deduplicated=True)
            insert_source_job(conn, job)
        log_event(
            logger,
            "INFO",
            "source_job.created",
            job_id=job["id"],
            action=job["action"],
            channel_ids=channel_ids,
            lookback_days=job["lookback_days"],
            status=job["status"],
        )
        return source_job_response(job)
    else:
        current = datetime.now(timezone.utc).astimezone().replace(microsecond=0)
        lower_bound = current - timedelta(days=payload.lookback_days)
        windows = []
        with db() as conn:
            known = {row["id"] for row in conn.execute("SELECT id FROM channels")}
            for channel_id in payload.channel_ids:
                if channel_id not in known:
                    raise HTTPException(404, f"信源不存在: {channel_id}")
                reserved = latest_reserved_at(conn, channel_id, scope_key)
                if reserved and current - reserved < MIN_COLLECTION_INTERVAL:
                    continue
                window_start = max(lower_bound, reserved) if reserved else lower_bound
                if window_start < current:
                    windows.append({"channel_id": channel_id, "window_start": iso(window_start), "window_end": iso(current)})
        job = {
            "id": uuid4().hex[:10], "action": payload.action, "channel_ids": json.dumps(payload.channel_ids),
            "windows": json.dumps(windows), "lookback_days": payload.lookback_days, "skill_name": payload.skill_name,
            "report_title": payload.report_title, "status": "queued" if windows else "deduplicated",
            "created_at": now(), "report": None, "report_anchor": "", "parent_task_id": payload.parent_task_id,
            "query": scope_key, "evidence_layer": payload.evidence_layer,
        }
    with db() as conn:
        insert_source_job(conn, job)
    log_event(
        logger,
        "INFO",
        "source_job.created",
        job_id=job["id"],
        action=job["action"],
        channel_ids=payload.channel_ids,
        lookback_days=job["lookback_days"],
        status=job["status"],
        window_count=len(windows),
        parent_task_id=job["parent_task_id"],
        evidence_layer=job["evidence_layer"],
    )
    return source_job_response(job)


@app.post("/api/source-jobs/{job_id}/complete")
def complete_source_job(job_id: str, payload: CompleteSourceJobInput) -> dict:
    with db() as conn:
        job = conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "信源任务不存在")
        if job["status"] not in ("queued", "running"):
            raise HTTPException(409, "只有排队中或执行中的任务可以完成")
        windows = json.loads(job["windows"])
        allowed_channels = {window["channel_id"] for window in windows}
        submitted_channels = {item.channel_id for item in payload.snapshots}
        if not payload.snapshots:
            raise HTTPException(409, "没有真实快照，不能推进采集水位")
        if not submitted_channels.issubset(allowed_channels):
            raise HTTPException(409, "快照包含不属于当前任务窗口的信源")
        channel_windows = {window["channel_id"]: window for window in windows}
        for item in payload.snapshots:
            window = channel_windows[item.channel_id]
            if not datetime.fromisoformat(window["window_start"]) <= item.occurred_at.astimezone() <= datetime.fromisoformat(window["window_end"]):
                raise HTTPException(409, f"快照时间戳不属于当前采集窗口: {item.channel_id}")
        collected_at = now()
        general_refresh = job["evidence_layer"] == "local_source_snapshots" and not canonical_scope_key(job["query"])
        scope_type = "general" if general_refresh or not (job["parent_task_id"] or job["query"] or job["evidence_layer"]) else "research"
        scope_key = canonical_scope_key(job["query"]) if scope_type == "research" else ""
        inserted = 0
        inserted_snapshot_ids: list[str] = []
        submitted_counts = {channel_id: 0 for channel_id in submitted_channels}
        inserted_counts = {channel_id: 0 for channel_id in submitted_channels}
        for snapshot in payload.snapshots:
            submitted_counts[snapshot.channel_id] += 1
            before = conn.total_changes
            snapshot_id = uuid4().hex[:12]
            conn.execute(
                """
                INSERT OR IGNORE INTO source_snapshots(
                  id,channel_id,occurred_at,collected_at,source_url,content,scope_type,scope_key
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    snapshot.channel_id,
                    iso(snapshot.occurred_at),
                    collected_at,
                    snapshot.source_url,
                    snapshot.content,
                    scope_type,
                    scope_key,
                ),
            )
            delta = conn.total_changes - before
            inserted += delta
            if delta:
                inserted_counts[snapshot.channel_id] += 1
                inserted_snapshot_ids.append(snapshot_id)
            else:
                existing = conn.execute(
                    """
                    SELECT id FROM source_snapshots
                    WHERE channel_id=? AND source_url=? AND occurred_at=? AND scope_type=? AND scope_key=?
                    """,
                    (snapshot.channel_id, snapshot.source_url, iso(snapshot.occurred_at), scope_type, scope_key),
                ).fetchone()
                if not existing:
                    continue
                snapshot_id = existing["id"]
            conn.execute(
                "INSERT OR IGNORE INTO source_job_snapshots(job_id,snapshot_id) VALUES(?,?)",
                (job_id, snapshot_id),
            )
        for window in windows:
            if window["channel_id"] not in submitted_channels:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at) VALUES(?,?,?)",
                (window["channel_id"], scope_key, window["window_end"]),
            )
            conn.execute(
                """
                INSERT INTO source_collection_runs(job_id,channel_id,status,completed_at,snapshot_count,duplicate_count)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(job_id,channel_id) DO UPDATE SET
                  status=excluded.status,completed_at=excluded.completed_at,
                  snapshot_count=excluded.snapshot_count,duplicate_count=excluded.duplicate_count
                """,
                (
                    job_id,
                    window["channel_id"],
                    "completed" if inserted_counts[window["channel_id"]] else "deduplicated",
                    collected_at,
                    inserted_counts[window["channel_id"]],
                    submitted_counts[window["channel_id"]] - inserted_counts[window["channel_id"]],
                ),
            )
        partial = submitted_channels != allowed_channels
        next_status = "generating_report" if job["action"] == "collect_report" else "partial_completed" if partial else "completed"
        conn.execute(
            "UPDATE source_collection_jobs SET status=?,snapshot_count=?,completed_at=? WHERE id=?",
            (next_status, inserted, collected_at, job_id),
        )
    for snapshot_id in inserted_snapshot_ids:
        normalize_snapshot_record(snapshot_id)
    if job["parent_task_id"] and collection_worker.on_evidence_ready:
        with db() as conn:
            conn.execute("UPDATE tasks SET status='evidence_ready' WHERE id=?", (job["parent_task_id"],))
        collection_worker.on_evidence_ready(job["parent_task_id"])
    return {"status": next_status, "snapshot_count": inserted, "report": None}


@app.post("/api/source-jobs/{job_id}/retry")
def retry_source_job(job_id: str) -> dict:
    with db() as conn:
        job = conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "信源任务不存在")
        can_generate_deduplicated_report = job["status"] in ("completed", "deduplicated") and job["action"] == "collect_report"
        can_regenerate_deleted_report = job["status"] == "report_deleted" and job["action"] in ("report", "collect_report")
        if job["status"] not in ("failed", "report_failed", "cancelled") and not can_generate_deduplicated_report and not can_regenerate_deleted_report:
            raise HTTPException(409, "只有失败或已取消的信源任务可以重试")
        attempted_at = job["completed_at"] or job["started_at"]
        if job["status"] == "failed" and attempted_at:
            retry_at = datetime.fromisoformat(attempted_at) + MIN_COLLECTION_INTERVAL
            if datetime.now(timezone.utc).astimezone() < retry_at:
                raise HTTPException(409, f"为避免信源封号，请在 {iso(retry_at)} 后重试")
        if job["status"] == "report_failed" or can_generate_deduplicated_report or can_regenerate_deleted_report:
            payload = SourceJobInput(
                action="report",
                channel_ids=json.loads(job["channel_ids"]),
                lookback_days=job["lookback_days"],
                report_title=job["report_title"],
                skill_name=job["skill_name"],
                query=job["query"],
                evidence_layer=job["evidence_layer"],
            )
        else:
            payload = None
            conn.execute(
                """
                UPDATE source_collection_jobs
                SET status='queued',started_at='',completed_at='',snapshot_count=0,error=''
                WHERE id=?
                """,
                (job_id,),
            )
    if payload:
        with db() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status='generating_report',error='' WHERE id=?",
                (job_id,),
            )
        return {"status": "generating_report"}
    return {"status": "queued"}


@app.get("/api/source-jobs/{job_id}/report")
def source_job_report(job_id: str) -> dict:
    with db() as conn:
        job = conn.execute(
            "SELECT id,report_title,status,report,report_anchor,error FROM source_collection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if not job:
        raise HTTPException(404, "信源任务不存在")
    if not job["report"]:
        raise HTTPException(409, "该任务尚未生成 HTML 报告")
    return dict(job)


@app.get("/api/source-jobs/{job_id}/snapshots")
def source_job_snapshots(job_id: str) -> dict:
    with db() as conn:
        job = conn.execute("SELECT * FROM source_collection_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "信源任务不存在")
        snapshots = [
            dict(row)
            for row in conn.execute(
                """
                SELECT s.id,s.channel_id,c.name AS channel_name,s.occurred_at,s.collected_at,s.source_url,s.content,s.scope_type,s.scope_key,
                       s.normalization_status,s.normalized_at,s.normalization_error,s.normalized_item_count
                FROM source_job_snapshots js
                JOIN source_snapshots s ON s.id=js.snapshot_id
                LEFT JOIN channels c ON c.id=s.channel_id
                WHERE js.job_id=?
                ORDER BY s.occurred_at DESC
                """,
                (job_id,),
            )
        ]
        if not snapshots and job["started_at"] and job["completed_at"]:
            channel_ids = json.loads(job["channel_ids"])
            placeholders = ",".join("?" for _ in channel_ids)
            if channel_ids:
                snapshots = [
                    dict(row)
                    for row in conn.execute(
                        f"""
                        SELECT s.id,s.channel_id,c.name AS channel_name,s.occurred_at,s.collected_at,s.source_url,s.content,s.scope_type,s.scope_key,
                               s.normalization_status,s.normalized_at,s.normalization_error,s.normalized_item_count
                        FROM source_snapshots s
                        LEFT JOIN channels c ON c.id=s.channel_id
                        WHERE s.channel_id IN ({placeholders}) AND s.collected_at BETWEEN ? AND ?
                        ORDER BY s.occurred_at DESC
                        """,
                        [*channel_ids, job["started_at"], job["completed_at"]],
                    )
                ]
    return {
        "job": {
            "id": job["id"],
            "report_title": job["report_title"],
            "action": job["action"],
            "status": job["status"],
            "created_at": job["created_at"],
            "completed_at": job["completed_at"],
            "snapshot_count": len(snapshots),
        },
        "snapshots": snapshots,
    }


@app.get("/api/normalized-items")
def list_normalized_items(channel_id: str = "", snapshot_id: str = "", limit: int = 100) -> dict:
    conditions: list[str] = []
    params: list[object] = []
    if channel_id:
        conditions.append("n.channel_id=?")
        params.append(channel_id)
    if snapshot_id:
        conditions.append("n.snapshot_id=?")
        params.append(snapshot_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit = max(1, min(500, limit))
    with db() as conn:
        items = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT n.*,c.name AS channel_name
                FROM normalized_source_items n
                LEFT JOIN channels c ON c.id=n.channel_id
                {where}
                ORDER BY n.occurred_at DESC,n.created_at DESC
                LIMIT ?
                """,
                [*params, limit],
            )
        ]
    for item in items:
        item["attachments"] = json.loads(item["attachments"] or "[]")
        item["metadata"] = json.loads(item["metadata"] or "{}")
    return {"items": items}


@app.post("/api/snapshots/{snapshot_id}/normalize")
def normalize_source_snapshot(snapshot_id: str) -> dict:
    return normalize_snapshot_record(snapshot_id, force=True)


@app.post("/api/channels/{channel_id}/normalize-existing")
def normalize_existing_channel_snapshots(channel_id: str, limit: int = 5) -> dict:
    limit = max(1, min(20, limit))
    with db() as conn:
        channel = conn.execute("SELECT id FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not channel:
            raise HTTPException(404, "Channel not found")
        snapshot_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM source_snapshots WHERE channel_id=? ORDER BY collected_at DESC LIMIT ?",
                (channel_id, limit),
            )
        ]
    results = [normalize_snapshot_record(snapshot_id, force=True) for snapshot_id in snapshot_ids]
    return {"channel_id": channel_id, "snapshot_count": len(snapshot_ids), "results": results}


def encrypted_provider_key(api_key: str, current: dict | None = None) -> str:
    if not api_key or api_key == MASKED_API_KEY:
        return (current or {}).get("encrypted_api_key", "")
    return cipher().encrypt(api_key.encode()).decode()


def save_provider_record(provider_id: str, payload: ProviderInput, current: dict | None = None) -> dict:
    encrypted_api_key = encrypted_provider_key(payload.api_key, current)
    value = {
        "id": provider_id,
        "name": payload.name.strip(),
        "base_url": payload.base_url.rstrip("/"),
        "model": payload.model.strip(),
        "protocol": payload.protocol,
        "encrypted_api_key": encrypted_api_key,
        "extra_body": json.dumps(payload.extra_body, ensure_ascii=False),
        "enabled": int(payload.enabled),
        "updated_at": now(),
    }
    if not value["name"] or not value["base_url"] or not value["model"]:
        raise HTTPException(400, "请填写模型通道名称、Base URL 和模型名称")
    with db() as conn:
        if current:
            conn.execute(
                """
                UPDATE model_providers SET name=:name,base_url=:base_url,model=:model,protocol=:protocol,
                encrypted_api_key=:encrypted_api_key,extra_body=:extra_body,enabled=:enabled,updated_at=:updated_at
                WHERE id=:id
                """,
                value,
            )
        else:
            count = conn.execute("SELECT COUNT(*) AS count FROM model_providers").fetchone()["count"]
            conn.execute(
                """
                INSERT INTO model_providers(
                  id,name,base_url,model,protocol,encrypted_api_key,extra_body,enabled,is_default,status,created_at,updated_at
                ) VALUES(:id,:name,:base_url,:model,:protocol,:encrypted_api_key,:extra_body,:enabled,:is_default,'untested',:created_at,:updated_at)
                """,
                {**value, "is_default": int(count == 0), "created_at": now()},
            )
    log_event(
        logger,
        "INFO",
        "provider.saved",
        provider_id=provider_id,
        name=value["name"],
        base_url=value["base_url"],
        model=value["model"],
        protocol=value["protocol"],
        enabled=bool(value["enabled"]),
        operation="updated" if current else "created",
    )
    return provider_public(provider_row(provider_id)) or {}


@app.get("/api/providers")
def list_providers() -> list[dict]:
    return providers_public()


@app.post("/api/providers")
def create_provider(payload: ProviderInput) -> dict:
    return save_provider_record(uuid4().hex[:10], payload)


@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: str, payload: ProviderInput) -> dict:
    current = provider_row(provider_id)
    if not current:
        raise HTTPException(404, "模型通道不存在")
    return save_provider_record(provider_id, payload, current)


@app.delete("/api/providers/{provider_id}")
def delete_provider(provider_id: str) -> dict:
    with db() as conn:
        current = conn.execute("SELECT is_default FROM model_providers WHERE id=?", (provider_id,)).fetchone()
        if not current:
            raise HTTPException(404, "模型通道不存在")
        conn.execute("DELETE FROM model_providers WHERE id=?", (provider_id,))
        if current["is_default"]:
            next_provider = conn.execute("SELECT id FROM model_providers WHERE enabled=1 ORDER BY created_at LIMIT 1").fetchone()
            if next_provider:
                conn.execute("UPDATE model_providers SET is_default=1 WHERE id=?", (next_provider["id"],))
    return {"status": "deleted"}


@app.post("/api/providers/{provider_id}/activate")
def activate_provider(provider_id: str) -> dict:
    provider = provider_row(provider_id)
    if not provider:
        raise HTTPException(404, "模型通道不存在")
    if not provider["enabled"]:
        raise HTTPException(409, "请先启用模型通道")
    if not provider.get("encrypted_api_key"):
        raise HTTPException(409, "请先填写 API Key")
    with db() as conn:
        conn.execute("UPDATE model_providers SET is_default=0")
        conn.execute("UPDATE model_providers SET is_default=1,updated_at=? WHERE id=?", (now(), provider_id))
    return provider_public(provider_row(provider_id)) or {}


@app.post("/api/providers/{provider_id}/test")
def test_provider(provider_id: str) -> dict:
    log_event(logger, "INFO", "provider.health_check.started", provider_id=provider_id)
    started = time.perf_counter()
    try:
        answer = call_provider("只回复：模型通道可用", provider_id, purpose="provider_health_check")
    except HTTPException as exc:
        with db() as conn:
            conn.execute("UPDATE model_providers SET status='failed',last_test_at=?,updated_at=? WHERE id=?", (now(), now(), provider_id))
        log_exception(logger, "provider.health_check.failed", exc, provider_id=provider_id, detail=exc.detail)
        raise
    latency_ms = round((time.perf_counter() - started) * 1000)
    with db() as conn:
        conn.execute(
            "UPDATE model_providers SET status='online',latency_ms=?,last_test_at=?,updated_at=? WHERE id=?",
            (latency_ms, now(), now(), provider_id),
        )
    log_event(logger, "INFO", "provider.health_check.completed", provider_id=provider_id, latency_ms=latency_ms)
    return {"status": "online", "message": answer, "latency_ms": latency_ms}


@app.put("/api/settings/provider")
def save_legacy_provider(payload: ProviderInput) -> dict:
    current = provider_row()
    if current:
        return save_provider_record(current["id"], payload, current)
    return create_provider(payload)


@app.post("/api/settings/provider/test")
def test_legacy_provider() -> dict:
    provider = provider_row()
    if not provider:
        raise HTTPException(409, "请先在设置中配置模型供应商")
    return test_provider(provider["id"])


@app.post("/api/tasks")
def create_task(payload: TaskInput) -> dict:
    task = {
        "id": uuid4().hex[:10],
        **payload.model_dump(),
        "status": "queued",
        "created_at": now(),
        "report": None,
        "agent_state": "{}",
        "agent_error": "",
        "report_anchor": "",
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tasks(id,title,target,objective,status,created_at,report,skill_name,lookback_days,agent_state,agent_error,report_anchor)
            VALUES(:id,:title,:target,:objective,:status,:created_at,:report,:skill_name,:lookback_days,:agent_state,:agent_error,:report_anchor)
            """,
            task,
        )
    log_event(logger, "INFO", "research_task.created", task_id=task["id"], target=task["target"], skill_name=task["skill_name"], lookback_days=task["lookback_days"])
    return task


EVIDENCE_LAYERS = ("local_source_snapshots", "akshare", "http_requests", "playwright", "model_knowledge")


def record_agent_event(task_id: str, event_type: str, detail: dict | str) -> None:
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    with db() as conn:
        conn.execute(
            "INSERT INTO agent_events(id,task_id,event_type,detail,created_at) VALUES(?,?,?,?,?)",
            (uuid4().hex[:12], task_id, event_type, text[:6000], now()),
        )
    log_event(logger, "INFO", f"agent.{event_type}", task_id=task_id, detail=detail)


def save_agent_state(task_id: str, state: dict, status: str | None = None, error: str = "") -> None:
    with db() as conn:
        if status:
            conn.execute(
                "UPDATE tasks SET agent_state=?,status=?,agent_error=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), status, error, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET agent_state=?,agent_error=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), error, task_id),
            )


def channel_ids_for_layer(layer: str) -> list[str]:
    modes = {
        "akshare": ("akshare",),
        "http_requests": ("requests", "industry_news"),
        "playwright": ("playwright",),
    }.get(layer)
    if not modes:
        return []
    placeholders = ",".join("?" for _ in modes)
    with db() as conn:
        return [
            row["id"]
            for row in conn.execute(
                f"""
                SELECT id FROM channels
                WHERE collection_mode IN ({placeholders}) AND status='online' AND research_enabled=1
                ORDER BY builtin DESC,updated_at
                """,
                modes,
            )
        ]


def research_refresh_channel_ids() -> list[str]:
    with db() as conn:
        return [
            row["id"]
            for row in conn.execute(
                """
                SELECT id FROM channels
                WHERE status='online' AND collection_mode<>'manual'
                ORDER BY builtin DESC,updated_at
                """
            )
        ]


def stale_general_channel_ids(channel_ids: list[str]) -> list[str]:
    current = datetime.now(timezone.utc).astimezone()
    with db() as conn:
        return [
            channel_id
            for channel_id in channel_ids
            if not (reserved := latest_reserved_at(conn, channel_id, ""))
            or current - reserved >= MIN_COLLECTION_INTERVAL
        ]


def refresh_general_sources_before_research(task: dict, state: dict) -> dict | None:
    channel_ids = research_refresh_channel_ids()
    stale_channel_ids = stale_general_channel_ids(channel_ids)
    if not stale_channel_ids:
        return None
    job = create_source_job(
        SourceJobInput(
            action="collect",
            channel_ids=channel_ids,
            lookback_days=task["lookback_days"],
            report_title=f"{task['target']} · 个股分析前置全量信源刷新",
            skill_name=task["skill_name"],
            parent_task_id=task["id"],
            evidence_layer="local_source_snapshots",
        )
    )
    record_agent_event(
        task["id"],
        "general_source_refresh_created",
        {"job_id": job["id"], "channel_ids": channel_ids, "stale_channel_ids": stale_channel_ids, "status": job["status"]},
    )
    if job["status"] == "deduplicated":
        return None
    save_agent_state(task["id"], state, "evidence_queued")
    return {"id": task["id"], "status": "evidence_queued", "job_id": job["id"], "evidence_layer": "local_source_snapshots"}


def task_snapshot_context(lookback_days: int, scope_key: str) -> tuple[str, str, str]:
    with db() as conn:
        channel_ids = [row["id"] for row in conn.execute("SELECT id FROM channels ORDER BY builtin DESC,updated_at")]
    if not channel_ids:
        return "无已配置信源", "无本地快照", "当前没有已配置信源，也没有可传递给模型的本地快照。"
    try:
        return local_snapshot_context(
            channel_ids,
            lookback_days,
            research_scope_key=scope_key,
            include_source_reports=True,
        )
    except HTTPException:
        return "无本地快照", "无本地快照", "当前没有可用的本地信源快照。不能形成事实判断，只能请求下一层证据。"


def completed_evidence_layers(task_id: str, state: dict) -> set[str]:
    completed = set(state.get("completed_layers", []))
    completed.add("local_source_snapshots")
    with db() as conn:
        rows = conn.execute(
            """
            SELECT evidence_layer FROM source_collection_jobs
            WHERE parent_task_id=? AND evidence_layer<>''
              AND status IN ('completed','partial_completed','review','partial_review','deduplicated')
            """,
            (task_id,),
        )
        completed.update(row["evidence_layer"] for row in rows)
    return completed


def advance_analysis_task(task_id: str) -> dict:
    log_event(logger, "INFO", "agent.advance.started", task_id=task_id)
    with db() as conn:
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task_row:
        raise HTTPException(404, "任务不存在")
    task = dict(task_row)
    state = json.loads(task.get("agent_state") or "{}")
    state.setdefault("completed_layers", [])
    state.setdefault("steps", 0)
    refresh_job = refresh_general_sources_before_research(task, state)
    if refresh_job:
        return refresh_job
    for _ in range(8):
        completed = completed_evidence_layers(task_id, state)
        state["completed_layers"] = [layer for layer in EVIDENCE_LAYERS if layer in completed]
        pending = [layer for layer in EVIDENCE_LAYERS[1:] if layer not in completed]
        next_layer = pending[0] if pending else "final_report"
        anchor, window_start, evidence = task_snapshot_context(task["lookback_days"], task["target"])
        state["steps"] += 1
        prompt = build_agent_step_prompt(
            target=task["target"],
            objective=task["objective"],
            skill_name=task["skill_name"],
            window_start=window_start,
            anchor=anchor,
            evidence=evidence,
            completed_layers=state["completed_layers"],
            next_layer=next_layer,
            allow_model_knowledge="model_knowledge" in completed,
        )
        decision = call_provider_structured(prompt, AgentDecision, purpose="stock_agent_decision").model_dump()
        record_agent_event(task_id, "model_decision", {"next_allowed_layer": next_layer, "decision": decision})
        if decision["decision"] == "final":
            if decision.get("used_model_knowledge") and "model_knowledge" not in completed:
                raise HTTPException(409, "模型试图提前使用自身知识库，已被红线阻止")
            report = require_or_repair_html_report(decision["report"], purpose="stock_report")
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET status='review',report=?,report_anchor=?,agent_state=?,agent_error='' WHERE id=?",
                    (report, anchor, json.dumps(state, ensure_ascii=False), task_id),
                )
            record_agent_event(task_id, "report_ready", {"anchor": anchor})
            log_event(logger, "INFO", "agent.advance.completed", task_id=task_id, status="review", report_chars=len(report), anchor=anchor)
            return {"id": task_id, "status": "review", "report": report, "report_anchor": anchor}
        requested_layer = decision["next_source"]
        if next_layer == "final_report":
            raise HTTPException(409, "证据链已结束，但模型没有输出最终报告")
        if requested_layer != next_layer or requested_layer == "final_report":
            raise HTTPException(409, f"模型请求了不允许的证据层: {requested_layer}，当前只允许: {next_layer}")
        if requested_layer == "model_knowledge":
            state["completed_layers"].append("model_knowledge")
            save_agent_state(task_id, state, "analyzing")
            record_agent_event(task_id, "model_knowledge_enabled", decision.get("reason", ""))
            continue
        channel_ids = channel_ids_for_layer(requested_layer)
        if not channel_ids:
            state["completed_layers"].append(requested_layer)
            save_agent_state(task_id, state, "analyzing")
            record_agent_event(task_id, "layer_unavailable", {"layer": requested_layer})
            continue
        job = create_source_job(
            SourceJobInput(
                action="collect",
                channel_ids=channel_ids,
                lookback_days=task["lookback_days"],
                report_title=f"{task['target']} · {requested_layer} 证据采集",
                skill_name=task["skill_name"],
                parent_task_id=task_id,
                query=task["target"],
                evidence_layer=requested_layer,
            )
        )
        record_agent_event(task_id, "collection_job_created", {"layer": requested_layer, "job_id": job["id"], "status": job["status"]})
        if job["status"] == "deduplicated":
            state["completed_layers"].append(requested_layer)
            save_agent_state(task_id, state, "analyzing")
            continue
        save_agent_state(task_id, state, "evidence_queued")
        return {"id": task_id, "status": "evidence_queued", "job_id": job["id"], "evidence_layer": requested_layer}
    raise HTTPException(409, "Agent 超过单次最大证据推进步数，请检查模型输出和渠道配置")


@app.post("/api/tasks/{task_id}/analyze")
def analyze_stock_task(task_id: str) -> dict:
    try:
        return advance_analysis_task(task_id)
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        with db() as conn:
            conn.execute("UPDATE tasks SET status='agent_failed',agent_error=? WHERE id=?", (str(detail)[:1200], task_id))
        record_agent_event(task_id, "agent_failed", str(detail))
        log_exception(logger, "agent.advance.failed", exc, task_id=task_id, detail=detail)
        raise


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: str) -> dict:
    return analyze_stock_task(task_id)


def cancel_queued_task_jobs(conn: sqlite3.Connection, task_id: str) -> int:
    cursor = conn.execute(
        """
        UPDATE source_collection_jobs
        SET status='cancelled',completed_at=?,error='任务已由用户取消'
        WHERE parent_task_id=? AND status='queued'
        """,
        (now(), task_id),
    )
    return cursor.rowcount


@app.post("/api/tasks/{task_id}/reset")
def reset_task(task_id: str) -> dict:
    with db() as conn:
        task = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        running = conn.execute(
            "SELECT id FROM source_collection_jobs WHERE parent_task_id=? AND status='running' LIMIT 1",
            (task_id,),
        ).fetchone()
        if running:
            raise HTTPException(409, "任务仍有采集器正在执行，请等待当前信源窗口完成后再重置")
        cancelled_jobs = cancel_queued_task_jobs(conn, task_id)
        conn.execute(
            """
            UPDATE tasks
            SET status='queued',report=NULL,report_anchor='',agent_state='{}',agent_error=''
            WHERE id=?
            """,
            (task_id,),
        )
        conn.execute("DELETE FROM agent_events WHERE task_id=?", (task_id,))
    return {"status": "queued", "cancelled_jobs": cancelled_jobs}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str) -> dict:
    with db() as conn:
        task = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        active = conn.execute(
            """
            SELECT id FROM source_collection_jobs
            WHERE parent_task_id=? AND status IN ('queued','running','generating','generating_report')
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if active:
            raise HTTPException(409, "任务仍有采集器正在执行，请等待当前信源窗口完成后再删除")
        child_job_ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM source_collection_jobs WHERE parent_task_id=?", (task_id,))
        ]
        if child_job_ids:
            child_marks = ",".join("?" for _ in child_job_ids)
            conn.execute(f"DELETE FROM source_job_snapshots WHERE job_id IN ({child_marks})", child_job_ids)
            conn.execute(f"DELETE FROM source_collection_runs WHERE job_id IN ({child_marks})", child_job_ids)
            conn.execute(f"DELETE FROM source_collection_jobs WHERE id IN ({child_marks})", child_job_ids)
        conn.execute("DELETE FROM agent_events WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"status": "deleted", "cancelled_jobs": 0, "deleted_source_jobs": len(child_job_ids)}


collection_worker.on_evidence_ready = advance_analysis_task
