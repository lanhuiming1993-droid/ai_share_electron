from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

from backend.http_policy import browser_http_session

DEFAULT_WERSS_CONFIG = {
    "adapter": "werss_external_rss",
    "base_url": "http://127.0.0.1:8001",
    "feed_ids": ["all"],
    "access_key": "",
    "secret_key": "",
    "timeout_seconds": 20,
    "max_items_per_feed": 100,
}
MASKED_SECRET = "****************"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
MAX_FEED_RESPONSE_BYTES = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
WERSS_INTEGRATION_DIR = ROOT / "integrations" / "werss"
WERSS_COMPOSE_PATH = WERSS_INTEGRATION_DIR / "compose.yaml"


def normalize_werss_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = {**DEFAULT_WERSS_CONFIG, **(config or {}), "adapter": "werss_external_rss"}
    base_url = str(merged.get("base_url") or "").strip().rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WeRSS 服务地址必须是有效的 http 或 https URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WeRSS 服务地址不能包含账号、密码、查询参数或片段")
    feed_ids: list[str] = []
    for value in merged.get("feed_ids") or ["all"]:
        feed_id = str(value or "").strip()
        if feed_id and feed_id not in feed_ids:
            feed_ids.append(feed_id)
    if not feed_ids:
        feed_ids = ["all"]
    if len(feed_ids) > 50:
        raise ValueError("WeRSS Feed ID 最多配置 50 个")
    access_key = str(merged.get("access_key") or "").strip()
    secret_key = str(merged.get("secret_key") or "").strip()
    if bool(access_key) != bool(secret_key):
        raise ValueError("WeRSS AK 和 SK 必须同时填写或同时留空")
    return {
        "adapter": "werss_external_rss",
        "base_url": base_url,
        "feed_ids": feed_ids,
        "access_key": access_key,
        "secret_key": secret_key,
        "timeout_seconds": min(max(int(merged.get("timeout_seconds") or 20), 3), 120),
        "max_items_per_feed": min(max(int(merged.get("max_items_per_feed") or 100), 1), 500),
    }


def public_werss_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    configured = bool(normalized["access_key"] and normalized["secret_key"])
    return {
        **normalized,
        "management_url": f"{normalized['base_url']}/",
        "credentials_configured": configured,
        "access_key": MASKED_SECRET if configured else "",
        "secret_key": MASKED_SECRET if configured else "",
    }


def werss_headers(config: dict[str, Any]) -> dict[str, str]:
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1"}
    if config["access_key"] and config["secret_key"]:
        headers["Authorization"] = f"AK-SK {config['access_key']}:{config['secret_key']}"
    return headers


def feed_url(config: dict[str, Any], feed_id: str, query: str = "") -> str:
    encoded_feed_id = quote(feed_id, safe="")
    if query.strip():
        return f"{config['base_url']}/feed/search/{quote(query.strip(), safe='')}/{encoded_feed_id}.rss"
    return f"{config['base_url']}/feed/{encoded_feed_id}.rss"


