from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests

from backend.http_policy import browser_headers

DEFAULT_ITICK_API_BASE = "https://api0.itick.org"
DEFAULT_ITICK_SYMBOLS = ("HK:700", "US:AAPL", "SH:600519")
MASKED_SECRET = "****************"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[\r\n,;，；]+", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_itick_config(config: dict[str, Any] | None, include_fallback: bool = True) -> dict[str, Any]:
    config = dict(config or {})
    api_base = str(config.get("api_base") or config.get("base_url") or (DEFAULT_ITICK_API_BASE if include_fallback else "")).strip()
    if api_base and "://" not in api_base:
        api_base = f"https://{api_base}"
    symbols = _string_list(config.get("default_symbols") or config.get("symbols"))
    if not symbols and include_fallback:
        symbols = list(DEFAULT_ITICK_SYMBOLS)
    try:
        timeout_seconds = int(config.get("timeout_seconds") or 20)
    except (TypeError, ValueError):
        timeout_seconds = 20
    try:
        kline_type = int(config.get("kline_type") or 2)
    except (TypeError, ValueError):
        kline_type = 2
    try:
        kline_limit = int(config.get("kline_limit") or 60)
    except (TypeError, ValueError):
        kline_limit = 60
    return {
        "adapter": "itick_market_data",
        "api_base": api_base.rstrip("/") if api_base else "",
        "api_key": str(config.get("api_key") or "").strip(),
        "default_symbols": symbols,
        "kline_type": max(1, min(kline_type, 10)),
        "kline_limit": max(1, min(kline_limit, 300)),
        "timeout_seconds": max(3, min(timeout_seconds, 60)),
    }


def public_itick_config(config: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_itick_config(config)
    configured = bool(normalized.get("api_key"))
    normalized["api_key_configured"] = configured
    normalized["api_key"] = MASKED_SECRET if configured else ""
    return normalized


def _parse_symbol(value: str) -> dict[str, str] | None:
    value = value.strip().upper()
    if not value:
        return None
    explicit = re.fullmatch(r"([A-Z]{2,6})\s*[:/.\-]\s*([A-Z0-9._-]+)", value)
    if explicit:
        return {"region": explicit.group(1), "code": explicit.group(2)}
    if re.fullmatch(r"\d{6}", value):
        return {"region": "SH" if value.startswith(("5", "6", "9")) else "SZ", "code": value}
    if re.fullmatch(r"\d{4,5}", value):
        return {"region": "HK", "code": value.lstrip("0") or value}
    if re.fullmatch(r"[A-Z]{1,6}", value):
        return {"region": "US", "code": value}
    return None


def symbols_for_query(query: str, default_symbols: list[str]) -> list[dict[str, str]]:
    candidates: list[str] = []
    if query.strip():
        candidates.extend(match.group(0) for match in re.finditer(r"[A-Za-z]{2,6}\s*[:/.\-]\s*[A-Za-z0-9._-]+", query))
        candidates.extend(match.group(0) for match in re.finditer(r"(?<!\d)\d{4,6}(?!\d)", query))
        candidates.extend(match.group(0) for match in re.finditer(r"\b[A-Z]{1,6}\b", query.upper()))
    if not candidates:
        candidates = list(default_symbols)
    parsed: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        symbol = _parse_symbol(candidate)
        if not symbol:
            continue
        key = (symbol["region"], symbol["code"])
        if key not in seen:
            parsed.append(symbol)
            seen.add(key)
    return parsed[:20]


def _endpoint_url(api_base: str, endpoint: str) -> str:
    base = api_base.rstrip("/")
    path = urlsplit(base).path.rstrip("/")
    suffix = endpoint
    for product in ("stock", "crypto", "forex", "indices", "future", "fund"):
        prefix = f"/{product}"
        if path.endswith(prefix) and endpoint.startswith(f"{prefix}/"):
            suffix = endpoint[len(prefix):]
            break
    return f"{base}{suffix}"


def _unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "result", "items"):
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
    return payload


