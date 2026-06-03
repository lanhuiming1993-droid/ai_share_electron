from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from backend.http_policy import browser_headers, browser_http_session
from backend.ima_openapi import collect_ima_knowledge_base
from backend.industry_news_sources import collect_public_industry_news
from backend.subprocess_utils import hidden_window_creationflags
from backend.wechat_rss import collect_werss

ROOT = Path(__file__).resolve().parents[1]


def snapshot(channel_id: str, source_url: str, content: str, occurred_at: str | None = None) -> dict[str, str]:
    return {
        "channel_id": channel_id,
        "occurred_at": occurred_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_url": source_url,
        "content": content,
    }


def render_query_url(url: str, query: str) -> str:
    return url.replace("{query}", quote_plus(query)).replace("{query_raw}", query)


def collect_akshare_legacy(channel: dict[str, Any], window: dict[str, str], query: str = "") -> list[dict[str, str]]:
    import akshare as ak

    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", query)
    code = code_match.group(1) if code_match else ""
    datasets: dict[str, Any] = {"window": window, "query": query}
    valid_datasets: list[str] = []
    if code:
        start_date = window["window_start"][:10].replace("-", "")
        end_date = window["window_end"][:10].replace("-", "")
        exchange_code = f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"
        try:
            records = ak.stock_individual_info_em(symbol=code).to_dict(orient="records")
            datasets["stock_individual_info_em"] = records
            if records:
                valid_datasets.append("stock_individual_info_em")
        except Exception as exc:
            datasets["stock_individual_info_em_error"] = str(exc)
        try:
            records = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="",
            ).to_dict(orient="records")
            datasets["stock_zh_a_hist"] = records
            if records:
                valid_datasets.append("stock_zh_a_hist")
        except Exception as exc:
            datasets["stock_zh_a_hist_error"] = str(exc)
        if not any(name in valid_datasets for name in ("stock_zh_a_hist", "stock_zh_a_hist_tx")):
            try:
                records = ak.stock_zh_a_hist_tx(
                    symbol=exchange_code,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                ).to_dict(orient="records")
                datasets["stock_zh_a_hist_tx"] = records
                if records:
                    valid_datasets.append("stock_zh_a_hist_tx")
            except Exception as exc:
                datasets["stock_zh_a_hist_tx_error"] = str(exc)
        if not any(name in valid_datasets for name in ("stock_zh_a_hist", "stock_zh_a_hist_tx")):
            try:
                records = ak.stock_zh_a_daily(
                    symbol=exchange_code,
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                ).to_dict(orient="records")
                datasets["stock_zh_a_daily"] = records
                if records:
                    valid_datasets.append("stock_zh_a_daily")
            except Exception as exc:
                datasets["stock_zh_a_daily_error"] = str(exc)
    else:
        try:
            frame = ak.stock_zh_a_spot_em()
            dataset_name = "stock_zh_a_spot_em"
        except Exception as exc:
            datasets["stock_zh_a_spot_em_error"] = str(exc)
            frame = ak.stock_zh_a_spot()
            dataset_name = "stock_zh_a_spot"
        columns = [name for name in ("代码", "名称", "最新价", "涨跌幅", "成交额", "总市值", "市盈率-动态", "市净率") if name in frame.columns]
        records = (frame[columns] if columns else frame).head(600).to_dict(orient="records")
        datasets[dataset_name] = records
        if records:
            valid_datasets.append(dataset_name)
    if not valid_datasets:
        errors = {key: value for key, value in datasets.items() if key.endswith("_error")}
        raise RuntimeError(f"AkShare returned no usable datasets: {json.dumps(errors, ensure_ascii=False)}")
    datasets["valid_datasets"] = valid_datasets
    content = json.dumps(
        datasets,
        ensure_ascii=False,
        default=str,
    )
    return [snapshot(channel["id"], f"akshare://stock/{code or 'a-share-spot'}", content)]


