from __future__ import annotations

import json
import os
import re
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

DEFAULT_WERSS_BASE_URL = os.environ.get("ALPHADESK_WERSS_BASE_URL", "http://127.0.0.1:8001").strip().rstrip("/")
DEFAULT_WERSS_PUBLIC_URL = os.environ.get("ALPHADESK_WERSS_PUBLIC_URL", "").strip().rstrip("/")
DEFAULT_WERSS_ADMIN_USERNAME = os.environ.get("ALPHADESK_WERSS_USERNAME", "admin").strip()
DEFAULT_WERSS_ADMIN_PASSWORD = os.environ.get("ALPHADESK_WERSS_PASSWORD", "admin@123").strip()
WERSS_RUNTIME_MODE = os.environ.get("ALPHADESK_WERSS_MODE", "managed").strip().casefold()
DEFAULT_WERSS_CONFIG = {
    "adapter": "werss_external_rss",
    "base_url": DEFAULT_WERSS_BASE_URL,
    "feed_ids": ["all"],
    "access_key": "",
    "secret_key": "",
    "admin_username": DEFAULT_WERSS_ADMIN_USERNAME,
    "admin_password": DEFAULT_WERSS_ADMIN_PASSWORD,
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
WERSS_QR_IMAGE_RETRY_COUNT = 15
_WERSS_ADMIN_TOKENS: dict[str, tuple[str, float]] = {}
_WERSS_WECHAT_AUTH: dict[str, tuple[bool, float]] = {}
_WERSS_QR_IMAGE_URLS: dict[str, str] = {}


def normalize_werss_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = {**DEFAULT_WERSS_CONFIG, **(config or {}), "adapter": "werss_external_rss"}
    if os.environ.get("ALPHADESK_WERSS_BASE_URL", "").strip():
        merged["base_url"] = DEFAULT_WERSS_BASE_URL
    if os.environ.get("ALPHADESK_WERSS_USERNAME", "").strip():
        merged["admin_username"] = DEFAULT_WERSS_ADMIN_USERNAME
    if os.environ.get("ALPHADESK_WERSS_PASSWORD", "").strip():
        merged["admin_password"] = DEFAULT_WERSS_ADMIN_PASSWORD
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


def public_werss_base_url(config: dict[str, Any] | None = None) -> str:
    normalized = normalize_werss_config(config)
    if DEFAULT_WERSS_PUBLIC_URL:
        return DEFAULT_WERSS_PUBLIC_URL
    hostname = str(urlsplit(normalized["base_url"]).hostname or "").casefold()
    return normalized["base_url"] if hostname in {"127.0.0.1", "localhost", "::1"} else ""


def public_werss_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    public_base_url = public_werss_base_url(normalized)
    configured = bool(normalized["access_key"] and normalized["secret_key"])
    admin_password_configured = bool(normalized["admin_password"])
    return {
        **normalized,
        "base_url": public_base_url,
        "management_url": f"{public_base_url}/" if public_base_url else "",
        "credentials_configured": configured,
        "access_key": MASKED_SECRET if configured else "",
        "secret_key": MASKED_SECRET if configured else "",
        "admin_password_configured": admin_password_configured,
        "admin_password": MASKED_SECRET if admin_password_configured else "",
    }


def werss_api_url(config: dict[str, Any], path: str, *, api_prefix: str = WERSS_API_PREFIX) -> str:
    return f"{config['base_url']}{api_prefix.rstrip('/')}/{path.lstrip('/')}"


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


def werss_admin_get(
    config: dict[str, Any],
    path: str,
    *,
    params: dict[str, Any] | None = None,
    api_prefix: str = WERSS_API_PREFIX,
    session=None,
) -> Any:
    normalized = normalize_werss_config(config)
    session = session or browser_http_session()
    token = werss_admin_token(normalized, session=session)
    response = session.get(
        werss_api_url(normalized, path, api_prefix=api_prefix),
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=normalized["timeout_seconds"],
    )
    if response.status_code == 401:
        token = werss_admin_token(normalized, session=session, force_refresh=True)
        response = session.get(
            werss_api_url(normalized, path, api_prefix=api_prefix),
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=normalized["timeout_seconds"],
        )
    return werss_response_data(response)


def werss_admin_post(
    config: dict[str, Any],
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    api_prefix: str = WERSS_API_PREFIX,
    session=None,
) -> Any:
    normalized = normalize_werss_config(config)
    session = session or browser_http_session()
    token = werss_admin_token(normalized, session=session)
    response = session.post(
        werss_api_url(normalized, path, api_prefix=api_prefix),
        params=params,
        json=json_body or {},
        headers={"Authorization": f"Bearer {token}"},
        timeout=normalized["timeout_seconds"],
    )
    if response.status_code == 401:
        token = werss_admin_token(normalized, session=session, force_refresh=True)
        response = session.post(
            werss_api_url(normalized, path, api_prefix=api_prefix),
            params=params,
            json=json_body or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=normalized["timeout_seconds"],
        )
    return werss_response_data(response)


def werss_admin_delete(config: dict[str, Any], path: str, *, session=None) -> Any:
    normalized = normalize_werss_config(config)
    session = session or browser_http_session()
    token = werss_admin_token(normalized, session=session)
    response = session.delete(
        werss_api_url(normalized, path),
        headers={"Authorization": f"Bearer {token}"},
        timeout=normalized["timeout_seconds"],
    )
    if response.status_code == 401:
        token = werss_admin_token(normalized, session=session, force_refresh=True)
        response = session.delete(
            werss_api_url(normalized, path),
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


def delete_werss_subscription(config: dict[str, Any] | None, subscription_id: str, session=None) -> dict[str, str]:
    normalized = normalize_werss_config(config)
    normalized_id = str(subscription_id or "").strip()
    if not normalized_id:
        raise ValueError("WeRSS 订阅 ID 不能为空")
    werss_admin_delete(normalized, f"mps/{quote(normalized_id, safe='')}", session=session)
    return {"id": normalized_id}


def refresh_werss_subscription_articles(
    config: dict[str, Any] | None,
    subscription_id: str,
    *,
    start_page: int = 0,
    end_page: int = 1,
    session=None,
) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    normalized_id = str(subscription_id or "").strip()
    if not normalized_id:
        raise ValueError("WeRSS subscription ID cannot be empty")
    start_page = min(max(int(start_page), 0), 100)
    end_page = min(max(int(end_page), 1), 100)
    data = werss_admin_get(
        normalized,
        f"mps/update/{quote(normalized_id, safe='')}",
        params={"start_page": start_page, "end_page": end_page},
        session=session,
    )
    result: dict[str, Any] = {
        "id": normalized_id,
        "status": "submitted",
        "message": "已提交 WeRSS 公众号补采任务",
        "start_page": start_page,
        "end_page": end_page,
    }
    if isinstance(data, dict):
        if "time_span" in data:
            result["time_span"] = data.get("time_span")
        if "total" in data:
            result["returned_count"] = data.get("total")
        elif isinstance(data.get("list"), list):
            result["returned_count"] = len(data["list"])
        nested_mps = data.get("mps")
        if isinstance(nested_mps, dict):
            result["name"] = str(nested_mps.get("mp_name") or "")
    return result


def clear_werss_task_queue(
    config: dict[str, Any] | None,
    *,
    queue_type: str = "main",
    clear_history: bool = False,
    session=None,
) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    normalized_type = str(queue_type or "main").strip().casefold()
    if normalized_type not in {"main", "content"}:
        raise ValueError("WeRSS 队列类型只能是 main 或 content")
    queue_result = werss_admin_post(
        normalized,
        "task-queue/clear",
        params={"queue_type": normalized_type},
        session=session,
    )
    history_result = None
    if clear_history:
        history_result = werss_admin_post(
            normalized,
            "task-queue/history/clear",
            params={"queue_type": normalized_type},
            session=session,
        )
    status = werss_admin_get(
        normalized,
        "task-queue/status",
        session=session,
    )
    queue_label = "文章采集队列" if normalized_type == "main" else "内容补抓队列"
    return {
        "queue_type": normalized_type,
        "queue_label": queue_label,
        "cleared": True,
        "history_cleared": bool(clear_history),
        "queue_result": queue_result,
        "history_result": history_result,
        "queue_status": status if isinstance(status, dict) else {},
        "message": f"已清空 WeRSS {queue_label}的待执行任务；正在执行的任务不会被中断",
    }


def remember_werss_wechat_authorization(config: dict[str, Any], authorized: bool) -> bool:
    normalized = normalize_werss_config(config)
    ttl = WERSS_WECHAT_AUTH_TRUE_TTL_SECONDS if authorized else WERSS_WECHAT_AUTH_FALSE_TTL_SECONDS
    _WERSS_WECHAT_AUTH[normalized["base_url"]] = (authorized, time.time() + ttl)
    return authorized


def verify_werss_wechat_authorization(config: dict[str, Any] | None = None, session=None, force_refresh: bool = False) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    cached = _WERSS_WECHAT_AUTH.get(normalized["base_url"])
    if not force_refresh and cached and cached[1] > time.time():
        authorized = cached[0]
        return {
            "authorized": authorized,
            "admin_authorized": True,
            "login_state": "authorized" if authorized else "expired",
            "message": "微信授权有效" if authorized else "微信授权已失效",
            "qr_available": False,
        }
    try:
        verify_data = werss_admin_get(normalized, "auth/verify", session=session)
        admin_authorized = bool(verify_data.get("is_valid", True)) if isinstance(verify_data, dict) else True
        if not admin_authorized:
            remember_werss_wechat_authorization(normalized, False)
            return {
                "authorized": False,
                "admin_authorized": False,
                "login_state": "invalid_admin",
                "message": "WeRSS 管理会话无效",
                "qr_available": False,
            }
        data = werss_admin_get(normalized, "auth/qr/status", session=session)
    except Exception as exc:
        remember_werss_wechat_authorization(normalized, False)
        return {
            "authorized": False,
            "admin_authorized": False,
            "login_state": "failed",
            "message": f"WeRSS 授权检查失败：{type(exc).__name__}",
            "qr_available": False,
        }
    login_status = bool(data.get("login_status")) if isinstance(data, dict) else False
    qr_exists = bool(data.get("qr_code")) if isinstance(data, dict) else False
    remember_werss_wechat_authorization(normalized, login_status)
    if login_status:
        return {
            "authorized": True,
            "admin_authorized": True,
            "login_state": "authorized",
            "message": "微信授权有效",
            "qr_available": qr_exists,
        }
    return {
        "authorized": False,
        "admin_authorized": True,
        "login_state": "waiting_scan" if qr_exists else "expired",
        "message": "等待微信扫码授权" if qr_exists else "微信授权已失效",
        "qr_available": qr_exists,
    }


def probe_werss_wechat_authorization(config: dict[str, Any] | None = None, session=None, force_refresh: bool = False) -> bool:
    return bool(verify_werss_wechat_authorization(config, session=session, force_refresh=force_refresh).get("authorized"))


def start_werss_wechat_login(config: dict[str, Any] | None = None, session=None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    authorization = verify_werss_wechat_authorization(normalized, session=session, force_refresh=True)
    if authorization["authorized"]:
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
    qr_image_url = urljoin(f"{normalized['base_url']}/", qr_path.lstrip("/"))
    _WERSS_QR_IMAGE_URLS[normalized["base_url"]] = qr_image_url
    return {
        "login_state": "waiting_scan",
        "message": "请使用微信扫描二维码完成授权",
        "authorized": False,
        "qr_image_url": qr_image_url,
    }


def fetch_werss_qr_image(config: dict[str, Any] | None = None, session=None) -> tuple[bytes, str]:
    normalized = normalize_werss_config(config)
    session = session or browser_http_session()
    qr_image_url = _WERSS_QR_IMAGE_URLS.get(normalized["base_url"]) or urljoin(
        f"{normalized['base_url']}/",
        "static/wx_qrcode.png",
    )
    for attempt in range(WERSS_QR_IMAGE_RETRY_COUNT):
        response = session.get(
            qr_image_url,
            params={"alphadesk": int(time.time())},
            timeout=normalized["timeout_seconds"],
        )
        if response.status_code != 404 or attempt == WERSS_QR_IMAGE_RETRY_COUNT - 1:
            break
        time.sleep(1)
    response.raise_for_status()
    content_type = str(response.headers.get("Content-Type") or "image/png").split(";", 1)[0].strip().casefold()
    if not content_type.startswith("image/"):
        raise RuntimeError("WeRSS 二维码响应不是图片")
    content = bytes(response.content)
    if not content:
        raise RuntimeError("WeRSS 尚未生成微信登录二维码，请稍后重试")
    return content, content_type


def werss_wechat_login_status(config: dict[str, Any] | None = None, session=None) -> dict[str, Any]:
    normalized = normalize_werss_config(config)
    authorization = verify_werss_wechat_authorization(normalized, session=session, force_refresh=True)
    if authorization["authorized"]:
        return {"login_state": "authorized", "message": "微信授权有效", "authorized": True}
    if authorization["login_state"] == "waiting_scan":
        return {"login_state": "waiting_scan", "message": "等待微信扫码授权", "authorized": False}
    if authorization["login_state"] == "failed":
        return {"login_state": "failed", "message": authorization["message"], "authorized": False}
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


def collection_feed_accounts(config: dict[str, Any], session=None) -> list[dict[str, str]]:
    subscriptions: list[dict[str, Any]] = []
    try:
        subscriptions = fetch_werss_subscriptions(config, session=session)
    except Exception:
        pass
    enabled = [subscription for subscription in subscriptions if subscription.get("enabled", True)]
    by_id = {str(subscription.get("id") or ""): subscription for subscription in enabled}
    feeds: list[dict[str, str]] = []
    seen: set[str] = set()
    for requested_feed_id in config["feed_ids"]:
        requested_feed_id = str(requested_feed_id or "").strip()
        candidates = enabled if requested_feed_id == "all" and enabled else [by_id.get(requested_feed_id, {"id": requested_feed_id})]
        for candidate in candidates:
            feed_id = str(candidate.get("id") or "").strip()
            if not feed_id or feed_id in seen:
                continue
            seen.add(feed_id)
            feeds.append({"id": feed_id, "name": str(candidate.get("name") or "").strip()})
    return feeds or [{"id": "all", "name": ""}]


def inferred_account_id(feed_id: str, article: dict[str, str]) -> str:
    if feed_id and feed_id != "all":
        return feed_id
    match = re.match(r"^(\d+)-", str(article.get("feed_item_id") or ""))
    return f"MP_WXS_{match.group(1)}" if match else ""


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
        feed_title = child_text(root, "./channel/title")
        for entry in root.findall("./channel/item"):
            items.append(
                {
                    "id": child_text(entry, "guid"),
                    "feed_item_id": child_text(entry, "id"),
                    "feed_title": feed_title,
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
        feed_title = child_text(root, f"{{{ATOM_NAMESPACE}}}title")
        for entry in root.findall(f"{{{ATOM_NAMESPACE}}}entry"):
            link = ""
            for link_element in entry.findall(f"{{{ATOM_NAMESPACE}}}link"):
                if link_element.attrib.get("rel", "alternate") == "alternate" and link_element.attrib.get("href"):
                    link = str(link_element.attrib["href"]).strip()
                    break
            items.append(
                {
                    "id": child_text(entry, f"{{{ATOM_NAMESPACE}}}id"),
                    "feed_item_id": child_text(entry, f"{{{ATOM_NAMESPACE}}}id"),
                    "feed_title": feed_title,
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
    account_feeds = collection_feed_accounts(config, session=session)
    account_names = {feed["id"]: feed["name"] for feed in account_feeds}
    for account_feed in account_feeds:
        feed_id = account_feed["id"]
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
                account_id = inferred_account_id(feed_id, article)
                account_name = account_names.get(account_id, "") or str(article.get("feed_title") or "").strip()
                if account_name.lower() == "werss":
                    account_name = ""
                source_account = {"id": account_id, "name": account_name}
                payload = {
                    "platform": "werss_external_rss",
                    "adapter": "external_sidecar",
                    "feed_id": feed_id,
                    "source_account": source_account,
                    "query": query,
                    "collection_window": window,
                    "article": {
                        **article,
                        "source_account_id": account_id,
                        "source_account_name": account_name,
                    },
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
    authorization = {
        "authorized": False,
        "admin_authorized": False,
        "login_state": "unknown",
        "message": "WeRSS 服务不可用",
        "qr_available": False,
    }
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
        authorization = verify_werss_wechat_authorization(normalized, session=session)
    rss_status = check_werss(normalized)
    docker_available = managed_werss_start_available() and shutil.which("docker") is not None
    public_base_url = public_werss_base_url(normalized)
    ready = service_online and rss_status["status"] == "online" and bool(subscriptions) and bool(authorization["authorized"])
    if ready:
        status_message = "WeRSS 公众号信源可用"
    elif service_online and not authorization["authorized"]:
        status_message = str(authorization["message"])
    elif subscription_error:
        status_message = subscription_error
    else:
        status_message = rss_status["message"]
    return {
        "status": "online" if ready else ("pending" if service_online else "offline"),
        "message": status_message,
        "checked_at": rss_status["checked_at"],
        "ready": ready,
        "service_online": service_online,
        "rss_online": rss_status["status"] == "online",
        "wechat_authorized": bool(authorization["authorized"]),
        "wechat_login_state": authorization["login_state"],
        "wechat_message": authorization["message"],
        "admin_authorized": bool(authorization["admin_authorized"]),
        "qr_available": bool(authorization["qr_available"]),
        "subscription_count": len(subscriptions),
        "subscriptions": subscriptions,
        "subscription_error": subscription_error,
        "docker_available": docker_available,
        "docker_engine_available": docker_engine_available() if docker_available else False,
        "managed_setup_available": managed_werss_start_available() and WERSS_COMPOSE_PATH.exists(),
        "management_url": f"{public_base_url}/" if public_base_url else "",
        "wechat_status_url": f"{public_base_url}/wechat-status" if public_base_url else "",
        "subscription_url": f"{public_base_url}/wechat/mp" if public_base_url else "",
        "add_subscription_url": f"{public_base_url}/add-subscription" if public_base_url else "",
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


def managed_werss_start_available() -> bool:
    return WERSS_RUNTIME_MODE not in {"compose", "external"}


def start_managed_werss() -> None:
    if not managed_werss_start_available():
        raise RuntimeError("WeRSS 由 Docker Compose 统一管理。请在项目目录执行启动脚本，并确认 werss 容器健康。")
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
