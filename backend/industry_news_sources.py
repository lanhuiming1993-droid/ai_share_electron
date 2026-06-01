from __future__ import annotations

import hashlib
import html
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AlphaDesk/0.1"
EASTMONEY_INDUSTRY_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_GLOBAL_NEWS_URL = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
EASTMONEY_STOCK_INFO_URL = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_STOCK_NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"
CNINFO_ANNOUNCEMENT_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"


def _clean_text(value: object, limit: int = 8_000) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _parse_datetime(value: object, fallback_tz) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=fallback_tz)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    candidates = (
        text.replace("Z", "+00:00"),
        text[:19],
        text[:10],
    )
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=fallback_tz)
        except ValueError:
            continue
    return None


def _in_window(value: object, window_start: datetime, window_end: datetime) -> datetime | None:
    occurred_at = _parse_datetime(value, window_start.tzinfo)
    if occurred_at and window_start <= occurred_at <= window_end:
        return occurred_at
    return None


def _snapshot(channel_id: str, source_url: str, payload: dict[str, Any], occurred_at: datetime) -> dict[str, str]:
    payload.setdefault("source_url", source_url)
    payload.setdefault("occurred_at", occurred_at.isoformat(timespec="seconds"))
    return {
        "channel_id": channel_id,
        "occurred_at": occurred_at.isoformat(timespec="seconds"),
        "source_url": source_url,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _event_id(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(item.get(key) or "").strip()
        if value:
            return value
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class ThrottledSession:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        min_interval_seconds: float = 1.05,
        jitter_seconds: float = 0.15,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self.sleep = sleep
        self.monotonic = monotonic
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        with self._lock:
            current = self.monotonic()
            wait_for = self._last_request_at + self.min_interval_seconds - current
            if self._last_request_at and wait_for > 0:
                self.sleep(wait_for + random.uniform(0, self.jitter_seconds))
            response = self.session.request(method, url, **kwargs)
            self._last_request_at = self.monotonic()
        response.raise_for_status()
        return response

    def get(self, url: str, **kwargs) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self.request("POST", url, **kwargs)


class PublicIndustryNewsCollector:
    def __init__(
        self,
        *,
        eastmoney: ThrottledSession | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.eastmoney = eastmoney or ThrottledSession(session=self.session)
        self.cninfo = ThrottledSession(session=self.session)

    def industry_rankings(self) -> dict[str, Any]:
        response = self.eastmoney.get(
            EASTMONEY_INDUSTRY_URL,
            params={
                "pn": "1",
                "pz": "100",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fs": "m:90+t:2",
                "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
            },
            timeout=15,
        )
        rows = []
        for rank, item in enumerate(response.json().get("data", {}).get("diff", []) or [], start=1):
            rows.append(
                {
                    "rank": rank,
                    "code": item.get("f12", ""),
                    "name": item.get("f14", ""),
                    "change_pct": item.get("f3", 0),
                    "up_count": item.get("f104", 0),
                    "down_count": item.get("f105", 0),
                    "leader": item.get("f140", ""),
                    "leader_change_pct": item.get("f136", 0),
                }
            )
        if not rows:
            raise RuntimeError("Eastmoney industry ranking returned no rows")
        return {"total": len(rows), "top": rows[:20], "bottom": rows[-20:]}

    def global_news(self, page_size: int = 50, sort_end: str = "") -> list[dict[str, Any]]:
        response = self.eastmoney.get(
            EASTMONEY_GLOBAL_NEWS_URL,
            params={
                "client": "web",
                "biz": "web_724",
                "fastColumn": "102",
                "sortEnd": sort_end,
                "pageSize": str(page_size),
                "req_trace": str(uuid.uuid4()),
            },
            headers={"Referer": "https://kuaixun.eastmoney.com/"},
            timeout=12,
        )
        return response.json().get("data", {}).get("fastNewsList", []) or []

    def stock_info(self, code: str) -> dict[str, Any]:
        market_code = 1 if code.startswith(("5", "6", "9")) else 0
        response = self.eastmoney.get(
            EASTMONEY_STOCK_INFO_URL,
            params={
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            },
            timeout=12,
        )
        data = response.json().get("data") or {}
        if not data:
            raise RuntimeError(f"Eastmoney stock profile returned no data for {code}")
        return {
            "code": data.get("f57", code),
            "name": data.get("f58", ""),
            "industry": data.get("f127", ""),
            "total_shares": data.get("f84", 0),
            "float_shares": data.get("f85", 0),
            "market_cap": data.get("f116", 0),
            "float_market_cap": data.get("f117", 0),
            "list_date": str(data.get("f189", "")),
            "price": data.get("f43", 0),
        }

    def stock_news(self, code: str, page_size: int = 30, page_index: int = 1) -> list[dict[str, Any]]:
        callback = "jQuery_alphadesk"
        inner = json.dumps(
            {
                "uid": "",
                "keyword": code,
                "type": ["cmsArticleWebOld"],
                "client": "web",
                "clientType": "web",
                "clientVersion": "curr",
                "param": {
                    "cmsArticleWebOld": {
                        "searchScope": "default",
                        "sort": "default",
                        "pageIndex": page_index,
                        "pageSize": page_size,
                        "preTag": "",
                        "postTag": "",
                    }
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = self.eastmoney.get(
            EASTMONEY_STOCK_NEWS_URL,
            params={"cb": callback, "param": inner},
            headers={"Referer": "https://so.eastmoney.com/"},
            timeout=15,
        )
        text = response.text
        if "(" not in text or ")" not in text:
            raise RuntimeError("Eastmoney stock news returned invalid JSONP")
        data = json.loads(text[text.index("(") + 1 : text.rindex(")")])
        return data.get("result", {}).get("cmsArticleWebOld", []) or []

    def announcements(self, code: str, page_size: int = 30, page_num: int = 1) -> list[dict[str, Any]]:
        if code.startswith("6"):
            org_id = f"gssh0{code}"
        elif code.startswith(("8", "4")):
            org_id = f"gsbj0{code}"
        else:
            org_id = f"gssz0{code}"
        response = self.cninfo.post(
            CNINFO_ANNOUNCEMENT_URL,
            data={
                "stock": f"{code},{org_id}",
                "tabName": "fulltext",
                "pageSize": str(page_size),
                "pageNum": str(page_num),
                "column": "",
                "category": "",
                "plate": "",
                "seDate": "",
                "searchkey": "",
                "secid": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.cninfo.com.cn/new/disclosure",
                "Origin": "https://www.cninfo.com.cn",
            },
            timeout=15,
        )
        response.raise_for_status()
        return response.json().get("announcements", []) or []


def check_public_industry_news() -> dict[str, Any]:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        ranking = PublicIndustryNewsCollector().industry_rankings()
        return {
            "status": "online",
            "message": f"产业趋势公开资讯可用，抽样读取 {ranking['total']} 个行业",
            "checked_at": checked_at,
        }
    except Exception as exc:
        return {
            "status": "offline",
            "message": f"产业趋势公开资讯不可用: {str(exc)[:300]}",
            "checked_at": checked_at,
        }


def _diagnostics_snapshot(
    channel_id: str,
    window: dict[str, str],
    query: str,
    errors: dict[str, str],
    occurred_at: datetime,
) -> dict[str, str]:
    return _snapshot(
        channel_id,
        f"industry-news://collector-diagnostics/{quote(query or 'general')}",
        {
            "platform": "public_industry_news",
            "category": "collector_diagnostics",
            "title": "Public industry news collector diagnostics",
            "content": "Some public HTTP sources were unavailable; retained successful evidence.",
            "source": "alphadesk",
            "collection_window": window,
            "query": query,
            "metadata": {"errors": errors},
        },
        occurred_at,
    )


def collect_public_industry_news(
    channel_id: str,
    window: dict[str, str],
    query: str = "",
    *,
    collector: PublicIndustryNewsCollector | None = None,
) -> list[dict[str, str]]:
    collector = collector or PublicIndustryNewsCollector()
    window_start = datetime.fromisoformat(window["window_start"])
    window_end = datetime.fromisoformat(window["window_end"])
    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", query)
    code = code_match.group(1) if code_match else ""
    snapshots: list[dict[str, str]] = []
    errors: dict[str, str] = {}

    def record_error(name: str, exc: Exception) -> None:
        errors[name] = str(exc)[:1_000]

    if code:
        try:
            profile = collector.stock_info(code)
            snapshots.append(
                _snapshot(
                    channel_id,
                    f"https://quote.eastmoney.com/{'sh' if code.startswith(('5', '6', '9')) else 'sz'}{code}.html",
                    {
                        "platform": "eastmoney_public",
                        "category": "company_profile",
                        "title": f"{profile.get('name') or code} company profile",
                        "content": json.dumps(profile, ensure_ascii=False, default=str),
                        "source": "eastmoney",
                        "collection_window": window,
                        "query": query,
                        "metadata": profile,
                    },
                    window_end,
                )
            )
        except Exception as exc:
            record_error("eastmoney_stock_info", exc)
        try:
            for page_index in range(1, 41):
                items = collector.stock_news(code, page_index=page_index)
                parsed_dates = [_parse_datetime(item.get("date"), window_start.tzinfo) for item in items]
                for item, parsed_at in zip(items, parsed_dates):
                    occurred_at = parsed_at if parsed_at and window_start <= parsed_at <= window_end else None
                    if not occurred_at:
                        continue
                    source_url = str(item.get("url") or "").strip()
                    if not source_url:
                        source_url = f"https://so.eastmoney.com/news/s?keyword={quote(code)}"
                    snapshots.append(
                        _snapshot(
                            channel_id,
                            source_url,
                            {
                                "platform": "eastmoney_public",
                                "category": "company_news",
                                "title": _clean_text(item.get("title"), 500),
                                "content": _clean_text(item.get("content"), 4_000),
                                "source": _clean_text(item.get("mediaName"), 255) or "eastmoney",
                                "collection_window": window,
                                "query": query,
                                "metadata": {"stock_code": code, "page_index": page_index},
                            },
                            occurred_at,
                        )
                    )
                valid_dates = [value for value in parsed_dates if value]
                if not items or len(items) < 30 or (valid_dates and min(valid_dates) < window_start):
                    break
            else:
                errors["eastmoney_stock_news_coverage"] = "pagination limit reached before the requested window start"
        except Exception as exc:
            record_error("eastmoney_stock_news", exc)
        try:
            for page_num in range(1, 41):
                items = collector.announcements(code, page_num=page_num)
                parsed_dates = [_parse_datetime(item.get("announcementTime"), window_start.tzinfo) for item in items]
                for item, parsed_at in zip(items, parsed_dates):
                    occurred_at = parsed_at if parsed_at and window_start <= parsed_at <= window_end else None
                    if not occurred_at:
                        continue
                    announcement_id = str(item.get("announcementId") or "").strip()
                    source_url = f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={quote(announcement_id)}"
                    snapshots.append(
                        _snapshot(
                            channel_id,
                            source_url,
                            {
                                "platform": "cninfo_public",
                                "category": "company_announcement",
                                "title": _clean_text(item.get("announcementTitle"), 500),
                                "content": _clean_text(item.get("announcementTitle"), 4_000),
                                "source": "cninfo",
                                "collection_window": window,
                                "query": query,
                                "metadata": {
                                    "stock_code": code,
                                    "announcement_id": announcement_id,
                                    "announcement_type": item.get("announcementTypeName", ""),
                                    "page_num": page_num,
                                },
                            },
                            occurred_at,
                        )
                    )
                valid_dates = [value for value in parsed_dates if value]
                if not items or len(items) < 30 or (valid_dates and min(valid_dates) < window_start):
                    break
            else:
                errors["cninfo_announcements_coverage"] = "pagination limit reached before the requested window start"
        except Exception as exc:
            record_error("cninfo_announcements", exc)
    else:
        try:
            ranking = collector.industry_rankings()
            snapshots.append(
                _snapshot(
                    channel_id,
                    EASTMONEY_INDUSTRY_URL,
                    {
                        "platform": "eastmoney_public",
                        "category": "industry_ranking",
                        "title": "Eastmoney A-share industry ranking snapshot",
                        "content": json.dumps(ranking, ensure_ascii=False, default=str),
                        "source": "eastmoney",
                        "collection_window": window,
                        "query": query,
                        "metadata": ranking,
                    },
                    window_end,
                )
            )
        except Exception as exc:
            record_error("eastmoney_industry_rankings", exc)
        try:
            sort_end = ""
            seen_ids: set[str] = set()
            for page_number in range(1, 41):
                items = collector.global_news(sort_end=sort_end)
                parsed_dates = [_parse_datetime(item.get("showTime"), window_start.tzinfo) for item in items]
                new_ids = {_event_id(item, "infoCode", "code", "id", "newsId") for item in items} - seen_ids
                if items and not new_ids:
                    errors["eastmoney_global_news_coverage"] = "pagination cursor repeated before the requested window start"
                    break
                for item, parsed_at in zip(items, parsed_dates):
                    item_id = _event_id(item, "infoCode", "code", "id", "newsId")
                    seen_ids.add(item_id)
                    occurred_at = parsed_at if parsed_at and window_start <= parsed_at <= window_end else None
                    if not occurred_at:
                        continue
                    source_url = (
                        str(item.get("url") or item.get("shareUrl") or item.get("articleUrl") or "").strip()
                        or f"https://kuaixun.eastmoney.com/?id={quote(item_id)}"
                    )
                    snapshots.append(
                        _snapshot(
                            channel_id,
                            source_url,
                            {
                                "platform": "eastmoney_public",
                                "category": "industry_news",
                                "title": _clean_text(item.get("title"), 500),
                                "content": _clean_text(item.get("summary") or item.get("title"), 4_000),
                                "source": "eastmoney_7x24",
                                "collection_window": window,
                                "query": query,
                                "metadata": {"event_id": item_id, "page_number": page_number},
                            },
                            occurred_at,
                        )
                    )
                valid_dates = [value for value in parsed_dates if value]
                if not items or len(items) < 50 or (valid_dates and min(valid_dates) < window_start):
                    break
                sort_end = str(items[-1].get("showTime") or "").strip()
                if not sort_end:
                    errors["eastmoney_global_news_coverage"] = "pagination cursor missing before the requested window start"
                    break
            else:
                errors["eastmoney_global_news_coverage"] = "pagination limit reached before the requested window start"
        except Exception as exc:
            record_error("eastmoney_global_news", exc)

    if errors and snapshots:
        snapshots.append(_diagnostics_snapshot(channel_id, window, query, errors, window_end))
    if not snapshots:
        raise RuntimeError(f"Public industry news returned no usable evidence: {json.dumps(errors, ensure_ascii=False)}")
    return snapshots
