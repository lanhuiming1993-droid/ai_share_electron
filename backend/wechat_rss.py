from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit
from xml.etree import ElementTree

from backend.http_policy import browser_http_session

DEFAULT_WERSS_CONFIG = {
    "adapter": "werss_external_rss",
    "base_url": "http://127.0.0.1:8001",
    "feed_ids": ["all"],
    "access_key": "",
    "secret_key": "",
    "admin_username": "admin",
    "admin_password": "admin@123",
    "timeout_seconds": 20,
    "max_items_per_feed": 100,
}
MASKED_SECRET = "****************"
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
CONTENT_NAMESPACE = "http://purl.org/rss/1.0/modules/content/"
MAX_FEED_RESPONSE_BYTES = 8 * 1024 * 1024
WERSS_FEED_PAGE_SIZE = 10
ROOT = Path(__file__).resolve().parents[1]
WERSS_INTEGRATION_DIR = ROOT / "integrations" / "werss"
WERSS_COMPOSE_PATH = WERSS_INTEGRATION_DIR / "compose.yaml"
WERSS_API_PREFIX = "/api/v1/wx"
WERSS_TOKEN_TTL_SECONDS = 25 * 60
WERSS_WECHAT_AUTH_TRUE_TTL_SECONDS = 5 * 60
WERSS_WECHAT_AUTH_FALSE_TTL_SECONDS = 10
_WERSS_ADMIN_TOKENS: dict[str, tuple[str, float]] = {}
_WERSS_WECHAT_AUTH: dict[str, tuple[bool, float]] = {}


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
    admin_username = str(merged.get("admin_username") or "admin").strip()
    admin_password = str(merged.get("admin_password") or "admin@123").strip()
    if not admin_username or not admin_password:
        raise ValueError("WeRSS 管理账号和密码不能为空")
    return {
        "adapter": "werss_external_rss",
        "base_url": base_url,
        "feed_ids": feed_ids,
        "access_key": access_key,
        "secret_key": secret_key,
        "admin_username": admin_username,
        "admin_password": admin_password,
        "timeout_seconds": min(max(int(merged.get("timeout_seconds") or 20), 3), 120),
        "max_items_per_feed": min(max(int(merged.get("max_items_per_feed") or 100), 1), 500),
    }


def public_werss_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    configured = bool(normalized["access_key"] and normalized["secret_key"])
    admin_password_configured = bool(normalized["admin_password"])
    return {
        **normalized,
        "management_url": f"{normalized['base_url']}/",
        "credentials_configured": configured,
        "access_key": MASKED_SECRET if configured else "",
        "secret_key": MASKED_SECRET if configured else "",
        "admin_password_configured": admin_password_configured,
        "admin_password": MASKED_SECRET if admin_password_configured else "",
    }


def werss_api_url(config: dict[str, Any], path: str) -> str:
    return f"{config['base_url']}{WERSS_API_PREFIX}/{path.lstrip('/')}"


def werss_response_data(response) -> Any:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("WeRSS 返回了无法识别的响应")
    detail = payload.get("detail")
    if isinstance(detail, dict):
        nested = detail.get("detail")
        detail = nested if isinstance(nested, dict) else detail
        raise RuntimeError(str(detail.get("message") or "WeRSS 请求失败"))
    if payload.get("code", 0) != 0:
        raise RuntimeError(str(payload.get("message") or "WeRSS 请求失败"))
    return payload.get("data")