def collect_akshare(channel: dict[str, Any], window: dict[str, str], query: str = "") -> list[dict[str, str]]:
    code_match = re.search(r"(?<!\d)(\d{6})(?!\d)", query)
    code = code_match.group(1) if code_match else ""
    config = channel.get("request_config") or {}
    component_timeout = min(max(int(config.get("component_timeout_seconds") or 35), 5), 120)
    enabled = [
        component
        for component in ("akshare", "baostock", "tushare")
        if bool(config.get(f"enable_{component}", True))
    ]
    token = str(config.get("tushare_token") or "").strip()
    results: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}

    def collect_component(component: str) -> tuple[str, dict[str, Any]]:
        if component == "tushare" and not token:
            return component, {"status": "skipped", "reason": "TuShare token is not configured"}
        environment = os.environ.copy()
        if component == "tushare":
            environment["ALPHADESK_TUSHARE_TOKEN"] = token
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "backend" / "market_data_sources.py"),
                    component,
                    "--window-json",
                    json.dumps(window, ensure_ascii=False),
                    "--query",
                    query,
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=component_timeout,
                creationflags=hidden_window_creationflags(),
                check=True,
            )
            output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
            if not output_lines:
                raise json.JSONDecodeError("market data subprocess returned no JSON", "", 0)
            payload = json.loads(output_lines[-1])
            return component, {
                "status": "ok" if payload.get("valid_datasets") else "empty",
                "valid_datasets": payload.get("valid_datasets") or [],
                "errors": payload.get("errors") or {},
                "payload": payload,
            }
        except subprocess.TimeoutExpired:
            return component, {"status": "timeout", "reason": f"exceeded {component_timeout} seconds"}
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            return component, {"status": "failed", "reason": detail[-2000:]}
        except (json.JSONDecodeError, OSError) as exc:
            return component, {"status": "failed", "reason": str(exc)}

    with ThreadPoolExecutor(max_workers=max(len(enabled), 1), thread_name_prefix="market-data") as executor:
        futures = [executor.submit(collect_component, component) for component in enabled]
        for future in as_completed(futures):
            component, outcome = future.result()
            diagnostics[component] = {key: value for key, value in outcome.items() if key != "payload"}
            if outcome["status"] == "ok":
                results[component] = outcome["payload"]
    content = json.dumps(
        {
            "adapter": "market_data_aggregate",
            "window": window,
            "query": query,
            "component_priority": ["akshare", "baostock", "tushare"],
            "component_diagnostics": diagnostics,
            "components": results,
            "collection_warning": "" if results else "No market data component returned usable datasets",
        },
        ensure_ascii=False,
        default=str,
    )
    return [snapshot(channel["id"], f"market-data://stock/{code or 'a-share-overview'}", content)]


