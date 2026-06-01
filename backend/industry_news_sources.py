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

    def get(self, url: str, **kwargs) -> requests.Response:
        with self._lock:
            current = self.monotonic()
            wait_for = self._last_request_at + self.min_interval_seconds - current
            if self._last_request_at and wait_for > 0:
                self.sleep(wait_for + random.uniform(0, self.jitter_seconds))
            response = self.session.get(url, **kwargs)
            self._last_request_at = self.monotonic()
        response.raise_for_status()
        return response


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

    def global_news(self, page_size: int = 50) -> list[dict[str, Any]]:
        response = self.eastmoney.get(
            EASTMONEY_GLOBAL_NEWS_URL,
            params={
                "client": "web",
                "biz": "web_724",
                "fastColumn": "102",
                "sortEnd": "",
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

    def stock_news(self, code: str, page_size: int = 30) -> list[dict[str, Any]]:
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
                        "pageIndex": 1,
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

    def announcements(self, code: str, page_size: int = 30) -> list[dict[str, Any]]:
        if code.startswith("6"):
            org_id = f"gssh0{code}"
        elif code.startswith(("8", "4")):
            org_id = f"gsbj0{code}"
        else:
            org_id = f"gssz0{code}"
        response = self.session.post(
            CNINFO_ANNOUNCEMENT_URL,
            data={
                "stock": f"{code},{org_id}",
                "tabName": "fulltext",
                "pageSize": str(page_size),
                "pageNum": "1",
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
            for item in collector.stock_news(code):
                occurred_at = _in_window(item.get("date"), window_start, window_end)
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
                            "metadata": {"stock_code": code},
                        },
                        occurred_at,
                    )
                )
        except Exception as exc:
            record_error("eastmoney_stock_news", exc)
        try:
            for item in collector.announcements(code):
                occurred_at = _in_window(item.get("announcementTime"), window_start, window_end)
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
                            },
                        },
                        occurred_at,
                    )
                )
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
            for item in collector.global_news():
                occurred_at = _in_window(item.get("showTime"), window_start, window_end)
                if not occurred_at:
                    continue
                item_id = _event_id(item, "infoCode", "code", "id", "newsId")
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
                            "metadata": {"event_id": item_id},
                        },
                        occurred_at,
                    )
                )
        except Exception as exc:
            record_error("eastmoney_global_news", exc)

    if errors and snapshots:
        snapshots.append(_diagnostics_snapshot(channel_id, window, query, errors, window_end))
    if not snapshots:
        raise RuntimeError(f"Public industry news returned no usable evidence: {json.dumps(errors, ensure_ascii=False)}")
    return snapshots