def werss_admin_token(config: dict[str, Any], session=None, force_refresh: bool = False) -> str:
    normalized = normalize_werss_config(config)
    cache_key = normalized["base_url"]
    cached = _WERSS_ADMIN_TOKENS.get(cache_key)
    if not force_refresh and cached and cached[1] > time.time():
        return cached[0]
    session = session or browser_http_session()
    response = session.post(
        werss_api_url(normalized, "auth/login"),
        data={"username": normalized["admin_username"], "password": normalized["admin_password"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=normalized["timeout_seconds"],
    )
    data = werss_response_data(response)
    token = str(data.get("access_token") if isinstance(data, dict) else "").strip()
    if not token:
        raise RuntimeError("WeRSS 管理登录未返回访问令牌")
    _WERSS_ADMIN_TOKENS[cache_key] = (token, time.time() + WERSS_TOKEN_TTL_SECONDS)
    return token


def werss_admin_get(config: dict[str, Any], path: str, *, params: dict[str, Any] | None = None, session=None) -> Any:
    normalized = normalize_werss_config(config)
    session = session or browser_http_session()
    token = werss_admin_token(normalized, session=session)
    response = session.get(
        werss_api_url(normalized, path),
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=normalized["timeout_seconds"],
    )
    if response.status_code == 401:
        token = werss_admin_token(normalized, session=session, force_refresh=True)
        response = session.get(
            werss_api_url(normalized, path),
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=normalized["timeout_seconds"],
        )
    return werss_response_data(response)


def werss_admin_post(config: dict[str, Any], path: str, *, json_body: dict[str, Any], session=None) -> Any:
    normalized = normalize_werss_config(config)
    session = session or browser_http_session()
    token = werss_admin_token(normalized, session=session)
    response = session.post(
        werss_api_url(normalized, path),
        json=json_body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=normalized["timeout_seconds"],
    )
    if response.status_code == 401:
        token = werss_admin_token(normalized, session=session, force_refresh=True)
        response = session.post(
            werss_api_url(normalized, path),
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=normalized["timeout_seconds"],
        )
    return werss_response_data(response)


def fetch_werss_subscriptions(config: dict[str, Any] | None = None, session=None) -> list[dict[str, Any]]:
    normalized = normalize_werss_config(config)
    data = werss_admin_get(normalized, "mps", params={"limit": 100, "offset": 0}, session=session)
    rows = data.get("list") if isinstance(data, dict) else []
    return [
        {
            "id": str(row.get("id") or ""),
            "name": str(row.get("mp_name") or ""),
            "avatar": str(row.get("mp_cover") or ""),
            "intro": str(row.get("mp_intro") or ""),
            "enabled": int(row.get("status") or 0) == 1,
        }
        for row in rows or []
        if isinstance(row, dict)
    ]


def search_werss_public_accounts(config: dict[str, Any] | None, keyword: str, session=None, limit: int = 10) -> list[dict[str, Any]]:
    normalized = normalize_werss_config(config)
    query = str(keyword or "").strip()
    if not query:
        raise ValueError("请输入公众号名称或关键词")
    data = werss_admin_get(
        normalized,
        f"mps/search/{quote(query, safe='')}",
        params={"limit": min(max(int(limit), 1), 20), "offset": 0},
        session=session,
    )
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise RuntimeError("微信公众号授权尚未生效，请重新扫码")
    return [
        {
            "id": str(row.get("fakeid") or ""),
            "name": str(row.get("nickname") or ""),
            "alias": str(row.get("alias") or ""),
            "avatar": str(row.get("round_head_img") or ""),
            "intro": str(row.get("signature") or ""),
            "service_type": int(row.get("service_type") or 0),
            "verify_status": int(row.get("verify_status") or 0),
        }
        for row in data["list"]
        if isinstance(row, dict) and row.get("fakeid") and row.get("nickname")
    ]


def add_werss_subscription(config: dict[str, Any] | None, account: dict[str, Any], session=None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    data = werss_admin_post(
        normalized,
        "mps",
        json_body={
            "mp_name": str(account.get("name") or "").strip(),
            "mp_cover": str(account.get("avatar") or "").strip(),
            "mp_id": str(account.get("id") or "").strip(),
            "avatar": str(account.get("avatar") or "").strip(),
            "mp_intro": str(account.get("intro") or "").strip(),
        },
        session=session,
    )
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("WeRSS 未返回新增订阅信息")
    return {
        "id": str(data.get("id") or ""),
        "name": str(data.get("mp_name") or ""),
        "avatar": str(data.get("mp_cover") or ""),
        "intro": str(data.get("mp_intro") or ""),
        "enabled": int(data.get("status") or 0) == 1,
    }


def remember_werss_wechat_authorization(config: dict[str, Any], authorized: bool) -> bool:
    normalized = normalize_werss_config(config)
    ttl = WERSS_WECHAT_AUTH_TRUE_TTL_SECONDS if authorized else WERSS_WECHAT_AUTH_FALSE_TTL_SECONDS
    _WERSS_WECHAT_AUTH[normalized["base_url"]] = (authorized, time.time() + ttl)
    return authorized


def probe_werss_wechat_authorization(config: dict[str, Any] | None = None, session=None, force_refresh: bool = False) -> bool:
    normalized = normalize_werss_config(config)
    cached = _WERSS_WECHAT_AUTH.get(normalized["base_url"])
    if not force_refresh and cached and cached[1] > time.time():
        return cached[0]
    try:
        search_werss_public_accounts(normalized, "证券", session=session, limit=1)
    except Exception:
        return remember_werss_wechat_authorization(normalized, False)
    return remember_werss_wechat_authorization(normalized, True)


def start_werss_wechat_login(config: dict[str, Any] | None = None, session=None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    if probe_werss_wechat_authorization(normalized, session=session):
        return {
            "login_state": "authorized",
            "message": "微信授权仍然有效，无需重复扫码",
            "authorized": True,
            "qr_image_url": "",
        }
    data = werss_admin_get(normalized, "auth/qr/code", session=session)
    qr_path = str(data.get("code") if isinstance(data, dict) else "").strip()
    if not qr_path:
        raise RuntimeError("WeRSS 尚未生成微信扫码二维码，请稍后重试")
    return {
        "login_state": "waiting_scan",
        "message": "请使用微信扫描二维码完成授权",
        "authorized": False,
        "qr_image_url": urljoin(f"{normalized['base_url']}/", qr_path.lstrip("/")),
    }


def werss_wechat_login_status(config: dict[str, Any] | None = None, session=None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    data = werss_admin_get(normalized, "auth/qr/status", session=session)
    login_status = bool(data.get("login_status")) if isinstance(data, dict) else False
    qr_exists = bool(data.get("qr_code")) if isinstance(data, dict) else False
    if login_status:
        remember_werss_wechat_authorization(normalized, True)
        return {"login_state": "authorized", "message": "微信扫码授权成功", "authorized": True}
    if probe_werss_wechat_authorization(normalized, session=session):
        return {"login_state": "authorized", "message": "微信授权有效", "authorized": True}
    if qr_exists:
        return {"login_state": "waiting_scan", "message": "等待微信扫码授权", "authorized": False}
    return {"login_state": "expired", "message": "二维码已失效，请重新获取", "authorized": False}


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


def fetch_werss_feed(
    config: dict[str, Any],
    feed_id: str,
    query: str = "",
    limit: int | None = None,
    offset: int = 0,
    session=None,
) -> tuple[str, list[dict[str, str]]]:
    url = feed_url(config, feed_id, query)
    session = session or browser_http_session()
    response = session.get(
        url,
        params={"limit": limit or config["max_items_per_feed"], "offset": max(int(offset), 0)},
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
        offset = 0
        remaining = config["max_items_per_feed"]
        while remaining > 0:
            page_limit = min(WERSS_FEED_PAGE_SIZE, remaining)
            while True:
                try:
                    request_url, articles = fetch_werss_feed(config, feed_id, query, limit=page_limit, offset=offset, session=session)
                    break
                except ValueError as exc:
                    if "8 MB" not in str(exc) or page_limit == 1:
                        raise
                    page_limit = max(page_limit // 2, 1)
            if not articles:
                break
            reached_window_start = False
            for article in articles:
                occurred_at = parsed_timestamp(article["published_at"])
                if not occurred_at:
                    continue
                if occurred_at < window_start:
                    reached_window_start = True
                    continue
                if occurred_at > window_end:
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
            offset += len(articles)
            remaining -= len(articles)
            if reached_window_start or len(articles) < page_limit:
                break
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
    subscriptions: list[dict[str, Any]] = []
    subscription_error = ""
    try:
        response = session.get(f"{normalized['base_url']}/", timeout=min(normalized["timeout_seconds"], 5))
        service_online = response.status_code < 500
    except Exception:
        pass
    if service_online:
        try:
            subscriptions = fetch_werss_subscriptions(normalized, session=session)
        except Exception as exc:
            subscription_error = f"{type(exc).__name__}: {exc}"
    rss_status = check_werss(normalized)
    docker_available = shutil.which("docker") is not None
    ready = service_online and rss_status["status"] == "online" and bool(subscriptions)
    return {
        "status": "online" if ready else ("pending" if service_online else "offline"),
        "message": "微信公众号信源可用" if ready else rss_status["message"],
        "checked_at": rss_status["checked_at"],
        "ready": ready,
        "service_online": service_online,
        "rss_online": rss_status["status"] == "online",
        "subscription_count": len(subscriptions),
        "subscriptions": subscriptions,
        "subscription_error": subscription_error,
        "docker_available": docker_available,
        "docker_engine_available": docker_engine_available() if docker_available else False,
        "managed_setup_available": WERSS_COMPOSE_PATH.exists(),
        "management_url": f"{normalized['base_url']}/",
        "wechat_status_url": f"{normalized['base_url']}/wechat-status",
        "subscription_url": f"{normalized['base_url']}/wechat/mp",
        "add_subscription_url": f"{normalized['base_url']}/add-subscription",
        "onboarding_steps": [
            "点击登录微信公众号",
            "使用微信扫描弹窗中的二维码",
            "AlphaDesk 自动同步 WeRSS 中已订阅公众号",
            "公众号列表同步成功后，渠道状态变为可用",
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
