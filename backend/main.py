from __future__ import annotations

import json
import hashlib
import importlib.util
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import requests
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.agent_runtime import build_agent_step_prompt, parse_agent_decision
from backend.worker import CollectionWorker

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "workbench.db"
KEY_PATH = DATA_DIR / "local.key"
SKILLS_DIR = ROOT / "skills"
CODEX_POLICY_PATH = ROOT / "config" / "codex-policy.toml"
RED_LINES_PATH = ROOT / "config" / "research-red-lines.toml"
MIN_COLLECTION_INTERVAL = timedelta(minutes=15)
REPORT_DEDUP_INTERVAL = timedelta(minutes=2)
MASKED_SECRET = "****************"
MARKET_DATA_DEFAULT_CONFIG = {
    "adapter": "market_data_aggregate",
    "enable_akshare": True,
    "enable_baostock": True,
    "enable_tushare": True,
    "tushare_token": "",
    "component_timeout_seconds": 35,
}
CANONICAL_CHANNEL_NAMES = {
    "akshare": "AkShare 市场数据",
    "industry-news": "产业趋势公开资讯",
    "zsxq": "知识星球",
    "web-rumors": "MX 小作文频道",
    "146aa28e21": "TG 小作文频道",
}
DATA_DIR.mkdir(exist_ok=True)

app = FastAPI(title="A股成长猎手本地服务", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "null"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProviderInput(BaseModel):
    name: str = "DeepSeek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = Field(default="", repr=False)
    protocol: Literal["openai_chat_completions"] = "openai_chat_completions"
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
    collection_mode: Literal["akshare", "industry_news", "requests", "playwright", "manual"] = "playwright"
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


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
              UNIQUE(channel_id, source_url, occurred_at)
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
              UNIQUE(channel_id,item_key)
            );
            CREATE TABLE IF NOT EXISTS source_job_snapshots (
              job_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,
              PRIMARY KEY(job_id,snapshot_id)
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
                "online",
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
            SET name=?,type=?,collection_mode='industry_news',status='online',notes=?,
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


init_db()

TOOLS = [
    {"id": "akshare", "name": "AkShare 市场数据", "kind": "python", "priority": 1, "status": "ready", "detail": "行情、财务、股东与公告基础数据"},
    {"id": "requests", "name": "HTTP 请求采集", "kind": "python", "priority": 2, "status": "ready", "detail": "公开接口与结构化网页请求"},
    {"id": "playwright", "name": "Playwright 持久化浏览器", "kind": "browser", "priority": 3, "status": "setup", "detail": "登录态渠道、动态网页与强反爬页面"},
    {"id": "manual", "name": "其他人工补充渠道", "kind": "fallback", "priority": 4, "status": "standby", "detail": "保留来源说明，进入报告审查"},
]

TOOLS[0] = {
    "id": "akshare",
    "name": "A股市场数据聚合",
    "kind": "python",
    "priority": 1,
    "status": "ready",
    "detail": "AkShare 优先，BaoStock 自动后备，TuShare 配置 token 后参与；组件独立限时，单个上游异常不会阻塞全部市场数据。",
}
TOOLS[1] = {
    "id": "requests",
    "name": "HTTP 请求与产业资讯采集",
    "kind": "python",
    "priority": 2,
    "status": "ready",
    "detail": "公开接口、结构化网页与产业趋势公开资讯；东方财富请求串行限流，个股补证可追加公司资料、新闻和巨潮公告。",
}

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
            "research_tasks_may_read_general_snapshots",
            "research_tasks_may_collect_scoped_evidence",
        )
    ):
        raise RuntimeError("Invalid red-line policy: source reports and stock research workflows must remain isolated")
    return policy


