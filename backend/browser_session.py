from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright


def visible_timestamps(text: str, timezone_info) -> list[datetime]:
    values = []
    for date_text, time_text in re.findall(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})(?:\s+(\d{1,2}:\d{2}))?", text):
        normalized = re.sub(r"[/.]", "-", date_text)
        try:
            parsed = datetime.strptime(f"{normalized} {time_text or '00:00'}", "%Y-%m-%d %H:%M")
            values.append(parsed.replace(tzinfo=timezone_info))
        except ValueError:
            continue
    return values


def login(profile: Path, url: str) -> None:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            viewport=None,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_event("close", timeout=0)
        context.close()


def check(profile: Path, url: str, success_url_contains: str, success_selector: str) -> None:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1200)
        final_url = page.url
        if success_selector:
            available = page.locator(success_selector).count() > 0
            message = f"页面选择器 {'已匹配' if available else '未匹配'}: {success_selector}"
        elif success_url_contains:
            available = success_url_contains in final_url
            message = f"当前页面 {'符合' if available else '不符合'}登录后 URL 规则"
        else:
            available = not any(word in final_url.lower() for word in ("login", "signin", "passport"))
            message = "已按页面重定向结果完成基础检查"
        context.close()
    print(json.dumps({"available": available, "message": message, "final_url": final_url}))


def collect(profile: Path, urls: list[str], window_start: str, window_end: str, query: str, max_scrolls: int) -> None:
    snapshots = []
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        for url in urls:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1800)
            is_zsxq_group = "wx.zsxq.com/group/" in url
            final_url = page.url.lower()
            if any(word in final_url for word in ("login", "signin", "passport")):
                raise RuntimeError(f"Browser profile is no longer authenticated: {page.url}")
            if is_zsxq_group and "/group/" not in final_url:
                raise RuntimeError(f"ZSXQ login state is unavailable: {page.url}")
            window_start_dt = datetime.fromisoformat(window_start)
            text = ""
            timestamps: list[datetime] = []
            for _ in range(max_scrolls):
                text = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
                timestamps = visible_timestamps(text, window_start_dt.tzinfo)
                if is_zsxq_group and timestamps and min(timestamps) <= window_start_dt:
                    break
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(650)
            if not text:
                text = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
            if not text:
                continue
            captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
            raw_html = page.content()
            if is_zsxq_group:
                group_match = re.search(r"/group/(\d+)", url)
                body = json.dumps(
                    {
                        "platform": "zsxq",
                        "group_id": group_match.group(1) if group_match else "",
                        "collection_window": {"start": window_start, "end": window_end},
                        "captured_at": captured_at,
                        "visible_timestamps": [value.isoformat(timespec="minutes") for value in timestamps],
                        "query": query,
                        "visible_text": text[:120_000],
                        "visible_text_truncated": len(text) > 120_000,
                        "raw_html": raw_html[:200_000],
                        "raw_html_truncated": len(raw_html) > 200_000,
                    },
                    ensure_ascii=False,
                )
            else:
                body = json.dumps(
                    {
                        "platform": "web",
                        "collection_window": {"start": window_start, "end": window_end},
                        "captured_at": captured_at,
                        "query": query,
                        "visible_text": text[:120_000],
                        "visible_text_truncated": len(text) > 120_000,
                        "raw_html": raw_html[:200_000],
                        "raw_html_truncated": len(raw_html) > 200_000,
                    },
                    ensure_ascii=False,
                )
            snapshots.append(
                {
                    "channel_id": profile.name,
                    "occurred_at": captured_at,
                    "source_url": page.url,
                    "content": body,
                }
            )
        context.close()
    print(json.dumps(snapshots))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("login", "check", "collect"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--url", default="")
    parser.add_argument("--urls-json", default="[]")
    parser.add_argument("--window-start", default="")
    parser.add_argument("--window-end", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--max-scrolls", type=int, default=8)
    parser.add_argument("--success-url-contains", default="")
    parser.add_argument("--success-selector", default="")
    args = parser.parse_args()
    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    if args.action == "login":
        login(profile, args.url)
    elif args.action == "check":
        check(profile, args.url, args.success_url_contains, args.success_selector)
    else:
        collect(profile, json.loads(args.urls_json), args.window_start, args.window_end, args.query, args.max_scrolls)


if __name__ == "__main__":
    main()
