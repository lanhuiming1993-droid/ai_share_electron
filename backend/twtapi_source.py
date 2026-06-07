from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus, urlsplit

import requests

from backend.http_policy import browser_headers

DEFAULT_TWTAPI_API_BASE = "https://api.twtapi.com/api/v1/twitter"
DEFAULT_TWTAPI_QUERIES = ("A股", "半导体", "光伏", "机器人")
DEFAULT_TWTAPI_TRACKED_USERS: tuple[str, ...] = ()
MASKED_SECRET = "****************"
VALID_RESULT_TYPES = {"Top", "Latest", "User", "Image", "Video"}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[\r\n,;，；]+", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_twtapi_username(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split()[0].strip().rstrip("/")
    match = re.search(r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/([^/?#]+)", text, re.IGNORECASE)
    if match:
        text = match.group(1)
    text = text.strip().lstrip("@").split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", text):
        return ""
    return text


def _username_list(value: Any) -> list[str]:
    users: list[str] = []
    seen: set[str] = set()
    for item in _string_list(value):
        username = normalize_twtapi_username(item)
        if not username:
            continue
        key = username.casefold()
        if key in seen:
            continue
        users.append(username)
        seen.add(key)
    return users


def normalize_twtapi_config(config: dict[str, Any] | None, include_fallback: bool = True) -> dict[str, Any]:
    config = dict(config or {})
    api_base = str(config.get("api_base") or config.get("base_url") or (DEFAULT_TWTAPI_API_BASE if include_fallback else "")).strip()
    if api_base and "://" not in api_base:
        api_base = f"https://{api_base}"
    if api_base:
        parsed = urlsplit(api_base)
        if parsed.netloc == "api.twtapi.com" and parsed.path.rstrip("/") in {"", "/"}:
            api_base = DEFAULT_TWTAPI_API_BASE
    raw_queries = config.get("default_queries") if "default_queries" in config else config.get("queries")
    queries = _string_list(raw_queries)
    if not queries and include_fallback and raw_queries is None:
        queries = list(DEFAULT_TWTAPI_QUERIES)
    if "tracked_users" in config:
        raw_tracked_users = config.get("tracked_users")
    elif "usernames" in config:
        raw_tracked_users = config.get("usernames")
    else:
        raw_tracked_users = config.get("screen_names")
    tracked_users = _username_list(raw_tracked_users)
    if not tracked_users and include_fallback and raw_tracked_users is None:
        tracked_users = list(DEFAULT_TWTAPI_TRACKED_USERS)
    result_type = str(config.get("result_type") or "Latest").strip() or "Latest"
    if result_type not in VALID_RESULT_TYPES:
        result_type = "Latest"
    try:
        max_results = int(config.get("max_results") or config.get("count") or 20)
    except (TypeError, ValueError):
        max_results = 20
    try:
        timeout_seconds = int(config.get("timeout_seconds") or 20)
    except (TypeError, ValueError):
        timeout_seconds = 20
    lang = str(config.get("lang") or "zh").strip() or "zh"
    return {
        "adapter": "x_twtapi",
        "api_base": api_base.rstrip("/") if api_base else "",
        "api_key": str(config.get("api_key") or "").strip(),
        "default_queries": queries[:100],
        "tracked_users": tracked_users[:100],
        "result_type": result_type,
        "max_results": max(1, min(max_results, 100)),
        "timeout_seconds": max(3, min(timeout_seconds, 60)),
        "lang": lang[:10],
    }


def public_twtapi_config(config: dict[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_twtapi_config(config)
    configured = bool(normalized.get("api_key"))
    normalized["api_key_configured"] = configured
    normalized["api_key"] = MASKED_SECRET if configured else ""
    return normalized


def _safe_host(api_base: str) -> str:
    return urlsplit(api_base).netloc or api_base.replace("https://", "").replace("http://", "").split("/")[0]


def _endpoint_url(api_base: str, endpoint: str) -> str:
    base = api_base.rstrip("/")
    endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    return f"{base}{endpoint}"


def _request_json(session: requests.Session, config: dict[str, Any], endpoint: str, params: dict[str, Any]) -> Any:
    api_key = str(config.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("TwtAPI API Key is not configured")
    response = session.get(
        _endpoint_url(str(config.get("api_base") or DEFAULT_TWTAPI_API_BASE), endpoint),
        params=params,
        headers={
            **browser_headers(),
            "accept": "application/json",
            "X-API-Key": api_key,
            "X-Lang": str(config.get("lang") or "zh"),
        },
        timeout=int(config.get("timeout_seconds") or 20),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"TwtAPI returned non-JSON response: {response.text[:200]}") from exc
    if isinstance(payload, dict):
        code = payload.get("code")
        if code not in (None, 0, 200, "0", "200"):
            message = payload.get("msg") or payload.get("message") or payload.get("error") or code
            raise RuntimeError(f"TwtAPI API error: {message}")
        if payload.get("success") is False:
            message = payload.get("msg") or payload.get("message") or payload.get("error") or "success=false"
            raise RuntimeError(f"TwtAPI API error: {message}")
    return payload


def _unwrap_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "result", "items"):
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
    return payload


def twtapi_tweet_id(tweet: dict[str, Any]) -> str:
    for key in ("id", "id_str", "tweet_id", "rest_id", "restId"):
        value = tweet.get(key)
        if value not in (None, ""):
            return str(value)
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    for key in ("id_str", "id", "tweet_id"):
        value = legacy.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def twtapi_tweet_text(tweet: dict[str, Any]) -> str:
    for key in ("text", "full_text", "content"):
        value = tweet.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    for key in ("full_text", "text"):
        value = legacy.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    note = tweet.get("note_tweet") if isinstance(tweet.get("note_tweet"), dict) else {}
    note_result = note.get("note_tweet_results") if isinstance(note.get("note_tweet_results"), dict) else {}
    note_payload = note_result.get("result") if isinstance(note_result.get("result"), dict) else {}
    value = note_payload.get("text")
    return str(value or "").strip()


def twtapi_tweet_user(tweet: dict[str, Any]) -> dict[str, str]:
    candidates: list[dict[str, Any]] = []
    for key in ("user", "author"):
        value = tweet.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    core = tweet.get("core") if isinstance(tweet.get("core"), dict) else {}
    user_results = core.get("user_results") if isinstance(core.get("user_results"), dict) else {}
    result = user_results.get("result") if isinstance(user_results.get("result"), dict) else {}
    if result:
        candidates.append(result)
    legacy_user = tweet.get("legacy", {}).get("user") if isinstance(tweet.get("legacy"), dict) else None
    if isinstance(legacy_user, dict):
        candidates.append(legacy_user)
    for user in candidates:
        legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else {}
        profile_core = user.get("core") if isinstance(user.get("core"), dict) else {}
        username = str(
            user.get("screen_name")
            or user.get("username")
            or user.get("userName")
            or profile_core.get("screen_name")
            or profile_core.get("username")
            or legacy.get("screen_name")
            or legacy.get("username")
            or ""
        ).strip().lstrip("@")
        name = str(user.get("name") or profile_core.get("name") or legacy.get("name") or username).strip()
        user_id = str(user.get("id") or user.get("id_str") or user.get("rest_id") or legacy.get("id_str") or "").strip()
        if username or name or user_id:
            return {"id": user_id, "username": username, "name": name}
    return {"id": "", "username": "", "name": ""}


def twtapi_tweet_url(tweet: dict[str, Any], fallback: str = "") -> str:
    for key in ("url", "source_url", "link", "expanded_url"):
        value = tweet.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    tweet_id = twtapi_tweet_id(tweet)
    user = twtapi_tweet_user(tweet)
    if tweet_id and user.get("username"):
        return f"https://x.com/{user['username']}/status/{tweet_id}"
    if tweet_id:
        return f"https://x.com/i/web/status/{tweet_id}"
    return fallback


def twtapi_tweet_timestamp(tweet: dict[str, Any]) -> str:
    values: list[Any] = []
    for key in ("created_at", "createdAt", "time", "timestamp", "date"):
        values.append(tweet.get(key))
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    values.append(legacy.get("created_at"))
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed:
            return parsed.isoformat(timespec="seconds")
    return ""


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
            pass
        try:
            return parsedate_to_datetime(text).astimezone()
        except (TypeError, ValueError, OSError):
            return None
    return None


def _looks_like_tweet(value: dict[str, Any]) -> bool:
    if twtapi_tweet_id(value) and twtapi_tweet_text(value):
        return True
    legacy = value.get("legacy") if isinstance(value.get("legacy"), dict) else {}
    return bool((legacy.get("id_str") or legacy.get("id")) and (legacy.get("full_text") or legacy.get("text")))


def extract_twtapi_tweets(payload: Any, limit: int = 200) -> list[dict[str, Any]]:
    tweets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        if len(tweets) >= limit:
            return
        key = twtapi_tweet_id(item) or json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)[:500]
        if key in seen:
            return
        seen.add(key)
        tweets.append(item)

    def walk(value: Any) -> None:
        if len(tweets) >= limit:
            return
        value = _unwrap_payload(value)
        if isinstance(value, dict):
            if _looks_like_tweet(value):
                add(value)
                return
            for key in ("tweet", "tweets", "items", "results", "statuses", "entries", "data", "search_results", "timeline"):
                if key in value:
                    walk(value[key])
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
                if len(tweets) >= limit:
                    break

    walk(payload)
    return tweets


def _extract_user_id(payload: Any) -> str:
    payload = _unwrap_payload(payload)
    if isinstance(payload, (int, float)):
        value = str(int(payload))
        return value if value.isdigit() else ""
    if isinstance(payload, str):
        value = payload.strip()
        return value if re.fullmatch(r"\d{2,30}", value) else ""
    if isinstance(payload, dict):
        for key in ("user_id", "rest_id", "restId", "id_str", "id"):
            value = payload.get(key)
            if value not in (None, ""):
                found = _extract_user_id(value)
                if found:
                    return found
        for key in ("user", "result", "legacy", "data", "profile", "user_results"):
            value = payload.get(key)
            if isinstance(value, (dict, list, str, int, float)):
                found = _extract_user_id(value)
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = _extract_user_id(value)
                if found:
                    return found
    if isinstance(payload, list):
        for item in payload:
            found = _extract_user_id(item)
            if found:
                return found
    return ""


def resolve_twtapi_user(session: requests.Session, config: dict[str, Any], username: str) -> dict[str, Any]:
    username = normalize_twtapi_username(username)
    if not username:
        raise RuntimeError("Invalid TwtAPI username")
    errors: list[str] = []
    for endpoint, params in (
        ("/UsernameToUserId", {"username": username}),
        ("/UserResultByScreenName", {"username": username}),
    ):
        try:
            payload = _request_json(session, config, endpoint, params)
        except Exception as exc:
            errors.append(f"{endpoint}: {exc}")
            continue
        user_id = _extract_user_id(payload)
        if user_id:
            return {"username": username, "user_id": user_id, "profile": payload, "resolver_endpoint": endpoint.lstrip("/")}
        errors.append(f"{endpoint}: response did not include a user id")
    raise RuntimeError("; ".join(errors) or f"TwtAPI user not found: {username}")


def _collect_tracked_user(
    session: requests.Session,
    config: dict[str, Any],
    username: str,
    channel_id: str,
    window: dict[str, str],
    query: str,
    collected_at: str,
) -> dict[str, str]:
    resolved = resolve_twtapi_user(session, config, username)
    raw_payload = _request_json(session, config, "/UserTweets", {"user_id": resolved["user_id"]})
    tweets = extract_twtapi_tweets(raw_payload, limit=config["max_results"])
    payload = {
        "platform": "x_twtapi",
        "adapter": "twtapi_user_timeline",
        "endpoint": "UserTweets",
        "query": query,
        "collection_window": window,
        "collected_at": collected_at,
        "api_base_host": _safe_host(config["api_base"]),
        "tracked_user": {"username": resolved["username"], "user_id": resolved["user_id"]},
        "profile": resolved["profile"],
        "resolver_endpoint": resolved["resolver_endpoint"],
        "max_results": config["max_results"],
        "tweets": tweets,
        "response": raw_payload,
        "diagnostics": {"tweet_count": len(tweets)},
    }
    return {
        "channel_id": channel_id,
        "occurred_at": collected_at,
        "source_url": f"https://x.com/{resolved['username']}",
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def twtapi_status(config: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    config = normalize_twtapi_config(config)
    session = session or requests.Session()
    if config["tracked_users"]:
        sample_user = config["tracked_users"][0]
        try:
            resolved = resolve_twtapi_user(session, config, sample_user)
            payload = _request_json(session, config, "/UserTweets", {"user_id": resolved["user_id"]})
        except Exception as exc:
            return {"status": "offline", "message": f"TwtAPI user timeline unavailable: {exc}", "checked_at": checked_at}
        return {
            "status": "online",
            "message": f"TwtAPI X user timeline is available, verified @{resolved['username']}",
            "checked_at": checked_at,
            "sample_user": resolved["username"],
            "sample_user_id": resolved["user_id"],
            "sample_available": bool(extract_twtapi_tweets(payload, limit=5)),
            "api_base_host": _safe_host(config["api_base"]),
        }
    sample_query = (config["default_queries"] or ["A股"])[0]
    try:
        payload = _request_json(
            session,
            config,
            "/Search",
            {"q": sample_query, "type": config["result_type"], "count": 1},
        )
    except Exception as exc:
        return {"status": "offline", "message": f"TwtAPI unavailable: {exc}", "checked_at": checked_at}
    return {
        "status": "online",
        "message": f"TwtAPI X search is available, verified query: {sample_query}",
        "checked_at": checked_at,
        "sample_query": sample_query,
        "sample_available": bool(extract_twtapi_tweets(payload, limit=5)),
        "api_base_host": _safe_host(config["api_base"]),
    }


def collect_twtapi_search(
    channel_id: str,
    window: dict[str, str],
    query: str = "",
    config: dict[str, Any] | None = None,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    config = normalize_twtapi_config(config)
    queries = [query.strip()] if query.strip() else list(config["default_queries"])
    queries = [item for item in queries if item]
    tracked_users = list(config["tracked_users"])
    if not queries and not tracked_users:
        raise RuntimeError("No TwtAPI search queries or tracked users are configured")
    session = session or requests.Session()
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    snapshots: list[dict[str, str]] = []
    failures: dict[str, str] = {}
    for search_query in queries[:20]:
        try:
            raw_payload = _request_json(
                session,
                config,
                "/Search",
                {"q": search_query, "type": config["result_type"], "count": config["max_results"]},
            )
        except Exception as exc:
            failures[search_query] = str(exc)
            continue
        tweets = extract_twtapi_tweets(raw_payload, limit=config["max_results"])
        payload = {
            "platform": "x_twtapi",
            "adapter": "twtapi_search",
            "endpoint": "Search",
            "query": search_query,
            "collection_window": window,
            "collected_at": collected_at,
            "api_base_host": _safe_host(config["api_base"]),
            "result_type": config["result_type"],
            "max_results": config["max_results"],
            "tweets": tweets,
            "response": raw_payload,
            "diagnostics": {"tweet_count": len(tweets)},
        }
        snapshots.append(
            {
                "channel_id": channel_id,
                "occurred_at": collected_at,
                "source_url": f"https://x.com/search?q={quote_plus(search_query)}&f=live",
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            }
        )
    for username in tracked_users[:20]:
        try:
            snapshots.append(_collect_tracked_user(session, config, username, channel_id, window, query.strip(), collected_at))
        except Exception as exc:
            failures[f"@{username}"] = str(exc)
    if not snapshots and failures:
        raise RuntimeError(json.dumps(failures, ensure_ascii=False))
    if failures:
        snapshots.append(
            {
                "channel_id": channel_id,
                "occurred_at": collected_at,
                "source_url": f"x-twtapi://diagnostics/{collected_at}",
                "content": json.dumps(
                    {
                        "platform": "x_twtapi",
                        "adapter": "twtapi_search",
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