def require_html_report(report: str) -> str:
    text = (report or "").strip()
    if (
        text.startswith("```")
        or not re.search(r"<html(?:\s|>)", text, flags=re.IGNORECASE)
        or not re.search(r"<body(?:\s|>)", text, flags=re.IGNORECASE)
        or not re.search(r"</body\s*>", text, flags=re.IGNORECASE)
        or not re.search(r"</html\s*>", text, flags=re.IGNORECASE)
    ):
        raise HTTPException(502, "模型返回的报告不是完整 HTML 文档。已按核心红线拒绝保存，请重试生成。")
    return text


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
6. 报告重点是产业趋势、政策、供需、技术迭代、产能、订单、价格、上下游和公司公告。严禁输出技术面分析、交易策略或买卖点。"""


def provider_endpoint(provider: dict) -> str:
    base_url = provider["base_url"].rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return base_url + "/chat/completions"
    return base_url + "/v1/chat/completions"


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


def call_provider(prompt: str, provider_id: str = "", system_prompt: str = "") -> str:
    research_red_lines()
    provider = provider_row(provider_id)
    if not provider or not provider.get("encrypted_api_key"):
        raise HTTPException(409, "请先在设置中配置模型供应商")
    if not provider["enabled"]:
        raise HTTPException(409, "模型通道已停用")
    api_key = cipher().decrypt(provider["encrypted_api_key"].encode()).decode()
    url = provider_endpoint(provider)
    payload = {
        "model": provider["model"],
        "messages": [
            {"role": "system", "content": system_prompt or analysis_system_prompt()},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    payload.update(provider.get("extra_body") or {})
    try:
        response = requests.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=90)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except requests.RequestException as exc:
        raise HTTPException(502, f"模型供应商请求失败: {exc}") from exc


def parse_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Model response must be a JSON object")
    return value


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
    payload = parse_json_object(call_provider(prompt, system_prompt=normalization_system_prompt()))
    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        raise ValueError("Model response items must be a list")
    top_score = clamp_quality_score(payload.get("quality_score"), 0)
    notes = str(payload.get("notes", "") or "")[:1_000]
    items: list[dict] = []
    for raw_item in raw_items[:100]:
        if not isinstance(raw_item, dict):
            continue
        content = str(raw_item.get("content", "") or "").strip()
        if not content:
            continue
        occurred_at = normalized_occurred_at(raw_item.get("occurred_at"), snapshot["occurred_at"] or snapshot["collected_at"])
        source_url = str(raw_item.get("source_url", "") or snapshot["source_url"] or "").strip()
        title = str(raw_item.get("title", "") or "").strip()
        item_key = str(raw_item.get("item_key", "") or "").strip()
        attachments = raw_item.get("attachments", [])
        metadata = raw_item.get("metadata", {})
        if not isinstance(attachments, list):
            attachments = []
        if not isinstance(metadata, dict):
            metadata = {}
        if not item_key:
            item_key = stable_item_key(snapshot["channel_id"], source_url, occurred_at, title, content)
        items.append(
            {
                "item_key": item_key[:255],
                "occurred_at": occurred_at,
                "author": str(raw_item.get("author", "") or "")[:255],
                "title": title[:500],
                "content": content,
                "source_url": source_url[:2_000],
                "attachments": attachments,
                "metadata": metadata,
                "quality_score": clamp_quality_score(raw_item.get("quality_score"), top_score),
                "normalization_mode": mode,
            }
        )
    if not items:
        raise ValueError("Model did not extract any content items")
    return items, notes


def normalize_snapshot_record(snapshot_id: str, force: bool = False) -> dict:
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
        if strategy == "fixed":
            items, notes = fixed_normalized_items(snapshot)
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
                  attachments,metadata,quality_score,normalization_mode,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    return {
        "snapshot_id": snapshot_id,
        "status": status,
        "parsed_item_count": len(items),
        "stored_item_count": stored_count,
        "average_quality": average_quality,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "alphadesk-local-api", "version": app.version, "time": now()}


def channel_names_for_ids(channel_ids: list[str], names: dict[str, str] | None = None) -> list[str]:
    if names is None:
        with db() as conn:
            names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM channels")}
    return [names.get(channel_id, CANONICAL_CHANNEL_NAMES.get(channel_id, channel_id)) for channel_id in channel_ids]


@app.get("/api/dashboard")
def dashboard() -> dict:
    with db() as conn:
        tasks = [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 8")]
        source_jobs = [dict(row) for row in conn.execute("SELECT * FROM source_collection_jobs ORDER BY created_at DESC LIMIT 80")]
        channels = [dict(row) for row in conn.execute("SELECT * FROM channels ORDER BY builtin DESC, updated_at")]
    for channel in channels:
        channel["group_ids"] = json.loads(channel.get("group_ids") or "[]")
        channel["profile_exists"] = browser_profile(channel["id"]).exists()
        if channel["id"] == "akshare":
            channel["market_data_config"] = market_data_config_public()
    channel_names = {channel["id"]: channel["name"] for channel in channels}
    for job in source_jobs:
        job["channel_ids"] = json.loads(job["channel_ids"])
        job["channel_names"] = channel_names_for_ids(job["channel_ids"], channel_names)
        job["windows"] = json.loads(job["windows"])
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
        jobs = [dict(row) for row in conn.execute("SELECT * FROM source_collection_jobs ORDER BY created_at DESC LIMIT 80")]
        channel_names = {row["id"]: row["name"] for row in conn.execute("SELECT id,name FROM channels")}
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
    for job in jobs:
        job["channel_ids"] = json.loads(job["channel_ids"])
        job["channel_names"] = channel_names_for_ids(job["channel_ids"], channel_names)
        job["windows"] = json.loads(job["windows"])
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
    return {"scope": scope, "deleted": {key: before[key] - after[key] for key in before}, "inventory": after}


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
                    cursor = conn.execute(f"DELETE FROM source_collection_jobs WHERE id IN ({child_marks})", child_job_ids)
                    deleted_source_jobs += cursor.rowcount
                conn.execute(f"DELETE FROM agent_events WHERE task_id IN ({task_marks})", task_ids)
                cursor = conn.execute(f"DELETE FROM tasks WHERE id IN ({task_marks})", task_ids)
                deleted_research_tasks = cursor.rowcount
        if scope in ("source-jobs", "all"):
            conn.execute("DELETE FROM source_job_snapshots")
            cursor = conn.execute("DELETE FROM source_collection_jobs")
            deleted_source_jobs += cursor.rowcount
    return {
        "scope": scope,
        "deleted_research_tasks": deleted_research_tasks,
        "deleted_source_jobs": deleted_source_jobs,
    }


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


@app.post("/api/channels/{channel_id}/import-mx-har")
def import_mx_har(channel_id: str, payload: MxHarImportInput) -> dict:
    if channel_id != "web-rumors":
        raise HTTPException(409, "HAR import is only available for the MX source channel")
    if len(payload.har_text.encode("utf-8")) > 32 * 1024 * 1024:
        raise HTTPException(413, "HAR file is too large; keep only the MX login and message-list requests")
    try:
        from backend.import_mx_har import import_har_text

        return import_har_text(payload.har_text)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)[:600]) from exc


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
    subprocess.Popen(
        [sys.executable, str(ROOT / "backend" / "browser_session.py"), "login", "--profile", str(profile), "--url", channel["url"]],
        cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return {"status": "opened", "message": "登录窗口已打开。扫码或登录完成后关闭窗口，再点击检查状态。"}


@app.post("/api/channels/{channel_id}/check")
def check_channel(channel_id: str) -> dict:
    with db() as conn:
        channel = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if channel_id == "akshare" and channel:
        result = market_data_component_status()
        with db() as conn:
            conn.execute(
                "UPDATE channels SET status=?,last_check=?,updated_at=? WHERE id=?",
                (result["status"], result["checked_at"], result["checked_at"], channel_id),
            )
        return result
    if channel:
        request_config = channel_request_config(channel_id)
        if request_config.get("adapter") == "mx_authorized_request_replay":
            return check_mx_channel(channel_id, request_config)
    if not channel:
        raise HTTPException(404, "渠道不存在")
    if channel["collection_mode"] != "playwright":
        return {"status": channel["status"], "message": "该渠道无需浏览器登录检查"}
    validation_url = channel["validation_url"] or channel["url"]
    if not validation_url:
        raise HTTPException(409, "请先填写检查 URL 或渠道入口 URL")
    profile = browser_profile(channel_id)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "backend" / "browser_session.py"), "check",
                "--profile", str(profile), "--url", validation_url,
                "--success-url-contains", channel["success_url_contains"],
                "--success-selector", channel["success_selector"],
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW,
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
    return {"status": status, "message": detail["message"], "checked_at": checked_at, "final_url": detail["final_url"]}


def refresh_browser_channel_states() -> None:
    with db() as conn:
        channels = [dict(row) for row in conn.execute("SELECT id FROM channels WHERE collection_mode='playwright' OR id IN ('web-rumors','akshare')")]
    for channel in channels:
        if channel["id"] in ("web-rumors", "akshare") or browser_profile(channel["id"]).exists():
            try:
                check_channel(channel["id"])
            except HTTPException:
                checked_at = now()
                with db() as conn:
                    conn.execute("UPDATE channels SET status='offline',last_check=?,updated_at=? WHERE id=?", (checked_at, checked_at, channel["id"]))


@app.post("/api/channels/check-all")
def check_all_channels() -> dict:
    refresh_browser_channel_states()
    return {"status": "ok", "message": "已完成浏览器渠道巡检"}


@app.on_event("startup")
def schedule_startup_channel_check() -> None:
    threading.Thread(target=refresh_browser_channel_states, daemon=True).start()


def iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def latest_reserved_at(conn: sqlite3.Connection, channel_id: str, scope_key: str = "") -> datetime | None:
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
        if (row["query"] or "") != scope_key:
            continue
        if row["status"] in ("failed", "report_failed"):
            attempted_at = row["completed_at"] or row["started_at"]
            if not attempted_at or current - datetime.fromisoformat(attempted_at) >= MIN_COLLECTION_INTERVAL:
                continue
        for window in json.loads(row["windows"]):
            if window["channel_id"] == channel_id and window.get("window_end"):
                values.append(window["window_end"])
    return max((datetime.fromisoformat(value) for value in values), default=None)


def local_snapshot_context(
    channel_ids: list[str],
    lookback_days: int,
    *,
    general_snapshots_only: bool = False,
) -> tuple[str, str, str]:
    placeholders = ",".join("?" for _ in channel_ids)
    general_filter = " AND scope_type='general'" if general_snapshots_only else ""
    normalized_general_filter = " AND s.scope_type='general'" if general_snapshots_only else ""
    with db() as conn:
        anchor_row = conn.execute(
            f"SELECT MAX(collected_at) AS anchor FROM source_snapshots WHERE channel_id IN ({placeholders}){general_filter}",
            channel_ids,
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
                SELECT n.channel_id,n.occurred_at,n.author,n.title,n.content,n.source_url,n.quality_score,n.normalization_mode
                FROM normalized_source_items n
                JOIN source_snapshots s ON s.id=n.snapshot_id
                WHERE n.channel_id IN ({placeholders}) AND n.occurred_at BETWEEN ? AND ?{normalized_general_filter}
                ORDER BY n.occurred_at DESC LIMIT 500
                """,
                [*channel_ids, window_start, anchor],
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
                [*channel_ids, window_start, anchor],
            )
        ]
    if not normalized_items and not snapshots:
        raise HTTPException(409, "本地快照存在，但所选时间窗口内没有可用于报告的内容。")
    normalized_chunks = [
        (
            f"[normalized:{item['channel_id']}] {item['occurred_at']} quality={item['quality_score']} "
            f"mode={item['normalization_mode']} author={item['author']} title={item['title']} {item['source_url']}\n"
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
    if normalized_chunks:
        chunks = [
            (
                f"{chunk}"
            )
            for chunk in [*normalized_chunks, *raw_chunks]
        ]
    else:
        chunks = raw_chunks
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
仅输出完整 HTML 文档，必须包含 <html>、<head> 和 <body>。可以使用内联 CSS 优化排版。
严禁输出 Markdown，严禁使用 Markdown 代码围栏，严禁输出 HTML 文档以外的解释文字。
报告名称：{payload.report_title}
数据锚点：{anchor}
报告窗口：{window_start} 至 {anchor}
信源：{", ".join(source_names)}

通用信源快照：
{context}"""
    return require_html_report(call_provider(prompt, system_prompt=source_report_system_prompt())), anchor


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


@app.on_event("startup")
def start_collection_worker() -> None:
    collection_worker.start()


@app.on_event("shutdown")
def stop_collection_worker() -> None:
    collection_worker.stop()


def insert_source_job(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        """
        INSERT INTO source_collection_jobs(id,action,channel_ids,windows,lookback_days,skill_name,report_title,status,created_at,report,report_anchor,parent_task_id,query,evidence_layer)
        VALUES(:id,:action,:channel_ids,:windows,:lookback_days,:skill_name,:report_title,:status,:created_at,:report,:report_anchor,:parent_task_id,:query,:evidence_layer)
        """,
        job,
    )


def source_job_response(job: dict, **extra: object) -> dict:
    result = {**job}
    if isinstance(result.get("channel_ids"), str):
        result["channel_ids"] = json.loads(result["channel_ids"])
    if isinstance(result.get("windows"), str):
        result["windows"] = json.loads(result["windows"])
    result["channel_names"] = channel_names_for_ids(result.get("channel_ids") or [])
    return {**result, **extra}


@app.post("/api/source-jobs")
def create_source_job(payload: SourceJobInput) -> dict:
    if payload.action in ("collect_report", "report"):
        ensure_general_source_report(payload)
    if payload.action == "report":
        channel_ids = sorted(set(payload.channel_ids))
        channel_ids_json = json.dumps(channel_ids, ensure_ascii=False)
        created_at = now()
        job = {
            "id": uuid4().hex[:10], "action": payload.action, "channel_ids": channel_ids_json,
            "windows": "[]", "lookback_days": payload.lookback_days, "skill_name": payload.skill_name,
            "report_title": payload.report_title, "status": "generating", "created_at": created_at,
            "report": None, "report_anchor": "", "parent_task_id": payload.parent_task_id,
            "query": payload.query, "evidence_layer": payload.evidence_layer,
        }
        cutoff = iso(datetime.fromisoformat(created_at) - REPORT_DEDUP_INTERVAL)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM source_collection_jobs
                WHERE action='report' AND channel_ids=? AND lookback_days=? AND skill_name=? AND report_title=?
                  AND parent_task_id=? AND query=? AND evidence_layer=? AND created_at>=?
                  AND status IN ('generating','review')
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    channel_ids_json,
                    payload.lookback_days,
                    payload.skill_name,
                    payload.report_title,
                    payload.parent_task_id,
                    payload.query,
                    payload.evidence_layer,
                    cutoff,
                ),
            ).fetchone()
            if existing:
                return source_job_response(dict(existing), deduplicated=True)
            insert_source_job(conn, job)
        try:
            report, anchor = generate_source_report(payload)
        except Exception as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            with db() as conn:
                conn.execute(
                    "UPDATE source_collection_jobs SET status='report_failed',error=?,completed_at=? WHERE id=?",
                    (error[:1200], now(), job["id"]),
                )
            raise
        completed_at = now()
        with db() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status='review',report=?,report_anchor=?,completed_at=? WHERE id=?",
                (report, anchor, completed_at, job["id"]),
            )
        job.update({"status": "review", "report": report, "report_anchor": anchor, "completed_at": completed_at})
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
                reserved = latest_reserved_at(conn, channel_id, payload.query)
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
            "query": payload.query, "evidence_layer": payload.evidence_layer,
        }
    with db() as conn:
        insert_source_job(conn, job)
    if job["action"] == "collect_report" and job["status"] == "deduplicated":
        try:
            report, anchor = report_after_collection(job)
        except Exception as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            completed_at = now()
            with db() as conn:
                conn.execute(
                    "UPDATE source_collection_jobs SET status='report_failed',error=?,completed_at=? WHERE id=?",
                    (error[:1200], completed_at, job["id"]),
                )
            job.update({"status": "report_failed", "error": error[:1200], "completed_at": completed_at})
            return source_job_response(job)
        completed_at = now()
        with db() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status='review',report=?,report_anchor=?,completed_at=? WHERE id=?",
                (report, anchor, completed_at, job["id"]),
            )
        job.update({"status": "review", "report": report, "report_anchor": anchor, "completed_at": completed_at})
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
        missing_channels = allowed_channels - submitted_channels
        if missing_channels:
            raise HTTPException(409, f"以下信源没有真实快照，不能推进水位: {', '.join(sorted(missing_channels))}")
        channel_windows = {window["channel_id"]: window for window in windows}
        for item in payload.snapshots:
            window = channel_windows[item.channel_id]
            if not datetime.fromisoformat(window["window_start"]) <= item.occurred_at.astimezone() <= datetime.fromisoformat(window["window_end"]):
                raise HTTPException(409, f"快照时间戳不属于当前采集窗口: {item.channel_id}")
        collected_at = now()
        scope_type = "research" if job["parent_task_id"] or job["query"] or job["evidence_layer"] else "general"
        scope_key = job["query"] if scope_type == "research" else ""
        inserted = 0
        inserted_channels: set[str] = set()
        inserted_snapshot_ids: list[str] = []
        for snapshot in payload.snapshots:
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
                inserted_channels.add(snapshot.channel_id)
                inserted_snapshot_ids.append(snapshot_id)
                conn.execute(
                    "INSERT OR IGNORE INTO source_job_snapshots(job_id,snapshot_id) VALUES(?,?)",
                    (job_id, snapshot_id),
                )
        missing_new_snapshots = allowed_channels - inserted_channels
        if missing_new_snapshots:
            raise HTTPException(409, f"以下信源没有新增快照，不能推进水位: {', '.join(sorted(missing_new_snapshots))}")
        for window in windows:
            conn.execute(
                "INSERT OR REPLACE INTO source_collection_watermarks_v2(channel_id,scope_key,last_success_at) VALUES(?,?,?)",
                (window["channel_id"], job["query"] or "", window["window_end"]),
            )
        next_status = "generating_report" if job["action"] == "collect_report" else "completed"
        conn.execute(
            "UPDATE source_collection_jobs SET status=?,snapshot_count=?,completed_at=? WHERE id=?",
            (next_status, inserted, collected_at, job_id),
        )
    for snapshot_id in inserted_snapshot_ids:
        normalize_snapshot_record(snapshot_id)
    report = None
    if job["action"] == "collect_report":
        request = SourceJobInput(
            action="report",
            channel_ids=json.loads(job["channel_ids"]),
            lookback_days=job["lookback_days"],
            report_title=job["report_title"],
            skill_name=job["skill_name"],
        )
        report, anchor = generate_source_report(request)
        with db() as conn:
            conn.execute("UPDATE source_collection_jobs SET status='review',report=?,report_anchor=? WHERE id=?", (report, anchor, job_id))
    if job["parent_task_id"] and collection_worker.on_evidence_ready:
        with db() as conn:
            conn.execute("UPDATE tasks SET status='evidence_ready' WHERE id=?", (job["parent_task_id"],))
        collection_worker.on_evidence_ready(job["parent_task_id"])
    return {"status": "review" if report else "completed", "snapshot_count": inserted, "report": report}


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
        try:
            report, anchor = generate_source_report(payload)
        except Exception as exc:
            with db() as conn:
                conn.execute("UPDATE source_collection_jobs SET error=? WHERE id=?", (str(exc)[:1200], job_id))
            raise
        with db() as conn:
            conn.execute(
                "UPDATE source_collection_jobs SET status='review',report=?,report_anchor=?,error='' WHERE id=?",
                (report, anchor, job_id),
            )
        return {"status": "review", "report": report, "report_anchor": anchor}
    return {"status": "queued"}


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
    started = time.perf_counter()
    try:
        answer = call_provider("只回复：模型通道可用", provider_id)
    except HTTPException:
        with db() as conn:
            conn.execute("UPDATE model_providers SET status='failed',last_test_at=?,updated_at=? WHERE id=?", (now(), now(), provider_id))
        raise
    latency_ms = round((time.perf_counter() - started) * 1000)
    with db() as conn:
        conn.execute(
            "UPDATE model_providers SET status='online',latency_ms=?,last_test_at=?,updated_at=? WHERE id=?",
            (latency_ms, now(), now(), provider_id),
        )
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
    return task


EVIDENCE_LAYERS = ("local_source_snapshots", "akshare", "http_requests", "playwright", "model_knowledge")


def record_agent_event(task_id: str, event_type: str, detail: dict | str) -> None:
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    with db() as conn:
        conn.execute(
            "INSERT INTO agent_events(id,task_id,event_type,detail,created_at) VALUES(?,?,?,?,?)",
            (uuid4().hex[:12], task_id, event_type, text[:6000], now()),
        )


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


def task_snapshot_context(lookback_days: int) -> tuple[str, str, str]:
    with db() as conn:
        channel_ids = [row["id"] for row in conn.execute("SELECT id FROM channels WHERE status='online'")]
    if not channel_ids:
        return "无在线信源", "无本地快照", "当前没有在线信源，也没有可传递给模型的本地快照。"
    try:
        return local_snapshot_context(channel_ids, lookback_days)
    except HTTPException:
        return "无本地快照", "无本地快照", "当前没有可用的本地信源快照。不能形成事实判断，只能请求下一层证据。"


def completed_evidence_layers(task_id: str, state: dict) -> set[str]:
    completed = set(state.get("completed_layers", []))
    completed.add("local_source_snapshots")
    with db() as conn:
        rows = conn.execute(
            """
            SELECT evidence_layer FROM source_collection_jobs
            WHERE parent_task_id=? AND evidence_layer<>'' AND status IN ('completed','review','deduplicated')
            """,
            (task_id,),
        )
        completed.update(row["evidence_layer"] for row in rows)
    return completed


def advance_analysis_task(task_id: str) -> dict:
    with db() as conn:
        task_row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task_row:
        raise HTTPException(404, "任务不存在")
    task = dict(task_row)
    state = json.loads(task.get("agent_state") or "{}")
    state.setdefault("completed_layers", [])
    state.setdefault("steps", 0)
    for _ in range(8):
        completed = completed_evidence_layers(task_id, state)
        state["completed_layers"] = [layer for layer in EVIDENCE_LAYERS if layer in completed]
        pending = [layer for layer in EVIDENCE_LAYERS[1:] if layer not in completed]
        next_layer = pending[0] if pending else "final_report"
        anchor, window_start, evidence = task_snapshot_context(task["lookback_days"])
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
        decision = parse_agent_decision(call_provider(prompt))
        record_agent_event(task_id, "model_decision", {"next_allowed_layer": next_layer, "decision": decision})
        if decision["decision"] == "final":
            if decision.get("used_model_knowledge") and "model_knowledge" not in completed:
                raise HTTPException(409, "模型试图提前使用自身知识库，已被红线阻止")
            report = require_html_report(decision["report"])
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET status='review',report=?,report_anchor=?,agent_state=?,agent_error='' WHERE id=?",
                    (report, anchor, json.dumps(state, ensure_ascii=False), task_id),
                )
            record_agent_event(task_id, "report_ready", {"anchor": anchor})
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
            conn.execute(f"DELETE FROM source_collection_jobs WHERE id IN ({child_marks})", child_job_ids)
        conn.execute("DELETE FROM agent_events WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return {"status": "deleted", "cancelled_jobs": 0, "deleted_source_jobs": len(child_job_ids)}


collection_worker.on_evidence_ready = advance_analysis_task
