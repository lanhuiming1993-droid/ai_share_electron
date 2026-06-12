from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

DEFAULT_ZSXQ_GROUP_ID = "28888222124181"
MASKED_SECRET_VALUE = "****************"
MCP_PROTOCOL_VERSION = "2025-06-18"


class ZsxqMcpError(RuntimeError):
    pass


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(max(parsed, minimum), maximum)


def normalize_zsxq_mcp_config(config: dict | None) -> dict[str, Any]:
    raw = config or {}
    mcp_url = str(raw.get("mcp_url") or "").strip()
    if mcp_url and not mcp_url.startswith(("http://", "https://")):
        raise ValueError("ZSXQ MCP URL must start with http:// or https://")
    return {
        "adapter": "zsxq_mcp",
        "mcp_url": mcp_url,
        "timeout_seconds": _bounded_int(raw.get("timeout_seconds"), 20, 3, 120),
        "page_limit": _bounded_int(raw.get("page_limit"), 10, 1, 30),
        "max_pages": _bounded_int(raw.get("max_pages"), 10, 1, 100),
        "include_comments": bool(raw.get("include_comments", False)),
        "comment_limit": _bounded_int(raw.get("comment_limit"), 20, 1, 30),
    }


def masked_mcp_url(mcp_url: str) -> str:
    if not mcp_url:
        return ""
    parts = urlsplit(mcp_url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in {"api_key", "token", "access_token", "secret"}:
            query.append((key, MASKED_SECRET_VALUE if value else ""))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def public_zsxq_mcp_config(config: dict | None) -> dict[str, Any]:
    normalized = normalize_zsxq_mcp_config(config)
    configured = bool(normalized["mcp_url"])
    return {
        "adapter": "zsxq_mcp",
        "mcp_url": MASKED_SECRET_VALUE if configured else "",
        "mcp_url_display": masked_mcp_url(normalized["mcp_url"]) if configured else "",
        "mcp_url_configured": configured,
        "timeout_seconds": normalized["timeout_seconds"],
        "page_limit": normalized["page_limit"],
        "max_pages": normalized["max_pages"],
        "include_comments": normalized["include_comments"],
    }


def normalize_zsxq_group_ids(group_ids: list[Any] | None) -> list[str]:
    values = [str(value).strip() for value in (group_ids or []) if str(value).strip()]
    allowed = [value for value in values if value == DEFAULT_ZSXQ_GROUP_ID]
    return allowed or [DEFAULT_ZSXQ_GROUP_ID]


def parse_zsxq_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        return datetime.fromisoformat(text).astimezone()
    except ValueError:
        return None


def _parse_event_stream(response: requests.Response) -> list[dict[str, Any]]:
    text = response.content.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type and text[:1] in "[{":
        payload = json.loads(text)
        return payload if isinstance(payload, list) else [payload]
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []

    def flush_event() -> None:
        if not data_lines:
            return
        data = "".join(data_lines).strip()
        data_lines.clear()
        if data and data != "[DONE]":
            events.append(json.loads(data))

    for line in text.splitlines():
        if not line.strip():
            flush_event()
            continue
        if line.startswith("data:"):
            if data_lines:
                data_lines.append("\n")
            data_lines.append(line[5:].lstrip())
        elif data_lines and not line.startswith(("event:", "id:", "retry:", ":")):
            # Some MCP gateways emit raw multiline text inside a JSON string without
            # repeating the SSE "data:" prefix. Repair that non-standard stream by
            # treating only the physical line break as an escaped newline; the rest
            # of the line may still contain the JSON-RPC closing syntax.
            data_lines.append("\\n")
            data_lines.append(line)
    flush_event()
    return events


def _post_json_rpc(
    session: requests.Session,
    mcp_url: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    session_id: str = "",
) -> tuple[list[dict[str, Any]], str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.post(mcp_url, json=payload, headers=headers, timeout=timeout_seconds)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            response.raise_for_status()
            next_session_id = response.headers.get("mcp-session-id") or session_id
            if response.status_code == 202 or not response.content:
                return [], next_session_id
            return _parse_event_stream(response), next_session_id
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            break
    raise ZsxqMcpError(f"ZSXQ MCP request failed: {last_error}") from last_error


def _response_by_id(events: list[dict[str, Any]], request_id: str) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("id") == request_id:
            if event.get("error"):
                raise ZsxqMcpError(json.dumps(event["error"], ensure_ascii=False))
            result = event.get("result")
            if isinstance(result, dict):
                return result
            return {"value": result}
    raise ZsxqMcpError(f"ZSXQ MCP response missing id {request_id}")


def _escape_control_chars_in_json_strings(text: str) -> str:
    repaired: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            repaired.append(char)
            escaped = False
        elif char == "\\":
            repaired.append(char)
            escaped = True
        elif char == '"':
            repaired.append(char)
            in_string = not in_string
        elif in_string and char == "\r":
            repaired.append("\\n")
            if index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
        elif in_string and char == "\n":
            repaired.append("\\n")
        elif in_string and char == "\t":
            repaired.append("\\t")
        elif in_string and ord(char) < 0x20:
            repaired.append(f"\\u{ord(char):04x}")
        else:
            repaired.append(char)
        index += 1
    return "".join(repaired)


def _loads_json_maybe_repaired(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_escape_control_chars_in_json_strings(text))


def _tool_result_payload(result: dict[str, Any]) -> Any:
    if result.get("isError"):
        texts = [
            str(item.get("text") or "")
            for item in result.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        raise ZsxqMcpError("\n".join(texts).strip() or "ZSXQ MCP tool returned an error")
    content = result.get("content")
    if isinstance(content, list):
        texts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and str(item.get("text") or "").strip()
        ]
        if len(texts) == 1:
            try:
                return _loads_json_maybe_repaired(texts[0])
            except json.JSONDecodeError:
                return texts[0]
        if texts:
            return texts
    return result


def mcp_tool_call(config: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> Any:
    normalized = normalize_zsxq_mcp_config(config)
    mcp_url = normalized["mcp_url"]
    if not mcp_url:
        raise ZsxqMcpError("ZSXQ MCP URL is not configured")
    timeout_seconds = normalized["timeout_seconds"]
    with requests.Session() as session:
        init_events, session_id = _post_json_rpc(
            session,
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": "initialize",
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "alphadesk-zsxq-source", "version": "1.0.0"},
                },
            },
            timeout_seconds,
        )
        _response_by_id(init_events, "initialize")
        _post_json_rpc(
            session,
            mcp_url,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            timeout_seconds,
            session_id,
        )
        call_events, _ = _post_json_rpc(
            session,
            mcp_url,
            {
                "jsonrpc": "2.0",
                "id": "tool-call",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            timeout_seconds,
            session_id,
        )
    return _tool_result_payload(_response_by_id(call_events, "tool-call"))


def _response_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ZsxqMcpError("ZSXQ MCP returned a non-object payload")
    if payload.get("success") is False:
        message = payload.get("message") or payload.get("error") or payload.get("body") or payload
        raise ZsxqMcpError(f"ZSXQ MCP tool failed: {message}")
    data = payload
    for key in ("body", "resp_data", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            data = nested
    return data


def _topics_payload(payload: Any) -> tuple[list[dict[str, Any]], bool, str]:
    data = _response_data(payload)
    topics = data.get("topics_brief") or data.get("topics") or data.get("items") or []
    if not isinstance(topics, list):
        topics = []
    return (
        [topic for topic in topics if isinstance(topic, dict)],
        bool(data.get("has_more")),
        str(data.get("next_end_time") or "").strip(),
    )


def _attachment_refs(topic: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for key in ("images", "files"):
        for item in topic.get(key) or []:
            if isinstance(item, str) and item.strip():
                refs.append(item.strip())
            elif isinstance(item, dict):
                value = item.get("url") or item.get("download_url") or item.get("name") or item.get("file_id")
                if value:
                    refs.append(str(value).strip())
    return [value for value in refs if value]


def _topic_snapshot(
    channel_id: str,
    topic: dict[str, Any],
    window: dict[str, str],
    query: str,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    topic_id = str(topic.get("topic_id") or "").strip()
    group = topic.get("group") if isinstance(topic.get("group"), dict) else {}
    group_id = str(group.get("group_id") or DEFAULT_ZSXQ_GROUP_ID).strip()
    occurred = parse_zsxq_datetime(topic.get("create_time")) or datetime.now().astimezone()
    content = str(topic.get("content") or topic.get("title") or "").strip()
    payload = {
        "platform": "zsxq_mcp",
        "adapter": "zsxq_mcp",
        "collection_window": window,
        "query": query,
        "topic": topic,
        "group": group,
        "owner": topic.get("owner") if isinstance(topic.get("owner"), dict) else {},
        "content": content,
        "attachments": _attachment_refs(topic),
        "comments": comments or [],
    }
    return {
        "channel_id": channel_id,
        "occurred_at": occurred.isoformat(timespec="seconds"),
        "source_url": f"zsxq://group/{group_id}/topic/{topic_id or 'unknown'}",
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _comments_for_topic(config: dict[str, Any], topic_id: str) -> list[dict[str, Any]]:
    if not topic_id:
        return []
    payload = mcp_tool_call(
        config,
        "get_topic_comments",
        {"topic_id": topic_id, "limit": normalize_zsxq_mcp_config(config)["comment_limit"]},
    )
    data = _response_data(payload)
    comments = data.get("comments") or data.get("comments_brief") or data.get("items") or []
    return [comment for comment in comments if isinstance(comment, dict)]


def collect_zsxq_mcp(channel: dict[str, Any], window: dict[str, str], query: str = "") -> list[dict[str, str]]:
    config = normalize_zsxq_mcp_config(channel.get("request_config") or {})
    group_ids = normalize_zsxq_group_ids(channel.get("group_ids") or [])
    window_start = datetime.fromisoformat(window["window_start"]).astimezone()
    window_end = datetime.fromisoformat(window["window_end"]).astimezone()
    snapshots: list[dict[str, str]] = []
    for group_id in group_ids:
        cursor = ""
        for _page in range(config["max_pages"]):
            arguments = {"group_id": group_id, "scope": "all", "limit": config["page_limit"]}
            if cursor:
                arguments["end_time"] = cursor
            topics, has_more, next_cursor = _topics_payload(mcp_tool_call(config, "get_group_topics", arguments))
            if not topics:
                break
            oldest: datetime | None = None
            for topic in topics:
                occurred = parse_zsxq_datetime(topic.get("create_time"))
                if not occurred:
                    continue
                oldest = min(oldest, occurred) if oldest else occurred
                if occurred < window_start or occurred > window_end:
                    continue
                comments = _comments_for_topic(config, str(topic.get("topic_id") or "")) if config["include_comments"] else []
                snapshots.append(_topic_snapshot(channel["id"], topic, window, query, comments))
            if oldest and oldest < window_start:
                break
            if not has_more or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    return sorted(snapshots, key=lambda item: item["occurred_at"], reverse=True)


def zsxq_mcp_status(config: dict[str, Any], group_ids: list[Any] | None = None) -> dict[str, Any]:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    normalized = normalize_zsxq_mcp_config(config)
    if not normalized["mcp_url"]:
        return {
            "status": "pending",
            "message": "ZSXQ MCP URL is not configured",
            "checked_at": checked_at,
            "group_id": DEFAULT_ZSXQ_GROUP_ID,
        }
    group_id = normalize_zsxq_group_ids(group_ids)[0]
    try:
        topics, _has_more, _next_cursor = _topics_payload(
            mcp_tool_call(normalized, "get_group_topics", {"group_id": group_id, "scope": "all", "limit": 1})
        )
        return {
            "status": "online",
            "message": f"ZSXQ MCP source is available; sampled {len(topics)} topic(s)",
            "checked_at": checked_at,
            "group_id": group_id,
            "topic_count": len(topics),
        }
    except Exception as exc:
        return {
            "status": "offline",
            "message": f"ZSXQ MCP source check failed: {exc}",
            "checked_at": checked_at,
            "group_id": group_id,
        }
