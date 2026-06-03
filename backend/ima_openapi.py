from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

DEFAULT_IMA_BASE_URL = os.environ.get("ALPHADESK_IMA_BASE_URL", "https://ima.qq.com").strip().rstrip("/")
DEFAULT_IMA_SKILL_DOWNLOAD_URL = os.environ.get(
    "ALPHADESK_IMA_SKILL_DOWNLOAD_URL",
    "https://app-dl.ima.qq.com/skills/ima-skills-1.1.7.zip",
).strip()
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("ALPHADESK_IMA_TIMEOUT_SECONDS", "30") or "30")
DEFAULT_RESULT_LIMIT = int(os.environ.get("ALPHADESK_IMA_RESULT_LIMIT", "10") or "10")


def read_user_config(name: str) -> str:
    try:
        return (Path.home() / ".config" / "ima" / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def config_has_explicit_credentials(config: dict[str, Any] | None) -> bool:
    return isinstance(config, dict) and ("client_id" in config or "api_key" in config)


def env_credentials() -> tuple[str, str]:
    client_id = (
        os.environ.get("ALPHADESK_IMA_CLIENT_ID", "").strip()
        or os.environ.get("IMA_OPENAPI_CLIENTID", "").strip()
        or os.environ.get("IMA_CLIENT_ID", "").strip()
        or read_user_config("client_id")
    )
    api_key = (
        os.environ.get("ALPHADESK_IMA_API_KEY", "").strip()
        or os.environ.get("IMA_OPENAPI_APIKEY", "").strip()
        or os.environ.get("IMA_API_KEY", "").strip()
        or read_user_config("api_key")
    )
    return client_id, api_key


def normalize_ima_config(config: dict[str, Any] | None = None, include_fallback: bool = True) -> dict[str, Any]:
    raw = config or {}
    explicit_credentials = config_has_explicit_credentials(raw)
    env_client_id, env_api_key = env_credentials()
    client_id = str(raw.get("client_id") or "").strip()
    api_key = str(raw.get("api_key") or "").strip()
    if include_fallback and not explicit_credentials:
        client_id = client_id or env_client_id
        api_key = api_key or env_api_key
    base_url = str(raw.get("base_url") or DEFAULT_IMA_BASE_URL).strip().rstrip("/") or DEFAULT_IMA_BASE_URL
    skill_download_url = str(raw.get("skill_download_url") or DEFAULT_IMA_SKILL_DOWNLOAD_URL).strip()
    timeout_seconds = int(raw.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    result_limit = int(raw.get("result_limit") or DEFAULT_RESULT_LIMIT)
    knowledge_base_ids = raw.get("knowledge_base_ids", [])
    if isinstance(knowledge_base_ids, str):
        knowledge_base_ids = [item.strip() for item in re.split(r"[,;\s]+", knowledge_base_ids) if item.strip()]
    elif isinstance(knowledge_base_ids, list):
        knowledge_base_ids = [str(item).strip() for item in knowledge_base_ids if str(item).strip()]
    else:
        knowledge_base_ids = []
    if include_fallback and not knowledge_base_ids:
        knowledge_base_ids = env_knowledge_base_ids()
    return {
        "adapter": "ima_openapi",
        "client_id": client_id,
        "api_key": api_key,
        "base_url": base_url,
        "skill_download_url": skill_download_url,
        "knowledge_base_ids": knowledge_base_ids,
        "timeout_seconds": max(3, min(120, timeout_seconds)),
        "result_limit": max(1, min(50, result_limit)),
    }


def ima_credentials(config: dict[str, Any] | None = None) -> tuple[str, str]:
    normalized = normalize_ima_config(config)
    return normalized["client_id"], normalized["api_key"]


def ima_configured(config: dict[str, Any] | None = None) -> bool:
    client_id, api_key = ima_credentials(config)
    return bool(client_id and api_key)


def env_knowledge_base_ids() -> list[str]:
    raw = os.environ.get("ALPHADESK_IMA_KNOWLEDGE_BASE_IDS", "").strip() or os.environ.get(
        "IMA_KNOWLEDGE_BASE_IDS", ""
    ).strip()
    return [item.strip() for item in re.split(r"[,;\s]+", raw) if item.strip()]


def configured_knowledge_base_ids(config: dict[str, Any] | None = None) -> list[str]:
    return normalize_ima_config(config).get("knowledge_base_ids", [])


def ima_openapi_request(
    api_path: str,
    body: dict[str, Any],
    timeout: int | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_ima_config(config)
    client_id, api_key = normalized["client_id"], normalized["api_key"]
    if not client_id or not api_key:
        raise RuntimeError("IMA OpenAPI credentials are not configured")
    response = requests.post(
        f"{normalized['base_url']}/{api_path.lstrip('/')}",
        headers={
            "ima-openapi-clientid": client_id,
            "ima-openapi-apikey": api_key,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout or normalized["timeout_seconds"],
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(str(payload.get("msg") or "IMA OpenAPI request failed"))
    return payload.get("data") if isinstance(payload.get("data"), dict) else {}


def normalize_kb(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(item.get("id") or item.get("kb_id") or "").strip(),
        "name": str(item.get("name") or item.get("kb_name") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "base_type": str(item.get("base_type") or "").strip(),
        "role_type": str(item.get("role_type") or "").strip(),
        "content_count": str(item.get("content_count") or "").strip(),
    }


def list_ima_knowledge_bases(query: str = "", limit: int = 20, config: dict[str, Any] | None = None) -> list[dict[str, str]]:
    ids = configured_knowledge_base_ids(config)
    if ids:
        result: list[dict[str, str]] = []
        for start in range(0, len(ids), 20):
            data = ima_openapi_request(
                "openapi/wiki/v1/get_knowledge_base",
                {"ids": ids[start : start + 20]},
                config=config,
            )
            items = data.get("info_list") or data.get("knowledge_base_infos") or data.get("items") or []
            result.extend(normalize_kb(item) for item in items if isinstance(item, dict))
        return [item for item in result if item["id"]]

    cursor = ""
    result: list[dict[str, str]] = []
    while len(result) < limit:
        data = ima_openapi_request(
            "openapi/wiki/v1/search_knowledge_base",
            {"query": query, "cursor": cursor, "limit": min(20, limit - len(result))},
            config=config,
        )
        items = data.get("info_list") or data.get("knowledge_base_infos") or data.get("items") or []
        result.extend(normalize_kb(item) for item in items if isinstance(item, dict))
        if data.get("is_end", True):
            break
        cursor = str(data.get("next_cursor") or "")
        if not cursor:
            break
    return [item for item in result if item["id"]]


def public_kb(item: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in item.items() if key != "id"}


def stable_ima_url(*parts: str) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"ima://knowledge/{digest}"


def clean_highlight(value: object) -> str:
    text = str(value or "")
    return re.sub(r"<[^>]+>", "", text).strip()


def normalize_knowledge_item(item: dict[str, Any], kb: dict[str, str]) -> dict[str, Any]:
    title = str(item.get("title") or item.get("name") or "").strip()
    highlight = clean_highlight(item.get("highlight_content") or item.get("summary") or item.get("content") or "")
    folder_name = str(item.get("folder_name") or item.get("parent_folder_name") or "").strip()
    media_type = item.get("media_type") or item.get("type") or ""
    return {
        "knowledge_base": public_kb(kb),
        "title": title,
        "snippet": highlight,
        "folder": folder_name,
        "media_type": media_type,
    }


def search_or_list_kb_items(
    kb: dict[str, str],
    query: str,
    limit: int,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if query:
        data = ima_openapi_request(
            "openapi/wiki/v1/search_knowledge",
            {"query": query, "knowledge_base_id": kb["id"], "cursor": ""},
            config=config,
        )
        raw_items = data.get("info_list") or data.get("knowledge_info_list") or data.get("items") or []
    else:
        data = ima_openapi_request(
            "openapi/wiki/v1/get_knowledge_list",
            {"knowledge_base_id": kb["id"], "cursor": "", "limit": min(max(limit, 1), 50)},
            config=config,
        )
        raw_items = []
        for key in ("folder_info_list", "folders", "folder_list"):
            raw_items.extend(data.get(key) or [])
        for key in ("knowledge_info_list", "info_list", "items"):
            raw_items.extend(data.get(key) or [])
    return [normalize_knowledge_item(item, kb) for item in raw_items[:limit] if isinstance(item, dict)]


def collect_ima_knowledge_base(
    channel_id: str,
    window: dict[str, str],
    query: str = "",
    config: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    normalized = normalize_ima_config(config)
    result_limit = normalized["result_limit"]
    knowledge_bases = list_ima_knowledge_bases(limit=20, config=normalized)
    if not knowledge_bases:
        raise RuntimeError("IMA returned no accessible knowledge bases")
    snapshots: list[dict[str, str]] = []
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for kb in knowledge_bases:
        items = search_or_list_kb_items(kb, query.strip(), result_limit, normalized)
        payload = {
            "platform": "ima_knowledge_base",
            "adapter": "ima_openapi",
            "query": query,
            "collection_window": window,
            "knowledge_base": public_kb(kb),
            "result_count": len(items),
            "results": items,
        }
        snapshots.append(
            {
                "channel_id": channel_id,
                "occurred_at": collected_at,
                "source_url": stable_ima_url(kb["id"], query, json.dumps(items, ensure_ascii=False, sort_keys=True)),
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            }
        )
    return snapshots


def ima_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    normalized = normalize_ima_config(config)
    if not ima_configured(normalized):
        return {"status": "pending", "message": "IMA OpenAPI 凭证未配置", "checked_at": checked_at, "knowledge_bases": []}
    try:
        knowledge_bases = list_ima_knowledge_bases(limit=20, config=normalized)
        return {
            "status": "online" if knowledge_bases else "pending",
            "message": f"IMA 知识库可用；可访问 {len(knowledge_bases)} 个知识库",
            "checked_at": checked_at,
            "knowledge_bases": [public_kb(item) for item in knowledge_bases],
        }
    except Exception as exc:
        return {
            "status": "offline",
            "message": f"IMA 知识库不可用：{type(exc).__name__}",
            "checked_at": checked_at,
            "knowledge_bases": [],
        }
