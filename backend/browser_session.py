from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

LOGIN_STATE_FILE = ".alphadesk-login-state.json"
LOGIN_STATE_MAX_AGE_SECONDS = 5
CHROMIUM_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


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


def write_login_state(profile: Path, url: str, active: bool) -> None:
    state_path = profile / LOGIN_STATE_FILE
    temporary_path = profile / f"{LOGIN_STATE_FILE}.tmp"
    temporary_path.write_text(
        json.dumps({"url": url, "active": active, "updated_at": time.time()}),
        encoding="utf-8",
    )
    temporary_path.replace(state_path)


def read_login_state(profile: Path) -> dict:
    try:
        payload = json.loads((profile / LOGIN_STATE_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cleanup_stale_chromium_singleton(profile: Path) -> bool:
    try:
        lock_target = os.readlink(profile / "SingletonLock")
    except OSError:
        return False
    hostname, separator, pid_text = lock_target.rpartition("-")
    if not separator or not pid_text.isdigit():
        return False
    if hostname == socket.gethostname():
        try:
            os.kill(int(pid_text), 0)
        except OSError:
            pass
        else:
            return False
    for name in CHROMIUM_SINGLETON_FILES:
        try:
            (profile / name).unlink()
        except FileNotFoundError:
            pass
    return True


def is_profile_in_use_error(exc: PlaywrightError) -> bool:
    detail = str(exc)
    return "Opening in existing browser session" in detail or "profile appears to be in use" in detail


def evaluate_login_url(final_url: str, success_url_contains: str, channel_id: str = "") -> tuple[bool, str]:
    if success_url_contains:
        available = success_url_contains in final_url
        return available, f"Current URL {'matches' if available else 'does not match'} the success rule"
    available = not any(word in final_url.lower() for word in ("login", "signin", "passport"))
    return available, "Basic redirect check completed"


def login(profile: Path, url: str) -> None:
    cleanup_stale_chromium_singleton(profile)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            viewport=None,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        last_url = url
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            while not page.is_closed():
                last_url = page.url
                write_login_state(profile, last_url, active=True)
                page.wait_for_timeout(1000)
        except PlaywrightError:
            if not page.is_closed():
                raise
        finally:
            write_login_state(profile, last_url, active=False)
            context.close()


def check(profile: Path, url: str, success_url_contains: str, success_selector: str, channel_id: str = "") -> None:
    cleanup_stale_chromium_singleton(profile)
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
        except PlaywrightError as exc:
            if not is_profile_in_use_error(exc):
                raise
            state = read_login_state(profile)
            final_url = str(state.get("url") or url)
            updated_at = float(state.get("updated_at") or 0)
            active = bool(state.get("active")) and time.time() - updated_at <= LOGIN_STATE_MAX_AGE_SECONDS
            available, message = evaluate_login_url(final_url, success_url_contains, channel_id)
            if not active:
                available = False
                message = "Login browser is still starting or the profile is inactive; close it and retry if login is complete"
            elif success_selector and not available:
                message = "Login browser owns the profile, so the selector cannot be checked until it is closed"
            print(json.dumps({"available": available, "message": message, "final_url": final_url}))
            return
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1200)
        final_url = page.url
        if success_selector:
            available = page.locator(success_selector).count() > 0
            message = f"Selector {'matched' if available else 'did not match'}: {success_selector}"
        else:
            available, message = evaluate_login_url(final_url, success_url_contains, channel_id)
        context.close()
    print(json.dumps({"available": available, "message": message, "final_url": final_url}))


def collect(profile: Path, urls: list[str], window_start: str, window_end: str, query: str, max_scrolls: int) -> None:
    snapshots = []
    cleanup_stale_chromium_singleton(profile)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
        page = context.pages[0] if context.pages else context.new_page()
        for url in urls:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(1800)
            final_url = page.url.lower()
            if any(word in final_url for word in ("login", "signin", "passport")):
                raise RuntimeError(f"Browser profile is no longer authenticated: {page.url}")
            window_start_dt = datetime.fromisoformat(window_start)
            text = ""
            timestamps: list[datetime] = []
            for _ in range(max_scrolls):
                text = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
                timestamps = visible_timestamps(text, window_start_dt.tzinfo)
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(650)
            if not text:
                text = re.sub(r"\s+", " ", page.locator("body").inner_text()).strip()
            if not text:
                continue
            captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
            raw_html = page.content()
            body = json.dumps(
                {
                    "platform": "web",
                    "collection_window": {"start": window_start, "end": window_end},
                    "captured_at": captured_at,
                    "query": query,
                    "visible_timestamps": [value.isoformat(timespec="minutes") for value in timestamps],
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
    parser.add_argument("--channel-id", default="")
    args = parser.parse_args()
    profile = Path(args.profile)
    profile.mkdir(parents=True, exist_ok=True)
    if args.action == "login":
        login(profile, args.url)
    elif args.action == "check":
        check(profile, args.url, args.success_url_contains, args.success_selector, args.channel_id)
    else:
        collect(
            profile,
            json.loads(args.urls_json),
            args.window_start,
            args.window_end,
            args.query,
            args.max_scrolls,
        )


if __name__ == "__main__":
    main()
