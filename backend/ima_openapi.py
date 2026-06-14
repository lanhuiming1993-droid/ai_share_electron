from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests

DEFAULT_IMA_BASE_URL = os.environ.get("ALPHADESK_IMA_BASE_URL", "https://ima.qq.com").strip().rstrip("/")
DEFAULT_IMA_SKILL_DOWNLOAD_URL = os.environ.get(
    "ALPHADESK_IMA_SKILL_DOWNLOAD_URL",
    "https://app-dl.ima.qq.com/skills/ima-skills-1.1.7.zip",
).strip()
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("ALPHADESK_IMA_TIMEOUT_SECONDS", "30") or "30")
DEFAULT_RESULT_LIMIT = int(os.environ.get("ALPHADESK_IMA_RESULT_LIMIT", "10") or "10")
DEFAULT_CONTENT_FETCH_LIMIT = int(os.environ.get("ALPHADESK_IMA_CONTENT_FETCH_LIMIT", "10") or "10")
DEFAULT_CONTENT_MAX_BYTES = int(os.environ.get("ALPHADESK_IMA_CONTENT_MAX_BYTES", "10485760") or "10485760")
DEFAULT_CONTENT_MAX_CHARS = int(os.environ.get("ALPHADESK_IMA_CONTENT_MAX_CHARS", "20000") or "20000")
TEXT_CONTENT_TYPE_MARKERS = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/javascript",
    "application/x-javascript",
    "application/markdown",
    "application/x-ndjson",
)
TEXT_FILE_EXTENSIONS = (".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".html", ".htm", ".xml", ".log")
WORD_CONTENT_TYPE_MARKERS = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)
WORD_FILE_EXTENSIONS = (".docx",)
FOLDER_MEDIA_TYPES = {"99"}
TEXT_MEDIA_TYPES = {"7", "13"}


def read_user_config(name: str) -> str:
    try:
        return (Path.home() / ".config" / "ima" / name).read_text(encoding="utf-8-sig").strip().lstrip("\ufeff")
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
    content_fetch_limit = int(raw.get("content_fetch_limit") or DEFAULT_CONTENT_FETCH_LIMIT)
    content_max_bytes = int(raw.get("content_max_bytes") or DEFAULT_CONTENT_MAX_BYTES)
    content_max_chars = int(raw.get("content_max_chars") or DEFAULT_CONTENT_MAX_CHARS)
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
        "content_fetch_limit": max(0, min(50, content_fetch_limit)),
        "content_max_bytes": max(16_384, min(10_485_760, content_max_bytes)),
        "content_max_chars": max(1_000, min(200_000, content_max_chars)),
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
    payload: dict[str, Any] = {}
    try:
        raw_payload = response.json()
        if isinstance(raw_payload, dict):
            payload = raw_payload
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        message = str(payload.get("msg") or response.text or response.reason or "IMA OpenAPI request failed").strip()
        code = payload.get("code")
        detail = f"IMA OpenAPI {code}: {message}" if code is not None else message
        raise RuntimeError(detail)
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


def safe_error(exc: Exception) -> str:
    return str(exc).replace("\n", " ").strip()[:180] or type(exc).__name__


def is_folder_item(item: dict[str, Any]) -> bool:
    item_type = str(item.get("media_type") or item.get("type") or "").strip().lower()
    if item_type in {"folder", "folder_info"} or item_type in FOLDER_MEDIA_TYPES:
        return True
    return any(key in item for key in ("folder_id", "file_number", "folder_number", "is_top"))


def extract_folder_id(item: dict[str, Any]) -> str:
    return str(item.get("folder_id") or item.get("media_id") or item.get("id") or "").strip()


def extract_media_id(item: dict[str, Any]) -> str:
    if is_folder_item(item):
        return ""
    return str(item.get("media_id") or "").strip()