def collect_mx(channel: dict[str, Any], window: dict[str, str], query: str = "") -> list[dict[str, str]]:
    config = channel.get("request_config") or {}
    token = str(config.get("token") or "").strip()
    base_url = str(config.get("base_url") or "").strip().rstrip("/")
    if not token or not base_url.startswith("https://"):
        raise RuntimeError("MX authorized request replay is not configured; import a fresh HAR session")
    try:
        room_ids = {str(room_id).strip() for room_id in config.get("room_ids") or [] if str(room_id).strip()}
        page_size = min(max(int(config.get("page_size") or 30), 1), 100)
        max_pages_per_room = min(max(int(config.get("max_pages_per_room") or 500), 1), 600)
        request_delay_seconds = min(max(float(config.get("request_delay_seconds") or 0.25), 0.0), 2.0)
        allow_partial_window = bool(config.get("allow_partial_window"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MX request replay limits are invalid") from exc
    if not room_ids:
        raise RuntimeError("MX room whitelist is empty")

    session = browser_http_session()
    session.headers.update(
        {
            "Content-Type": "application/json",
            "token": token,
            "AD": str(config.get("ad") or "1"),
            "version": str(config.get("version") or "4.3.3"),
            "i": str(config.get("i") or "qq"),
            "Referer": str(config.get("referer") or "https://mx.2026.naaifu.cn/"),
        }
    )
    request_count = 0

    def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal request_count
        if request_count:
            time.sleep(request_delay_seconds)
        response = session.post(
            f"{base_url}{path}",
            json={**payload, "tt": int(time.time() * 1000)},
            timeout=30,
        )
        request_count += 1
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 200:
            raise RuntimeError(f"MX request rejected {path}: {data.get('msg') or 'unknown response'}")
        return data

    window_start = datetime.fromisoformat(window["window_start"])
    window_end = datetime.fromisoformat(window["window_end"])
    rooms = post("/api/room/list", {"pages": 1, "limit": 1_000_000}).get("list") or []
    selected_rooms = [room for room in rooms if str(room.get("id") or "").strip() in room_ids]
    missing_room_ids = room_ids - {str(room.get("id") or "").strip() for room in selected_rooms}
    if missing_room_ids:
        raise RuntimeError(f"MX configured rooms are unavailable: {', '.join(sorted(missing_room_ids))}")
    snapshots: list[dict[str, str]] = []

    for room in selected_rooms:
        room_id = str(room.get("id") or "").strip()
        if not room_id:
            continue
        cursor = 0
        seen_cursors: set[int] = set()
        for page_number in range(max_pages_per_room):
            messages = post("/api/msg/list", {"rid": int(room_id), "msgid": cursor, "pagesize": page_size}).get("list") or []
            if not messages:
                break
            oldest_at: datetime | None = None
            next_cursor_candidates: list[int] = []
            for message in messages:
                raw_created_at = message.get("createtime")
                try:
                    timestamp = float(raw_created_at)
                    if timestamp > 10_000_000_000:
                        timestamp /= 1000
                    occurred = datetime.fromtimestamp(timestamp, tz=window_start.tzinfo)
                except (TypeError, ValueError, OSError):
                    continue
                oldest_at = min(oldest_at, occurred) if oldest_at else occurred
                try:
                    next_cursor_candidates.append(int(message.get("id")))
                except (TypeError, ValueError):
                    pass
                if not window_start <= occurred <= window_end:
                    continue
                raw_message = str(message.get("msg") or "")
                try:
                    parts = json.loads(raw_message)
                except json.JSONDecodeError:
                    parts = [{"type": "raw", "msg": raw_message}]
                message_id = str(message.get("id") or message.get("oid") or "").strip()
                if not message_id:
                    continue
                payload = {
                    "platform": "mx_authorized_request_replay",
                    "collection_window": window,
                    "query": query,
                    "room": {"id": room_id, "title": str(room.get("title") or "")},
                    "message": {
                        "id": message_id,
                        "oid": str(message.get("oid") or ""),
                        "rid": room_id,
                        "createtime": raw_created_at,
                        "parts": parts,
                    },
                }
                snapshots.append(
                    snapshot(
                        channel["id"],
                        f"mx://room/{room_id}/message/{message_id}",
                        json.dumps(payload, ensure_ascii=False, default=str),
                        occurred.isoformat(timespec="seconds"),
                    )
                )
            if oldest_at and oldest_at < window_start:
                break
            if not next_cursor_candidates:
                break
            next_cursor = min(next_cursor_candidates)
            if next_cursor in seen_cursors or next_cursor == cursor:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            if not allow_partial_window:
                raise RuntimeError(f"MX pagination limit reached before the time window boundary for room {room_id}")
    return snapshots


def collect_http(channel: dict[str, Any], window: dict[str, str], query: str = "") -> list[dict[str, str]]:
    if not channel["url"]:
        raise ValueError("HTTP channel URL is empty")
    request_url = render_query_url(channel["url"], query)
    response = requests.get(request_url, timeout=30, headers=browser_headers())
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    response_text = response.text
    if "t.me/s/" in response.url:
        snapshots: list[dict[str, str]] = []
        window_start = datetime.fromisoformat(window["window_start"])
        window_end = datetime.fromisoformat(window["window_end"])
        page_url = response.url.split("?", 1)[0]
        page_text = response_text
        seen_posts: set[str] = set()
        reached_window_start = False
        for page_number in range(30):
            soup = BeautifulSoup(page_text, "html.parser")
            oldest_post_id: int | None = None
            timestamped_posts = 0
            for item in soup.select(".tgme_widget_message[data-post]"):
                post_ref = str(item.get("data-post") or "").strip()
                time_tag = item.select_one("time[datetime]")
                occurred_at = str(time_tag.get("datetime") if time_tag else "").strip()
                if not post_ref or not occurred_at or post_ref in seen_posts:
                    continue
                seen_posts.add(post_ref)
                try:
                    post_number = int(post_ref.rsplit("/", 1)[-1])
                    oldest_post_id = min(oldest_post_id, post_number) if oldest_post_id else post_number
                    occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).astimezone(window_start.tzinfo)
                except (TypeError, ValueError):
                    continue
                timestamped_posts += 1
                if occurred < window_start:
                    reached_window_start = True
                    continue
                if occurred > window_end:
                    continue
                links = sorted(
                    {
                        urljoin(page_url, href)
                        for href in (tag.get("href") for tag in item.select("a[href]"))
                        if href and not href.startswith("javascript:")
                    }
                )
                media = sorted(
                    {
                        urljoin(page_url, source)
                        for source in (tag.get("src") for tag in item.select("[src]"))
                        if source and not source.startswith("data:")
                    }
                )
                payload = {
                    "platform": "telegram_public_preview",
                    "channel": post_ref.split("/", 1)[0],
                    "post_id": post_ref,
                    "collection_window": window,
                    "query": query,
                    "occurred_at": occurred.isoformat(timespec="seconds"),
                    "text": re.sub(r"\s+", " ", item.get_text(" ", strip=True)),
                    "links": links,
                    "media": media,
                    "raw_html": str(item)[:80_000],
                }
                snapshots.append(
                    snapshot(
                        channel["id"],
                        f"https://t.me/{post_ref}",
                        json.dumps(payload, ensure_ascii=False, default=str),
                        occurred.isoformat(timespec="seconds"),
                    )
                )
            if reached_window_start or not oldest_post_id or not timestamped_posts:
                break
            if page_number == 29:
                raise RuntimeError("Telegram pagination limit reached before the requested window start")
            time.sleep(0.8)
            older_response = requests.get(
                f"{page_url}?before={oldest_post_id}",
                timeout=30,
                headers=browser_headers(),
            )
            older_response.raise_for_status()
            page_text = older_response.text
        return snapshots
    payload: dict[str, Any] = {
        "collection_window": window,
        "query": query,
        "content_type": content_type,
        "raw_text": response_text[:120_000],
        "truncated": len(response_text) > 120_000,
    }
    if "json" in content_type:
        payload["structured_json"] = response.json()
    else:
        soup = BeautifulSoup(response_text, "html.parser")
        payload["visible_text"] = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:120_000]
        payload["raw_html"] = response_text[:200_000]
        payload["html_truncated"] = len(response_text) > 200_000
    return [snapshot(channel["id"], request_url, json.dumps(payload, ensure_ascii=False, default=str))]