def parsed_timestamp(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        result = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.astimezone()
    return result.astimezone()


def child_text(element: ElementTree.Element, *names: str) -> str:
    for name in names:
        child = element.find(name)
        if child is not None:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def parse_werss_feed(xml_text: str) -> list[dict[str, str]]:
    root = ElementTree.fromstring(xml_text)
    items: list[dict[str, str]] = []
    if root.tag.rsplit("}", 1)[-1].lower() == "rss":
        for entry in root.findall("./channel/item"):
            items.append(
                {
                    "id": child_text(entry, "guid"),
                    "title": child_text(entry, "title"),
                    "author": child_text(entry, "author", "{http://purl.org/dc/elements/1.1/}creator"),
                    "published_at": child_text(entry, "pubDate", "{http://purl.org/dc/elements/1.1/}date"),
                    "link": child_text(entry, "link"),
                    "description": child_text(entry, "description"),
                    "content": child_text(entry, f"{{{CONTENT_NAMESPACE}}}encoded", "description"),
                }
            )
        return items
    if root.tag == f"{{{ATOM_NAMESPACE}}}feed":
        for entry in root.findall(f"{{{ATOM_NAMESPACE}}}entry"):
            link = ""
            for link_element in entry.findall(f"{{{ATOM_NAMESPACE}}}link"):
                if link_element.attrib.get("rel", "alternate") == "alternate" and link_element.attrib.get("href"):
                    link = str(link_element.attrib["href"]).strip()
                    break
            items.append(
                {
                    "id": child_text(entry, f"{{{ATOM_NAMESPACE}}}id"),
                    "title": child_text(entry, f"{{{ATOM_NAMESPACE}}}title"),
                    "author": child_text(entry, f"{{{ATOM_NAMESPACE}}}author/{{{ATOM_NAMESPACE}}}name"),
                    "published_at": child_text(entry, f"{{{ATOM_NAMESPACE}}}published", f"{{{ATOM_NAMESPACE}}}updated"),
                    "link": link,
                    "description": child_text(entry, f"{{{ATOM_NAMESPACE}}}summary"),
                    "content": child_text(entry, f"{{{ATOM_NAMESPACE}}}content", f"{{{ATOM_NAMESPACE}}}summary"),
                }
            )
        return items
    raise ValueError("WeRSS 返回的内容不是受支持的 RSS 或 Atom XML")


def fetch_werss_feed(config: dict[str, Any], feed_id: str, query: str = "", limit: int | None = None, session=None) -> tuple[str, list[dict[str, str]]]:
    url = feed_url(config, feed_id, query)
    session = session or browser_http_session()
    response = session.get(
        url,
        params={"limit": limit or config["max_items_per_feed"], "offset": 0},
        headers=werss_headers(config),
        timeout=config["timeout_seconds"],
    )
    response.raise_for_status()
    if len(response.text.encode("utf-8")) > MAX_FEED_RESPONSE_BYTES:
        raise ValueError("WeRSS RSS 响应超过 8 MB 限制")
    return response.url, parse_werss_feed(response.text)


def collect_werss(channel: dict[str, Any], window: dict[str, str], query: str = "") -> list[dict[str, str]]:
    config = normalize_werss_config(channel.get("request_config"))
    window_start = datetime.fromisoformat(window["window_start"])
    window_end = datetime.fromisoformat(window["window_end"])
    seen: set[tuple[str, str, str]] = set()
    snapshots: list[dict[str, str]] = []
    session = browser_http_session()
    for feed_id in config["feed_ids"]:
        request_url, articles = fetch_werss_feed(config, feed_id, query, session=session)
        for article in articles:
            occurred_at = parsed_timestamp(article["published_at"])
            if not occurred_at or occurred_at < window_start or occurred_at > window_end:
                continue
            source_url = article["link"] or f"{request_url}#{quote(article['id'] or article['title'], safe='')}"
            stable_key = (feed_id, article["id"] or source_url, occurred_at.isoformat(timespec="seconds"))
            if stable_key in seen:
                continue
            seen.add(stable_key)
            payload = {
                "platform": "werss_external_rss",
                "adapter": "external_sidecar",
                "feed_id": feed_id,
                "query": query,
                "collection_window": window,
                "article": article,
            }
            snapshots.append(
                {
                    "channel_id": channel["id"],
                    "occurred_at": occurred_at.isoformat(timespec="seconds"),
                    "source_url": source_url,
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            )
    return snapshots


def check_werss(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        feed_id = normalized["feed_ids"][0]
        _, items = fetch_werss_feed(normalized, feed_id, limit=1, session=browser_http_session())
    except Exception as exc:
        return {
            "status": "offline",
            "message": f"WeRSS RSS 服务不可用：{type(exc).__name__}",
            "checked_at": checked_at,
        }
    return {
        "status": "online",
        "message": f"WeRSS RSS 服务可用；抽样读取 {len(items)} 条文章",
        "checked_at": checked_at,
    }


def managed_werss_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    session = browser_http_session()
    service_online = False
    try:
        response = session.get(f"{normalized['base_url']}/", timeout=min(normalized["timeout_seconds"], 5))
        service_online = response.status_code < 500
    except Exception:
        pass
    rss_status = check_werss(normalized)
    docker_available = shutil.which("docker") is not None
    return {
        "status": rss_status["status"],
        "message": rss_status["message"],
        "checked_at": rss_status["checked_at"],
        "service_online": service_online,
        "rss_online": rss_status["status"] == "online",
        "docker_available": docker_available,
        "docker_engine_available": docker_engine_available() if docker_available else False,
        "managed_setup_available": WERSS_COMPOSE_PATH.exists(),
        "management_url": f"{normalized['base_url']}/",
        "wechat_status_url": f"{normalized['base_url']}/wechat-status",
        "subscription_url": f"{normalized['base_url']}/wechat/mp",
        "add_subscription_url": f"{normalized['base_url']}/add-subscription",
        "onboarding_steps": [
            "启动本地 WeRSS 组件，或填写已有 WeRSS 服务地址",
            "打开 WeRSS 管理台并登录管理账号",
            "进入公众号状态页面，使用微信扫码授权",
            "进入添加订阅页面，搜索并选择需要采集的公众号",
            "回到 AlphaDesk 检查状态，再发起信源采集任务",
        ],
    }


def docker_engine_available() -> bool:
    if shutil.which("docker") is None:
        return False
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def start_managed_werss() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("未检测到 Docker。请先安装并启动 Docker Desktop，再启动本地 WeRSS 组件。")
    if not docker_engine_available():
        raise RuntimeError("已检测到 Docker 命令，但 Docker Desktop 引擎未运行。请启动 Docker Desktop 后重试。")
    if not WERSS_COMPOSE_PATH.exists():
        raise RuntimeError("缺少 WeRSS Compose 配置，无法启动本地组件。")
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    completed = subprocess.run(
        ["docker", "compose", "-f", str(WERSS_COMPOSE_PATH), "up", "-d"],
        cwd=WERSS_INTEGRATION_DIR,
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=creationflags,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "docker compose failed").strip()
        raise RuntimeError(f"WeRSS 启动失败：{detail[-1200:]}")