def clip_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def looks_textual_response(content_type: str, url: str, sample: bytes) -> bool:
    lowered_type = content_type.lower()
    if any(marker in lowered_type for marker in TEXT_CONTENT_TYPE_MARKERS):
        return True
    lowered_url = url.lower().split("?", 1)[0]
    if lowered_url.endswith(TEXT_FILE_EXTENSIONS):
        return True
    if "application/pdf" in lowered_type or "application/octet-stream" in lowered_type:
        return False
    if not sample or b"\x00" in sample[:2048]:
        return False
    try:
        decoded = sample[:2048].decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for char in decoded if char.isprintable() or char.isspace())
    return printable / max(1, len(decoded)) > 0.85


def item_filename(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or item.get("file_name") or "").strip()


def item_media_type(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("media_type") or item.get("type") or "").strip()


def filename_ext(value: str) -> str:
    return Path(value.split("?", 1)[0]).suffix.lower()


def should_treat_as_text(content_type: str, url: str, sample: bytes, item: dict[str, Any] | None = None) -> bool:
    name = item_filename(item)
    media_type = item_media_type(item)
    if media_type in TEXT_MEDIA_TYPES or filename_ext(name) in TEXT_FILE_EXTENSIONS:
        return True
    return looks_textual_response(content_type, url, sample)


def should_treat_as_docx(content_type: str, item: dict[str, Any] | None = None) -> bool:
    lowered_type = content_type.lower()
    name = item_filename(item)
    return any(marker in lowered_type for marker in WORD_CONTENT_TYPE_MARKERS) or filename_ext(name) in WORD_FILE_EXTENSIONS


def docx_to_text(raw: bytes) -> str:
    paragraphs: list[str] = []
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_bytes)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    for paragraph in root.findall(".//w:p", namespace):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs).strip()