def collect_playwright(channel: dict[str, Any], window: dict[str, str], profile: Path, query: str = "") -> list[dict[str, str]]:
    urls: list[str] = []
    if channel["id"] == "zsxq" and channel.get("group_ids"):
        urls = [f"https://wx.zsxq.com/group/{group_id}" for group_id in channel["group_ids"]]
    elif channel["url"]:
        urls = [render_query_url(channel["url"], query)]
    if not urls:
        raise ValueError("Playwright channel URL is empty")
    command = [
        sys.executable,
        str(ROOT / "backend" / "browser_session.py"),
        "collect",
        "--profile",
        str(profile),
        "--urls-json",
        json.dumps(urls),
        "--window-start",
        window["window_start"],
        "--window-end",
        window["window_end"],
        "--query",
        query,
        "--max-scrolls",
        str(channel.get("max_scrolls") or 8),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=hidden_window_creationflags(),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(f"Playwright collection failed for {channel['id']}: {detail[-4000:]}") from exc
    return json.loads(result.stdout)


def collect_channel(channel: dict[str, Any], window: dict[str, str], profile: Path, query: str = "") -> list[dict[str, str]]:
    mode = channel["collection_mode"]
    if mode == "akshare":
        return collect_akshare(channel, window, query)
    if mode == "industry_news":
        return collect_public_industry_news(channel["id"], window, query)
    if mode == "wechat_rss":
        return collect_werss(channel, window, query)
    if mode == "ima_knowledge_base":
        return collect_ima_knowledge_base(channel["id"], window, query, channel.get("request_config") or {})
    if mode == "requests":
        if (channel.get("request_config") or {}).get("adapter") == "mx_authorized_request_replay":
            return collect_mx(channel, window, query)
        return collect_http(channel, window, query)
    if mode == "playwright":
        return collect_playwright(channel, window, profile, query)
    raise ValueError(f"Unsupported automatic collection mode: {mode}")