def _request_json(session: requests.Session, config: dict[str, Any], endpoint: str, params: dict[str, Any]) -> Any:
    api_base = str(config.get("api_base") or DEFAULT_ITICK_API_BASE)
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("iTick API Key is not configured")
    response = session.get(
        _endpoint_url(api_base, endpoint),
        params=params,
        headers={**browser_headers(), "accept": "application/json", "token": api_key},
        timeout=int(config.get("timeout_seconds") or 20),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"iTick returned non-JSON response: {response.text[:200]}") from exc
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0, 200, "0", "200"):
            message = payload.get("msg") or payload.get("message") or payload.get("error") or code
            raise RuntimeError(f"iTick API error: {message}")
    return payload


def _first_record(value: Any) -> Any:
    value = _unwrap_payload(value)
    if isinstance(value, list) and value:
        return value[0]
    return value


def _find_timestamp(value: Any) -> datetime | None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"t", "ts", "time", "timestamp", "datetime", "date", "time_millis", "timemillis"}:
                parsed = _parse_timestamp(item)
                if parsed:
                    return parsed
        for item in value.values():
            found = _find_timestamp(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_timestamp(item)
            if found:
                return found
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10,13}", text):
            return _parse_timestamp(int(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
    return None


def _safe_host(api_base: str) -> str:
    return urlsplit(api_base).netloc or api_base.replace("https://", "").replace("http://", "").split("/")[0]


def itick_status(config: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    config = normalize_itick_config(config)
    symbols = symbols_for_query("", config["default_symbols"])
    if not symbols:
        return {"status": "offline", "message": "iTick default symbols are empty", "checked_at": checked_at}
    session = session or requests.Session()
    symbol = symbols[0]
    try:
        payload = _request_json(session, config, "/stock/quote", symbol)
    except Exception as exc:
        return {"status": "offline", "message": f"iTick API unavailable: {exc}", "checked_at": checked_at}
    quote = _first_record(payload)
    return {
        "status": "online",
        "message": f"iTick 行情 API 可用，已验证 {symbol['region']}:{symbol['code']}",
        "checked_at": checked_at,
        "sample_symbol": symbol,
        "sample_available": bool(quote),
        "api_base_host": _safe_host(config["api_base"]),
    }


def collect_itick_market_data(
    channel_id: str,
    window: dict[str, str],
    query: str = "",
    config: dict[str, Any] | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    config = normalize_itick_config(config)
    symbols = symbols_for_query(query, config["default_symbols"])
    if not symbols:
        raise RuntimeError("No iTick symbols were available for collection")
    session = session or requests.Session()
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    snapshots: list[dict[str, str]] = []
    failures: dict[str, str] = {}
    for symbol in symbols:
        symbol_key = f"{symbol['region']}:{symbol['code']}"
        try:
            quote_payload = _request_json(session, config, "/stock/quote", symbol)
            kline_payload = _request_json(
                session,
                config,
                "/stock/kline",
                {**symbol, "kType": config["kline_type"], "limit": config["kline_limit"]},
            )
        except Exception as exc:
            failures[symbol_key] = str(exc)
            continue
        quote = _first_record(quote_payload)
        occurred_at_dt = _find_timestamp(quote) or _find_timestamp(kline_payload) or datetime.now().astimezone()
        payload = {
            "platform": "itick_market_data",
            "adapter": "itick_rest",
            "symbol": symbol,
            "query": query,
            "collection_window": window,
            "collected_at": collected_at,
            "api_base_host": _safe_host(config["api_base"]),
            "quote": quote_payload,
            "kline": kline_payload,
            "diagnostics": {"quote_available": bool(quote), "kline_available": bool(_unwrap_payload(kline_payload))},
        }
        snapshots.append(
            {
                "channel_id": channel_id,
                "occurred_at": occurred_at_dt.isoformat(timespec="seconds"),
                "source_url": f"itick://stock/{symbol['region']}/{symbol['code']}",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            }
        )
    if not snapshots and failures:
        raise RuntimeError(json.dumps(failures, ensure_ascii=False))
    if failures:
        snapshots.append(
            {
                "channel_id": channel_id,
                "occurred_at": collected_at,
                "source_url": f"itick://diagnostics/{collected_at}",
                "content": json.dumps(
                    {
                        "platform": "itick_market_data",
                        "adapter": "itick_rest",
                        "category": "collector_diagnostics",
                        "query": query,
                        "collection_window": window,
                        "api_base_host": _safe_host(config["api_base"]),
                        "failures": failures,
                    },
                    ensure_ascii=False,
                ),
            }
        )
    return snapshots