def fetch_url_text(url_info: dict[str, Any], config: dict[str, Any], item: dict[str, Any] | None = None) -> dict[str, Any]:
    url = str(url_info.get("url") or "").strip()
    if not url:
        return {"content_status": "unavailable"}
    headers = url_info.get("headers") if isinstance(url_info.get("headers"), dict) else {}
    max_bytes = int(config["content_max_bytes"])
    raw = bytearray()
    truncated = False
    try:
        with requests.get(url, headers=headers, timeout=config["timeout_seconds"], stream=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk:
                    continue
                remaining = max_bytes - len(raw)
                if remaining <= 0:
                    truncated = True
                    break
                raw.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    break
            if should_treat_as_docx(content_type, item):
                if truncated:
                    return {
                        "content_status": "truncated",
                        "content_error": "document exceeds IMA content download limit",
                        "content_type": content_type,
                        "content_bytes": len(raw),
                        "content_truncated": True,
                    }
                try:
                    text, char_truncated = clip_text(docx_to_text(bytes(raw)), int(config["content_max_chars"]))
                except Exception as exc:
                    return {"content_status": "error", "content_error": safe_error(exc), "content_type": content_type}
                if not text:
                    return {"content_status": "empty", "content_type": content_type}
                return {
                    "content": text,
                    "content_status": "ok",
                    "content_type": content_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "content_truncated": truncated or char_truncated,
                }
            if not should_treat_as_text(content_type, url, bytes(raw[:4096]), item):
                return {
                    "content_status": "binary",
                    "content_type": content_type,
                    "content_bytes": len(raw),
                    "content_truncated": truncated,
                }
            encoding = response.encoding or "utf-8"
    except Exception as exc:
        return {"content_status": "error", "content_error": safe_error(exc)}

    text = bytes(raw).decode(encoding, errors="replace").strip()
    if "html" in content_type.lower():
        text = html_to_text(text)
    text, char_truncated = clip_text(text, int(config["content_max_chars"]))
    if not text:
        return {"content_status": "empty", "content_type": content_type}
    return {
        "content": text,
        "content_status": "ok",
        "content_type": content_type,
        "content_truncated": truncated or char_truncated,
    }


def fetch_note_content(note_id: str, config: dict[str, Any]) -> dict[str, Any]:
    if not note_id:
        return {"content_status": "unavailable"}
    try:
        data = ima_openapi_request(
            "openapi/note/v1/get_doc_content",
            {"note_id": note_id, "target_content_format": 0},
            config=config,
        )
    except Exception as exc:
        return {"content_status": "error", "content_error": safe_error(exc)}
    text, truncated = clip_text(str(data.get("content") or "").strip(), int(config["content_max_chars"]))
    if not text:
        return {"content_status": "empty", "media_type": 11}
    return {
        "content": text,
        "content_status": "ok",
        "media_type": 11,
        "content_type": "text/plain",
        "content_truncated": truncated,
    }


def fetch_knowledge_item_content(item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    media_id = extract_media_id(item)
    if not media_id:
        return {"content_status": "missing_media_id" if not is_folder_item(item) else "folder"}
    try:
        media_info = ima_openapi_request("openapi/wiki/v1/get_media_info", {"media_id": media_id}, config=config)
    except Exception as exc:
        return {"content_status": "error", "content_error": safe_error(exc)}

    media_type = media_info.get("media_type") or item.get("media_type") or item.get("type") or ""
    if str(media_type) == "11":
        notebook_ext_info = media_info.get("notebook_ext_info") if isinstance(media_info.get("notebook_ext_info"), dict) else {}
        return fetch_note_content(str(notebook_ext_info.get("notebook_id") or "").strip(), config)

    url_info = media_info.get("url_info") if isinstance(media_info.get("url_info"), dict) else {}
    if url_info:
        result = fetch_url_text(url_info, config, item)
        if result.get("content_status") != "ok":
            result.setdefault("media_type", media_type)
        return result
    return {"content_status": "unavailable", "media_type": media_type}


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


def list_kb_items_recursive(kb: dict[str, str], limit: int, config: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    queue: list[str | None] = [None]
    visited_folders: set[str] = set()
    while queue and len(result) < limit:
        folder_id = queue.pop(0)
        if folder_id:
            if folder_id in visited_folders:
                continue
            visited_folders.add(folder_id)
        cursor = ""
        while len(result) < limit:
            body: dict[str, Any] = {"knowledge_base_id": kb["id"], "cursor": cursor, "limit": min(max(limit, 1), 50)}
            if folder_id:
                body["folder_id"] = folder_id
            data = ima_openapi_request("openapi/wiki/v1/get_knowledge_list", body, config=config)
            folders: list[dict[str, Any]] = []
            for key in ("folder_info_list", "folders", "folder_list"):
                folders.extend(item for item in data.get(key) or [] if isinstance(item, dict))
            for item in data.get("knowledge_list") or []:
                if isinstance(item, dict) and is_folder_item(item):
                    folders.append(item)
            for folder in folders:
                nested_folder_id = extract_folder_id(folder)
                if nested_folder_id and nested_folder_id not in visited_folders:
                    queue.append(nested_folder_id)
            raw_items: list[dict[str, Any]] = []
            for key in ("knowledge_list", "knowledge_info_list", "info_list", "items"):
                raw_items.extend(item for item in data.get(key) or [] if isinstance(item, dict))
            result.extend(item for item in raw_items if not is_folder_item(item))
            if data.get("is_end", True):
                break
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                break
    return result[:limit]


def search_or_list_kb_items(
    kb: dict[str, str],
    query: str,
    limit: int,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_ima_config(config)
    if query:
        data = ima_openapi_request(
            "openapi/wiki/v1/search_knowledge",
            {"query": query, "knowledge_base_id": kb["id"], "cursor": ""},
            config=normalized,
        )
        raw_items = data.get("info_list") or data.get("knowledge_info_list") or data.get("items") or []
    else:
        raw_items = list_kb_items_recursive(kb, limit, normalized)

    items: list[dict[str, Any]] = []
    fetched_content = 0
    content_fetch_limit = int(normalized["content_fetch_limit"])
    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            continue
        normalized_item = normalize_knowledge_item(item, kb)
        if content_fetch_limit > 0 and fetched_content < content_fetch_limit and extract_media_id(item):
            normalized_item.update(fetch_knowledge_item_content(item, normalized))
            fetched_content += 1
        items.append(normalized_item)
    return items


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
