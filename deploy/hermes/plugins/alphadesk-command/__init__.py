from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


TRIGGER = "采集近30天数据并生成报告"
COMMAND = "alphadesk-report"
WERSS_COMMAND = "alphadesk-werss"
SUPPORTED_PLATFORMS = {"weixin", "lightclawbot"}
REPORT_SCRIPT = Path.home() / ".hermes" / "skills" / "alphadesk-cloud-report" / "scripts" / "collect_report.py"
RENDER_SCRIPT = Path.home() / ".hermes" / "skills" / "alphadesk-cloud-report" / "scripts" / "render_report_pdf.py"
SOURCE_AUTH_SCRIPT = Path.home() / ".hermes" / "skills" / "alphadesk-cloud-report" / "scripts" / "source_auth.py"
REPORT_OUTPUT_DIR = Path.home() / ".hermes" / "alphadesk-reports"
HERMES_AGENT_ROOT = Path.home() / ".hermes" / "hermes-agent"
HERMES_CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"
REPORT_REQUEST_RE = re.compile(r"采集近?(?P<days>\d{1,2})天(?:的)?数据.*生成(?:分析)?报告")
REPORT_FALLBACK_RE = re.compile(r"(生成|输出|做|整理).{0,8}(信源|三信源|聚合).{0,8}(报告|PDF)", re.I)
ANALYSIS_PREFIX_RE = re.compile(r"(?:帮我|请)?(?:分析一下|分析下|分析|研究一下|研究下|看看|看一下)(?P<query>[\w\u4e00-\u9fff·（）()\- ]{2,60})")
ANALYSIS_SUFFIX_RE = re.compile(r"(?P<query>[\w\u4e00-\u9fff·（）()\- ]{2,60})(?:分析|怎么样|怎么看|如何|值得关注吗|能不能看)")
MARKET_HINT_RE = re.compile(
    r"(A股|股票|个股|公司|行业|产业|板块|赛道|财报|公告|估值|订单|产能|半导体|芯片|光模块|算力|新能源|机器人|医药|消费|证券|银行|保险|[036]\d{5})",
    re.I,
)
META_OR_RUNTIME_QUESTION_RE = re.compile(
    r"("
    r"(hermes|agent|alphadesk|你|你们|这里|这次|当前|后台).{0,30}"
    r"(配置|供应商|模型|provider|config\.ya?ml|deepseek|jojo|skill|插件|工具|mcp|日志|调用|用了|使用|走的)"
    r"|"
    r"(调用|用了|使用|走了).{0,16}(哪些|什么|哪个|哪几个).{0,16}(skill|插件|工具|mcp|供应商|模型)"
    r"|"
    r"(skill|插件|工具|mcp|配置|供应商|模型|provider|config\.ya?ml|deepseek|jojo|api\s*key).{0,30}"
    r"(吗|么|什么|哪些|哪个|为什么|怎么|如何|是否|是不是)"
    r")",
    re.I,
)
WERSS_MANAGEMENT_RE = re.compile(
    r"^(?:请|帮我|麻烦)?\s*"
    r"(?P<action>公众号订阅状态|查看现有订阅公众号|查看订阅公众号|查看公众号订阅|现有订阅公众号|已订阅公众号|搜索公众号订阅|新增公众号订阅|添加公众号订阅|加入公众号订阅|移除公众号订阅|删除公众号订阅|补采公众号订阅|补采公众号|公众号授权|微信公众号授权|重新授权公众号|登录公众号)"
    r"\s*(?P<target>.*)$",
    re.I,
)
DEFAULT_AUDIT_PATH = Path.home() / ".hermes" / "alphadesk-command.audit.jsonl"
DEFAULT_STATE_PATH = Path.home() / ".hermes" / "alphadesk-command.state.json"
PDF_MEDIA_RE = re.compile(r"MEDIA:/\S+?\.pdf\b", re.I)
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
    if META_OR_RUNTIME_QUESTION_RE.search(text):
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
    if META_OR_RUNTIME_QUESTION_RE.search(str(value or "")):
        return None
    days = _match_report_request(value)
    if days is not None:
        return {"intent": "report", "days": days, "query": ""}
    query = _match_analysis_request(value)
    if query:
        return {"intent": "analysis", "days": 30, "query": query}
    return None


