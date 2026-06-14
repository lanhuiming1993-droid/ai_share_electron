from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path


TRIGGER = "采集近30天数据并生成报告"
COMMAND = "alphadesk-report"
SUPPORTED_PLATFORMS = {"weixin", "lightclawbot"}
REPORT_SCRIPT = Path.home() / ".hermes" / "skills" / "alphadesk-cloud-report" / "scripts" / "collect_report.py"
REPORT_REQUEST_RE = re.compile(r"采集近?(?P<days>\d{1,2})天(?:的)?数据.*生成(?:分析)?报告")
REPORT_FALLBACK_RE = re.compile(r"(生成|输出|做|整理).{0,8}(信源|三信源|聚合).{0,8}(报告|PDF)", re.I)
ANALYSIS_PREFIX_RE = re.compile(r"(?:帮我|请)?(?:分析一下|分析下|分析|研究一下|研究下|看看|看一下)(?P<query>[\w\u4e00-\u9fff·（）()\- ]{2,60})")
ANALYSIS_SUFFIX_RE = re.compile(r"(?P<query>[\w\u4e00-\u9fff·（）()\- ]{2,60})(?:分析|怎么样|怎么看|如何|值得关注吗|能不能看)")
MARKET_HINT_RE = re.compile(
    r"(A股|股票|个股|公司|行业|产业|板块|赛道|财报|公告|估值|订单|产能|半导体|芯片|光模块|算力|新能源|机器人|医药|消费|证券|银行|保险|[036]\d{5})",
    re.I,
)
DEFAULT_AUDIT_PATH = Path.home() / ".hermes" / "alphadesk-command.audit.jsonl"
logger = logging.getLogger(__name__)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _match_report_request(value: str) -> int | None:
    compact = _compact_text(value)
    match = REPORT_REQUEST_RE.search(compact)
    if not match and not REPORT_FALLBACK_RE.search(compact):
        return None
    if not match:
        return 30
    return max(1, min(30, int(match.group("days"))))


def _clean_query(value: str) -> str:
    query = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n，。！？；：,.!?;:、")
    query = re.sub(r"^(一下|下|关于|对|把|给我|帮我)", "", query).strip(" \t\r\n，。！？；：,.!?;:、")
    query = re.sub(r"(的)?(情况|资料|数据|报告|PDF|pdf)$", "", query).strip(" \t\r\n，。！？；：,.!?;:、")
    return query[:60]


def _match_analysis_request(value: str) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    for pattern in (ANALYSIS_PREFIX_RE, ANALYSIS_SUFFIX_RE):
        match = pattern.search(text)
        if not match:
            continue
        query = _clean_query(match.group("query"))
        if len(query) < 2:
            continue
        if MARKET_HINT_RE.search(text) or len(query) <= 16:
            return query
    return None


def _classify_alphadesk_request(value: str) -> dict | None:
    days = _match_report_request(value)
    if days is not None:
        return {"intent": "report", "days": days, "query": ""}
    query = _match_analysis_request(value)
    if query:
        return {"intent": "analysis", "days": 30, "query": query}
    return None


def _extract_command_options(raw_args: str) -> tuple[int, str]:
    days = 30
    query = ""
    tokens = shlex.split(raw_args or "")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--days" and index + 1 < len(tokens):
            days = max(1, min(30, int(tokens[index + 1])))
            index += 2
            continue
        if token.startswith("--days="):
            days = max(1, min(30, int(token.split("=", 1)[1])))
            index += 1
            continue
        if token == "--query" and index + 1 < len(tokens):
            query = tokens[index + 1].strip()
            index += 2
            continue
        if token.startswith("--query="):
            query = token.split("=", 1)[1].strip()
            index += 1
            continue
        if token.isdigit():
            days = max(1, min(30, int(token)))
        elif not query:
            query = token.strip()
        index += 1
    return days, query


def _audit_path() -> Path:
    configured = os.environ.get("ALPHADESK_COMMAND_AUDIT_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_AUDIT_PATH


def _source_attr(source, name: str) -> str:
    return str(getattr(source, name, "") or "")


def _preview_text(value: str, limit: int = 160) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    return compact[:limit]


def _record_alphadesk_request(event, *, intent: str, days: int, query: str) -> None:
    source = getattr(event, "source", None)
    platform = str(getattr(getattr(source, "platform", None), "value", "") or "").lower()
    text_preview = _preview_text(getattr(event, "text", ""))
    payload = {
        "timestamp": time.time(),
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "command": COMMAND,
        "intent": intent,
        "days": days,
        "query": query,
        "user_id": _source_attr(source, "user_id"),
        "user_name": _source_attr(source, "user_name"),
        "chat_id": _source_attr(source, "chat_id"),
        "chat_type": _source_attr(source, "chat_type"),
        "message_id": _source_attr(event, "message_id"),
        "content_preview": text_preview,
    }
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        logger.debug("alphadesk command audit write failed", exc_info=True)
    logger.info(
        "alphadesk command matched: platform=%s user=%s chat=%s intent=%s days=%s query=%r msg=%r",
        platform,
        payload["user_name"] or payload["user_id"] or "unknown",
        payload["chat_id"] or "unknown",
        intent,
        days,
        query,
        text_preview,
    )


async def _run_report(raw_args: str) -> str:
    days, query = _extract_command_options(raw_args)
    if not REPORT_SCRIPT.exists():
        return f"AlphaDesk report script is missing: {REPORT_SCRIPT}"

    command = ["python3", str(REPORT_SCRIPT), "--days", str(days)]
    if query:
        command.extend(["--query", query])
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return output or "AlphaDesk report completed, but the script returned no output."
    return f"AlphaDesk report failed with exit code {proc.returncode}.\n\n{output}"


def _pre_gateway_dispatch(event, **_kwargs):
    source = getattr(event, "source", None)
    platform = str(getattr(getattr(source, "platform", None), "value", "") or "").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None

    classification = _classify_alphadesk_request(getattr(event, "text", ""))
    if classification is None:
        return None

    _record_alphadesk_request(
        event,
        intent=classification["intent"],
        days=classification["days"],
        query=classification["query"],
    )
    return None


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_command(
        COMMAND,
        _run_report,
        description="Generate an AlphaDesk three-source PDF report or scoped analysis evidence pack.",
        args_hint="[--days 1-30] [--query 股票/公司/行业]",
    )