def _clean_werss_target(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n，。！？；;:：,.!?")


def _classify_werss_management_request(value: str) -> dict | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    match = WERSS_MANAGEMENT_RE.match(text)
    if not match:
        return None
    action_text = match.group("action")
    target = _clean_werss_target(match.group("target"))
    if action_text in {"公众号订阅状态", "查看现有订阅公众号", "查看订阅公众号", "查看公众号订阅", "现有订阅公众号", "已订阅公众号"}:
        return {"action": "status", "target": ""}
    if action_text == "搜索公众号订阅":
        return {"action": "search", "target": target}
    if action_text in {"新增公众号订阅", "添加公众号订阅", "加入公众号订阅"}:
        return {"action": "add", "target": target}
    if action_text in {"移除公众号订阅", "删除公众号订阅"}:
        return {"action": "remove", "target": target}
    if action_text in {"补采公众号", "补采公众号订阅"}:
        return {"action": "backfill", "target": target or "全部"}
    if action_text in {"公众号授权", "微信公众号授权", "重新授权公众号", "登录公众号"}:
        return {"action": "login", "target": ""}
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


def _quote_command_arg(value: str) -> str:
    return shlex.quote(str(value or ""))


def _command_text_for_classification(classification: dict) -> str:
    parts = [f"/{COMMAND}", "--days", str(int(classification.get("days") or 30))]
    query = str(classification.get("query") or "").strip()
    if query:
        parts.extend(["--query", _quote_command_arg(query)])
    return " ".join(parts)


def _command_text_for_werss_management(classification: dict) -> str:
    action = str(classification.get("action") or "status")
    parts = [f"/{WERSS_COMMAND}", action]
    target = str(classification.get("target") or "").strip()
    if target:
        parts.append(_quote_command_arg(target))
    return " ".join(parts)


def _audit_path() -> Path:
    configured = os.environ.get("ALPHADESK_COMMAND_AUDIT_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_AUDIT_PATH


def _state_path() -> Path:
    configured = os.environ.get("ALPHADESK_COMMAND_STATE_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_STATE_PATH


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


def _load_state() -> dict:
    path = _state_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("alphadesk state read failed", exc_info=True)
    return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        logger.debug("alphadesk state write failed", exc_info=True)


def _remember_session_intent(session_id: str, classification: dict, user_message: str, platform: str) -> None:
    if not session_id:
        return
    now = time.time()
    state = _load_state()
    sessions = state.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        state["sessions"] = sessions
    for key, item in list(sessions.items()):
        if not isinstance(item, dict) or now - float(item.get("timestamp") or 0) > 6 * 3600:
            sessions.pop(key, None)
    sessions[session_id] = {
        "timestamp": now,
        "intent": classification.get("intent") or "analysis",
        "days": classification.get("days") or 30,
        "query": classification.get("query") or "",
        "platform": platform,
        "content_preview": _preview_text(user_message),
        "require_pdf": True,
    }
    _save_state(state)


def _consume_session_intent(session_id: str) -> dict | None:
    if not session_id:
        return None
    state = _load_state()
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return None
    item = sessions.pop(session_id, None)
    _save_state(state)
    if not isinstance(item, dict):
        return None
    if time.time() - float(item.get("timestamp") or 0) > 6 * 3600:
        return None
    return item if item.get("require_pdf") else None


def _alphadesk_pre_llm_context(classification: dict) -> str:
    query = str(classification.get("query") or "").strip()
    days = int(classification.get("days") or 30)
    query_arg = f' --query "{query}"' if query else ""
    return (
        "AlphaDesk mandatory routing context:\n"
        "- This user request is an AlphaDesk stock/industry/source-report intent.\n"
        "- You must act as the AlphaDesk industry analyst, not as a generic chat assistant.\n"
        "- Use AlphaDesk evidence collection first when possible:\n"
        f"  python3 ~/.hermes/skills/alphadesk-cloud-report/scripts/collect_report.py --days {days}{query_arg}\n"
        "- The final user-visible answer must be a PDF file, not a long text report.\n"
        "- If you produce analysis text, the AlphaDesk plugin will package it into PDF and replace the chat reply with MEDIA:/...pdf."
    )


def _plain_text_to_structured_html(response_text: str, title: str) -> str:
    escaped = html.escape(str(response_text or "").strip()).replace("\n", "<br/>")
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body>
<div class="container">
  <div class="header">
    <h1>{html.escape(title)}</h1>
    <div class="meta">
      <span>生成方：Hermes / AlphaDesk</span>
      <span>交付形式：PDF</span>
      <span>说明：原始文字分析已转为文件版，便于阅读和保存</span>
    </div>
  </div>
  <h2>一、Hermes 分析正文</h2>
  <div class="card">
    <p><span class="source-tag source-high">AlphaDesk</span><span class="source-tag">Hermes 分析</span></p>
    <ul>
      <li><span class="infer">分析</span> {escaped}</li>
    </ul>
  </div>
  <h2>二、待核验与风险提示</h2>
  <div class="card">
    <p><span class="source-tag">AlphaDesk 风控</span></p>
    <ul>
      <li><span class="unverified">待核验</span> 若本报告未展示具体信源标签，说明本轮 Hermes 未按 AlphaDesk 证据模板输出，需复核证据采集日志。</li>
    </ul>
  </div>
</div>
</body>
</html>
"""


def _render_response_pdf(response_text: str, state: dict) -> str | None:
    if not response_text.strip() or PDF_MEDIA_RE.search(response_text):
        return None
    if not RENDER_SCRIPT.exists():
        logger.warning("alphadesk render script missing: %s", RENDER_SCRIPT)
        return None
    query = str(state.get("query") or "").strip()
    title = f"AlphaDesk-{query or '三信源'}分析报告"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    input_path = REPORT_OUTPUT_DIR / f"{title}-{stamp}.html"
    output_path = REPORT_OUTPUT_DIR / f"{title}-{stamp}.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(_plain_text_to_structured_html(response_text, title), encoding="utf-8")
    python_bin = "/opt/alphadesk/.venv/bin/python"
    if not Path(python_bin).exists():
        python_bin = "python3"
    try:
        proc = subprocess.run(
            [
                python_bin,
                str(RENDER_SCRIPT),
                "--input",
                str(input_path),
                "--format",
                "html",
                "--title",
                title,
                "--output",
                str(output_path),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )
    except Exception:
        logger.warning("alphadesk pdf render failed", exc_info=True)
        return None
    if proc.returncode != 0 or not output_path.exists():
        logger.warning("alphadesk pdf render returned %s: %s", proc.returncode, proc.stdout[-500:])
        return None
    return f"已生成 PDF 版报告，便于阅读和保存。\nMEDIA:{output_path}"


def _load_hermes_provider() -> dict:
    try:
        import yaml
        config = yaml.safe_load(HERMES_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("failed to read Hermes provider config", exc_info=True)
        return {}
    providers = config.get("providers") if isinstance(config.get("providers"), dict) else {}
    for name in ("custom_jojo", "deepseek", "kimicode"):
        provider = providers.get(name)
        if isinstance(provider, dict) and provider.get("api_key") and provider.get("base_url") and provider.get("model"):
            return {
                "name": name,
                "model": str(provider["model"]),
                "api_key": str(provider["api_key"]),
                "base_url": str(provider["base_url"]),
            }
    return {}


def _extract_final_response(stdout: str) -> str:
    marker = "🎯 FINAL RESPONSE:"
    text = str(stdout or "")
    if marker in text:
        tail = text.split(marker, 1)[1]
        tail = re.sub(r"^-{3,}\s*", "", tail.strip(), flags=re.M)
        tail = tail.split("\n\n👋", 1)[0].strip()
        if tail:
            return tail
    assistant_matches = re.findall(r"🤖 Assistant:\s*(.+)", text)
    if assistant_matches:
        return assistant_matches[-1].strip()
    return text.strip()


def _strip_code_fence(value: str) -> str:
    text = str(value or "").strip()
    fence = re.search(r"```(?:html)?\s*(.*?)```", text, flags=re.I | re.S)
    if fence:
        return fence.group(1).strip()
    return text


def _valid_structured_html(value: str) -> str | None:
    html_text = _strip_code_fence(value)
    lower = html_text.lower()
    if not lower.lstrip().startswith(("<!doctype", "<html", "<body", "<div", "<section", "<article")):
        return None
    invalid_phrases = (
        "尚未完成 html",
        "尚未完成 pdf",
        "无法继续执行",
        "工具调用次数已达上限",
        "不能生成文件",
        "无法生成文件",
    )
    if any(phrase in lower for phrase in invalid_phrases):
        return None

    def has_class(name: str) -> bool:
        return bool(re.search(r"class\s*=\s*['\"][^'\"]*\b" + re.escape(name) + r"\b", lower))

    if (
        re.search(r"<div\b[^>]*class\s*=\s*['\"][^'\"]*\bcontainer\b", lower)
        and has_class("card")
        and has_class("source-tag")
        and has_class("fact")
        and has_class("infer")
        and has_class("unverified")
    ):
        return html_text
    return None


def _strip_embedded_report_requirements(evidence_text: str) -> str:
    text = str(evidence_text or "")
    marker = "\nReport requirements:"
    if marker in text:
        return text.split(marker, 1)[0].rstrip()
    return text


def _evidence_line_value(evidence_text: str, prefix: str) -> str:
    for line in str(evidence_text or "").splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def _evidence_list_after(evidence_text: str, heading: str, *, limit: int = 20) -> list[str]:
    lines = str(evidence_text or "").splitlines()
    result: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            capture = True
            continue
        if capture and stripped and not stripped.startswith("-") and stripped.endswith(":"):
            break
        if capture and stripped.startswith("-"):
            result.append(stripped[1:].strip())
            if len(result) >= limit:
                break
    return result


def _selected_evidence_items(evidence_text: str, *, limit: int = 12) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    content_lines: list[str] = []
    header_re = re.compile(r"^\[(\d+)\]\s+([^|]+)\|\s*([^|]+)\|\s*(.*)$")
    for raw_line in str(evidence_text or "").splitlines():
        line = raw_line.strip()
        match = header_re.match(line)
        if match:
            if current:
                current["content"] = " ".join(content_lines).strip()
                items.append(current)
                if len(items) >= limit:
                    return items
            current = {
                "index": match.group(1).strip(),
                "channel": match.group(2).strip(),
                "time": match.group(3).strip(),
                "title": match.group(4).strip(),
            }
            content_lines = []
            continue
        if current and line and not line.startswith("Source:"):
            content_lines.append(line)
    if current and len(items) < limit:
        current["content"] = " ".join(content_lines).strip()
        items.append(current)
    return items


def _html_list(items: list[str], label_class: str = "fact", label: str = "事实") -> str:
    if not items:
        return f"<li><span class=\"unverified\">待核验</span> 暂无可用条目。</li>"
    return "\n".join(
        f"<li><span class=\"{label_class}\">{html.escape(label)}</span> {html.escape(item)}</li>"
        for item in items
    )


def _fallback_html_report(evidence_text: str, *, days: int, query: str, invalid_output: str = "") -> str:
    title = f"AlphaDesk {query or '三信源'}近{days}天分析报告"
    job = _evidence_line_value(evidence_text, "Job")
    status = _evidence_line_value(evidence_text, "Status")
    lookback = _evidence_line_value(evidence_text, "Lookback days") or str(days)
    source_runs = _evidence_list_after(evidence_text, "Source runs:", limit=10)
    coverage = _evidence_list_after(evidence_text, "Snapshot coverage:", limit=10)
    selected = _selected_evidence_items(evidence_text, limit=12)
    direct_items = [
        item for item in selected
        if query and query.lower() in (item.get("title", "") + item.get("content", "")).lower()
    ]
    evidence_scope = (
        f"本轮采集任务状态为 {status or 'unknown'}。"
        f"围绕 {query or '三信源'} 的直接证据为 {len(direct_items)} 条，"
        f"精选证据总数为 {len(selected)} 条。"
    )
    selected_lines = []
    for item in selected[:8]:
        title_text = item.get("title") or item.get("channel") or "未命名证据"
        content = item.get("content") or "无摘要"
        selected_lines.append(f"{item.get('channel')} | {title_text}：{content[:420]}")
    invalid_note = ""
    if invalid_output:
        invalid_note = (
            "<li><span class=\"unverified\">待核验</span> Hermes 模型本轮未返回合格结构化 HTML，"
            "系统已改用证据包生成兜底报告，避免把过程说明误当成研报。</li>"
        )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{html.escape(title)}</h1>
    <div class="meta">
      <span>Job：{html.escape(job or '-')}</span>
      <span>状态：{html.escape(status or '-')}</span>
      <span>窗口：近 {html.escape(str(lookback))} 天</span>
      <span>信源：WeRSS + IMA 知识库 + 知识星球 MCP</span>
    </div>
  </div>

  <h2>一、结论先行</h2>
  <div class="card">
    <p>
      <span class="source-tag source-high">AlphaDesk 证据包</span>
      <span class="source-tag">Hermes 结构化兜底</span>
    </p>
    <ul>
      <li><span class="fact">事实</span> {html.escape(evidence_scope)}</li>
      <li><span class="infer">推断</span> 当前报告应定位为“证据覆盖与待补采报告”，不能强行给出高置信投资结论。</li>
      <li><span class="unverified">待核验</span> 若需要公司级深度结论，应补采公告、财报、股价、券商研报、公众号与 IMA 知识库中的直接材料。</li>
    </ul>
  </div>

  <h2>二、逐信源状态</h2>
  <div class="card">
    <p>
      <span class="source-tag source-high">采集状态</span>
      <span class="source-tag">水位检测</span>
    </p>
    <ul>
      {_html_list(source_runs, "fact", "事实")}
    </ul>
  </div>

  <h2>三、快照覆盖</h2>
  <div class="card">
    <p>
      <span class="source-tag">快照统计</span>
    </p>
    <ul>
      {_html_list(coverage, "fact", "事实")}
      <li><span class="infer">推断</span> 有快照的信源可作为背景证据；没有快照或失败的信源不能支持确定性结论。</li>
    </ul>
  </div>

  <h2>四、精选证据摘要</h2>
  <div class="card">
    <p>
      <span class="source-tag source-high">知识星球 / WeRSS / IMA</span>
    </p>
    <ul>
      {_html_list(selected_lines, "fact", "事实")}
    </ul>
  </div>

  <h2>五、待核验与风险提示</h2>
  <div class="card">
    <p>
      <span class="source-tag">AlphaDesk 风控</span>
    </p>
    <ul>
      {invalid_note}
      <li><span class="unverified">待核验</span> 本报告不得替代投资建议；证据不足处必须回到原始信源复核。</li>
      <li><span class="unverified">待核验</span> IMA 超量、WeRSS 无快照或知识星球证据偏背景化时，结论置信度应下调。</li>
      <li><span class="infer">推断</span> 下一步应按“公司公告/财报/产业链订单/客户与竞品/股价与估值”补齐直接证据。</li>
    </ul>
  </div>
</div>
</body>
</html>"""


async def _run_subprocess(command: list[str], *, cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        stdout, _ = await proc.communicate()
        return 124, stdout.decode("utf-8", errors="replace")
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


def _extract_werss_command_options(raw_args: str) -> tuple[str, str]:
    tokens = shlex.split(raw_args or "")
    if not tokens:
        return "status", ""
    action = tokens[0].strip().lower()
    target = " ".join(tokens[1:]).strip()
    aliases = {
        "状态": "status",
        "status": "status",
        "search": "search",
        "搜索": "search",
        "add": "add",
        "新增": "add",
        "添加": "add",
        "加入": "add",
        "remove": "remove",
        "delete": "remove",
        "删除": "remove",
        "移除": "remove",
        "backfill": "backfill",
        "补采": "backfill",
        "login": "login",
        "auth": "login",
        "授权": "login",
    }
    return aliases.get(action, action), target


async def _run_werss(raw_args: str) -> str:
    if not SOURCE_AUTH_SCRIPT.exists():
        return f"AlphaDesk source auth script is missing: {SOURCE_AUTH_SCRIPT}"
    action, target = _extract_werss_command_options(raw_args)
    action_map = {
        "status": "werss-status",
        "search": "werss-search",
        "add": "werss-add",
        "remove": "werss-remove",
        "backfill": "werss-backfill",
        "login": "werss-login",
    }
    script_action = action_map.get(action)
    if not script_action:
        return "不支持的 WeRSS 管理动作。可用：status/search/add/remove/backfill/login。"
    if action in {"search", "add", "remove"} and not target:
        return f"请提供公众号名称、关键词、编号或 id。示例：/{WERSS_COMMAND} {action} 半导体行业观察"
    if action == "backfill" and not target:
        target = "全部"
    command = ["python3", str(SOURCE_AUTH_SCRIPT), script_action]
    if target:
        command.extend(["--query", target])
    code, stdout = await _run_subprocess(command, timeout=240)
    text = stdout.strip() or f"WeRSS command exited with code {code}."
    if code != 0 and "MEDIA:" not in text:
        return f"WeRSS 管理命令失败：exit={code}\n{text}"
    return text


def _analysis_prompt(evidence_text: str, *, days: int, query: str) -> str:
    title = f"AlphaDesk {query or '三信源'}近{days}天分析报告"
    evidence_body = _strip_embedded_report_requirements(evidence_text)
    return f"""
你是 AlphaDesk 行业分析师。请基于下面的 AlphaDesk 三信源证据包生成完整中文 HTML 报告。

硬性要求：
1. 只输出 HTML，不要输出 Markdown，不要输出解释文字。
2. 顶层必须包含 <div class="container">。
3. 每个主题必须使用 <div class="card">。
4. 每个 card 开头必须有 <span class="source-tag">，主证据用 <span class="source-tag source-high">。
5. 每条要点必须使用 <span class="fact">事实</span>、<span class="infer">推断</span> 或 <span class="unverified">待核验</span>。
6. 如果证据不足，仍然输出 PDF 友好的“证据不足/待补采”报告，不要编造事实。
7. 不要输出“尚未完成 HTML 文件保存”“尚未完成 PDF 渲染”“我无法生成文件”等过程说明；你只负责返回可渲染 HTML。
8. 报告标题：{title}

证据包：
{evidence_body}
""".strip()


async def _collect_evidence(days: int, query: str) -> tuple[int, str]:
    if not REPORT_SCRIPT.exists():
        return 2, f"AlphaDesk report script is missing: {REPORT_SCRIPT}"
    command = ["python3", str(REPORT_SCRIPT), "--days", str(days)]
    if query:
        command.extend(["--query", query])
    command.extend(["--max-evidence-chars", "24000", "--limit-per-channel", "12", "--preview-chars", "1200"])
    return await _run_subprocess(command, timeout=1900)


async def _generate_html_with_hermes(evidence_text: str, *, days: int, query: str) -> tuple[int, str]:
    provider = _load_hermes_provider()
    if not provider:
        return 2, "Hermes provider config is missing"
    run_agent = HERMES_AGENT_ROOT / "run_agent.py"
    python_bin = HERMES_AGENT_ROOT / "venv" / "bin" / "python"
    if not run_agent.exists():
        return 2, f"Hermes run_agent.py is missing: {run_agent}"
    command = [
        str(python_bin if python_bin.exists() else "python3"),
        str(run_agent),
        "--query",
        _analysis_prompt(evidence_text, days=days, query=query),
        "--max_turns",
        "1",
        "--model",
        provider["model"],
        "--api_key",
        provider["api_key"],
        "--base_url",
        provider["base_url"],
    ]
    code, stdout = await _run_subprocess(command, cwd=HERMES_AGENT_ROOT, timeout=240)
    return code, _extract_final_response(stdout)


async def _render_text_to_pdf(text: str, *, days: int, query: str, is_html: bool) -> str:
    if not RENDER_SCRIPT.exists():
        return f"AlphaDesk PDF render script is missing: {RENDER_SCRIPT}"
    title = f"AlphaDesk-{query or '三信源'}近{days}天分析报告"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = "html" if is_html else "md"
    input_path = REPORT_OUTPUT_DIR / f"{title}-{stamp}.{suffix}"
    output_path = REPORT_OUTPUT_DIR / f"{title}-{stamp}.pdf"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(text, encoding="utf-8")
    python_bin = "/opt/alphadesk/.venv/bin/python"
    if not Path(python_bin).exists():
        python_bin = "python3"
    code, stdout = await _run_subprocess(
        [
            python_bin,
            str(RENDER_SCRIPT),
            "--input",
            str(input_path),
            "--format",
            "html" if is_html else "markdown",
            "--title",
            title,
            "--output",
            str(output_path),
        ],
        timeout=180,
    )
    if code != 0 or not output_path.exists():
        return f"AlphaDesk PDF render failed: {stdout[-500:]}"
    return f"已生成 PDF 版报告，便于阅读和保存。\nMEDIA:{output_path}"


def _pre_llm_call(**kwargs):
    platform = str(kwargs.get("platform") or "").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None
    user_message = str(kwargs.get("user_message") or "")
    classification = _classify_alphadesk_request(user_message)
    if classification is None:
        return None
    session_id = str(kwargs.get("session_id") or "")
    _remember_session_intent(session_id, classification, user_message, platform)
    return {"context": _alphadesk_pre_llm_context(classification)}


def _transform_llm_output(**kwargs):
    platform = str(kwargs.get("platform") or "").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None
    session_id = str(kwargs.get("session_id") or "")
    state = _consume_session_intent(session_id)
    if not state:
        return None
    return _render_response_pdf(str(kwargs.get("response_text") or ""), state)


async def _run_report(raw_args: str) -> str:
    days, query = _extract_command_options(raw_args)
    collect_code, evidence = await _collect_evidence(days, query)
    if collect_code != 0 and not evidence.strip():
        return f"AlphaDesk 采集失败，未生成 PDF：exit={collect_code}"
    html_code, html_report = await _generate_html_with_hermes(evidence, days=days, query=query)
    structured_html = _valid_structured_html(html_report) if html_code == 0 else None
    if structured_html:
        rendered = await _render_text_to_pdf(structured_html, days=days, query=query, is_html=True)
        if "MEDIA:" in rendered:
            return rendered
        logger.warning("alphadesk analysis pdf render failed, falling back to evidence pdf: %s", rendered)
    fallback_html = _fallback_html_report(
        evidence,
        days=days,
        query=query,
        invalid_output=html_report if html_code == 0 else f"html_generation_exit={html_code}",
    )
    return await _render_text_to_pdf(fallback_html, days=days, query=query, is_html=True)


def _pre_gateway_dispatch(event, **_kwargs):
    source = getattr(event, "source", None)
    platform = str(getattr(getattr(source, "platform", None), "value", "") or "").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None

    text = getattr(event, "text", "")
    werss_classification = _classify_werss_management_request(text)
    if werss_classification is not None:
        return {"action": "rewrite", "text": _command_text_for_werss_management(werss_classification)}

    classification = _classify_alphadesk_request(text)
    if classification is None:
        return None

    _record_alphadesk_request(
        event,
        intent=classification["intent"],
        days=classification["days"],
        query=classification["query"],
    )
    return {"action": "rewrite", "text": _command_text_for_classification(classification)}


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_command(
        COMMAND,
        _run_report,
        description="Generate an AlphaDesk three-source PDF report or scoped analysis evidence pack.",
        args_hint="[--days 1-30] [--query 股票/公司/行业]",
    )
    ctx.register_command(
        WERSS_COMMAND,
        _run_werss,
        description="Manage AlphaDesk WeRSS official-account subscriptions and QR authorization.",
        args_hint="status | search 关键词 | add 名称/编号/id | remove 名称/id | backfill 名称/全部 | login",
    )
