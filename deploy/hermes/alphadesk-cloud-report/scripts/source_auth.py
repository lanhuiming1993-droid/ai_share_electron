#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from collect_report import DEFAULT_BASE_URL, DEFAULT_ENV_FILE, parse_env

DEFAULT_OUTPUT_DIR = Path.home() / ".hermes" / "alphadesk-auth"
DEFAULT_CACHE_PATH = Path.home() / ".hermes" / "alphadesk-auth" / "werss-search-cache.json"
CHANNELS = ("wechat-mp-rss", "ima-knowledge", "zsxq")


def request(
    method: str,
    base_url: str,
    path: str,
    payload: dict | None = None,
    *,
    expect_json: bool = True,
) -> dict | bytes:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            data = response.read()
            if expect_json:
                return json.loads(data.decode("utf-8"))
            return data
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc


def is_werss_auth_error(error: Exception | str) -> bool:
    text = str(error)
    markers = (
        "wechat_authorized",
        "wechat-login",
        "授权",
        "扫码",
        "二维码",
        "WeRSS 微信搜索授权",
        "微信公众号授权尚未生效",
    )
    return any(marker in text for marker in markers)


def status_label(value: str) -> str:
    return {"online": "可用", "pending": "待授权/待检查", "offline": "不可用"}.get(value or "", value or "未知")


def check_status(base_url: str) -> dict:
    werss = request("GET", base_url, "/api/channels/wechat-mp-rss/component-status")
    ima = request("POST", base_url, "/api/channels/ima-knowledge/check")
    zsxq = request("POST", base_url, "/api/channels/zsxq/check")
    return {"wechat-mp-rss": werss, "ima-knowledge": ima, "zsxq": zsxq}


def print_status(status: dict) -> None:
    werss = status.get("wechat-mp-rss") or {}
    ima = status.get("ima-knowledge") or {}
    zsxq = status.get("zsxq") or {}
    print("AlphaDesk 三信源授权状态：")
    print(
        "- WeRSS："
        f"{status_label(str(werss.get('status') or ''))}；"
        f"微信授权={bool(werss.get('wechat_authorized'))}；"
        f"订阅数={werss.get('subscription_count', 0)}；"
        f"说明={werss.get('wechat_message') or werss.get('message') or ''}"
    )
    print(f"- IMA 知识库：{status_label(str(ima.get('status') or ''))}；说明={ima.get('message') or ''}")
    print(f"- 知识星球 MCP：{status_label(str(zsxq.get('status') or ''))}；说明={zsxq.get('message') or ''}")
    if not werss.get("wechat_authorized"):
        print("WeRSS 微信授权不可用时，请运行：python3 source_auth.py werss-login")
    if ima.get("status") != "online":
        print("IMA 不可用时，请让用户提供 client_id 和 api_key，然后运行 configure-ima。")
    if zsxq.get("status") != "online":
        print("知识星球 MCP 不可用时，请让用户提供 mcp_url，然后运行 configure-zsxq。")


def start_werss_login(base_url: str, output_dir: Path) -> Path:
    request("POST", base_url, "/api/channels/wechat-mp-rss/wechat-login")
    last_error = ""
    content = b""
    for _ in range(20):
        try:
            content = request("GET", base_url, "/api/channels/wechat-mp-rss/qr-image", expect_json=False)  # type: ignore[assignment]
            if content:
                break
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.5)
    if not content:
        raise RuntimeError(f"WeRSS QR image is not ready: {last_error}")
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = mimetypes.guess_extension("image/png") or ".png"
    path = output_dir / f"werss-login-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
    path.write_bytes(content)
    return path


def emit_werss_login_hint(base_url: str, output_dir: Path) -> Path:
    qr_path = start_werss_login(base_url, output_dir)
    print("WeRSS 微信授权不可用，已生成二维码。请在微信中打开图片并扫码授权。")
    print(f"MEDIA:{qr_path}")
    return qr_path


def normalize_text(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def subscription_label(item: dict) -> str:
    return str(item.get("name") or item.get("nickname") or item.get("title") or item.get("id") or "").strip()


def subscription_id(item: dict) -> str:
    return str(item.get("id") or item.get("mp_id") or item.get("biz") or item.get("fakeid") or "").strip()


def print_subscriptions(result: dict) -> None:
    subscriptions = result.get("subscriptions") or []
    print(
        "WeRSS 公众号订阅状态："
        f"授权={bool(result.get('wechat_authorized'))}；"
        f"订阅数={result.get('subscription_count', len(subscriptions))}；"
        f"状态={result.get('status') or 'unknown'}；"
        f"说明={result.get('wechat_message') or result.get('message') or ''}"
    )
    if not subscriptions:
        print("当前没有已订阅公众号。")
        return
    for index, item in enumerate(subscriptions, 1):
        enabled = "启用" if item.get("enabled", True) else "停用"
        print(f"{index}. {subscription_label(item)} | id={subscription_id(item)} | {enabled}")


def get_werss_subscriptions(base_url: str) -> dict:
    return request("GET", base_url, "/api/channels/wechat-mp-rss/subscriptions")  # type: ignore[return-value]


def search_werss_subscriptions(base_url: str, query: str, cache_path: Path) -> dict:
    keyword = str(query or "").strip()
    if not keyword:
        raise SystemExit("werss-search requires --query")
    result = request(
        "GET",
        base_url,
        f"/api/channels/wechat-mp-rss/subscriptions/search?q={urllib.parse.quote(keyword)}",
    )
    items = result.get("items") or []  # type: ignore[union-attr]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"query": keyword, "items": items, "saved_at": time.time()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"WeRSS 搜索“{keyword}”找到 {len(items)} 个候选：")
    for index, item in enumerate(items, 1):
        print(f"{index}. {subscription_label(item)} | id={subscription_id(item)}")
    if not items:
        print("没有找到匹配的公众号，请换一个关键词。")
    return result  # type: ignore[return-value]


def load_cached_candidates(cache_path: Path) -> list[dict]:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def score_candidate(candidate: dict, value: str) -> float:
    target = normalize_text(value)
    if not target:
        return 0.0
    cid = normalize_text(subscription_id(candidate))
    label = normalize_text(subscription_label(candidate))
    if target == cid or target == label:
        return 1.0
    if target.isdigit():
        return 0.0
    if target and (target in label or target in cid):
        return 0.9
    return SequenceMatcher(None, target, label).ratio()


def select_candidate(candidates: list[dict], value: str) -> tuple[dict | None, list[dict]]:
    target = str(value or "").strip()
    if target.isdigit():
        index = int(target) - 1
        if 0 <= index < len(candidates):
            return candidates[index], []
    ranked = sorted(
        ((score_candidate(item, target), item) for item in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    matches = [item for score, item in ranked if score >= 0.72]
    if len(matches) == 1:
        return matches[0], []
    return None, matches[:5]


def add_werss_subscription_from_value(base_url: str, value: str, cache_path: Path) -> dict:
    target = str(value or "").strip()
    if not target:
        raise SystemExit("werss-add requires --query")
    cached = load_cached_candidates(cache_path)
    selected, ambiguous = select_candidate(cached, target)
    if not selected:
        search_result = request(
            "GET",
            base_url,
            f"/api/channels/wechat-mp-rss/subscriptions/search?q={urllib.parse.quote(target)}",
        )
        items = search_result.get("items") or []  # type: ignore[union-attr]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"query": target, "items": items, "saved_at": time.time()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        selected, ambiguous = select_candidate(items, target)
        if not selected and len(items) == 1:
            selected = items[0]
        if not selected:
            print(f"没有唯一匹配到要加入的公众号“{target}”。候选如下，请回复“新增公众号订阅 编号”：")
            for index, item in enumerate(ambiguous or items[:8], 1):
                print(f"{index}. {subscription_label(item)} | id={subscription_id(item)}")
            return {"status": "needs_selection", "items": ambiguous or items[:8]}
    result = request("POST", base_url, "/api/channels/wechat-mp-rss/subscriptions", selected)
    subscription = result.get("subscription") or selected  # type: ignore[union-attr]
    print(f"已加入公众号订阅：{subscription_label(subscription)} | id={subscription_id(subscription)}")
    print_subscriptions(result)  # type: ignore[arg-type]
    return result  # type: ignore[return-value]


def find_existing_subscription(base_url: str, value: str) -> tuple[dict | None, list[dict], dict]:
    result = get_werss_subscriptions(base_url)
    subscriptions = result.get("subscriptions") or []
    selected, ambiguous = select_candidate(subscriptions, value)
    if selected:
        return selected, [], result
    target = normalize_text(value)
    matches = [
        item
        for item in subscriptions
        if target and (target in normalize_text(subscription_label(item)) or target in normalize_text(subscription_id(item)))
    ]
    if len(matches) == 1:
        return matches[0], [], result
    return None, (matches or subscriptions[:8]), result


def remove_werss_subscription(base_url: str, value: str) -> dict:
    target = str(value or "").strip()
    if not target:
        raise SystemExit("werss-remove requires --query")
    selected, ambiguous, _status = find_existing_subscription(base_url, target)
    if not selected:
        print(f"没有唯一匹配到要移除的公众号“{target}”。候选如下，请使用更完整名称或 id：")
        for index, item in enumerate(ambiguous, 1):
            print(f"{index}. {subscription_label(item)} | id={subscription_id(item)}")
        return {"status": "needs_selection", "items": ambiguous}
    sid = subscription_id(selected)
    result = request("DELETE", base_url, f"/api/channels/wechat-mp-rss/subscriptions/{urllib.parse.quote(sid, safe='')}")
    print(f"已移除公众号订阅：{subscription_label(selected)} | id={sid}")
    print_subscriptions(result)  # type: ignore[arg-type]
    return result  # type: ignore[return-value]


def backfill_werss_subscriptions(base_url: str, value: str, start_page: int, end_page: int) -> dict:
    target = str(value or "全部").strip()
    payload: dict = {"start_page": start_page, "end_page": end_page, "subscription_ids": []}
    if target not in {"全部", "all", "*"}:
        selected, ambiguous, _status = find_existing_subscription(base_url, target)
        if not selected:
            print(f"没有唯一匹配到要补采的公众号“{target}”。候选如下，请使用更完整名称或 id：")
            for index, item in enumerate(ambiguous, 1):
                print(f"{index}. {subscription_label(item)} | id={subscription_id(item)}")
            return {"status": "needs_selection", "items": ambiguous}
        payload["subscription_ids"] = [subscription_id(selected)]
    result = request("POST", base_url, "/api/channels/wechat-mp-rss/subscriptions/backfill", payload)
    print(
        "WeRSS 补采已提交："
        f"目标={target}；"
        f"提交={result.get('submitted_count', 0)}；"
        f"失败={result.get('failed_count', 0)}；"
        f"页码={start_page}-{end_page}"
    )
    for item in result.get("results") or []:  # type: ignore[union-attr]
        print(f"- {item.get('id')}: {item.get('status')}；{item.get('message') or ''}")
    return result  # type: ignore[return-value]


def configure_ima(base_url: str, client_id: str, api_key: str, skill_download_url: str) -> dict:
    if not client_id.strip() or not api_key.strip():
        raise SystemExit("configure-ima requires --client-id and --api-key")
    payload = {"client_id": client_id.strip(), "api_key": api_key.strip()}
    if skill_download_url.strip():
        payload["skill_download_url"] = skill_download_url.strip()
    return request("PUT", base_url, "/api/channels/ima-knowledge/config", payload)  # type: ignore[return-value]


def configure_zsxq(base_url: str, mcp_url: str, include_comments: bool) -> dict:
    if not mcp_url.strip():
        raise SystemExit("configure-zsxq requires --mcp-url")
    payload = {"mcp_url": mcp_url.strip(), "include_comments": include_comments}
    return request("PUT", base_url, "/api/channels/zsxq/config", payload)  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(description="AlphaDesk source authorization helper for Hermes.")
    parser.add_argument(
        "action",
        choices=(
            "status",
            "werss-login",
            "werss-status",
            "werss-search",
            "werss-add",
            "werss-remove",
            "werss-backfill",
            "configure-ima",
            "configure-zsxq",
        ),
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE_PATH)
    parser.add_argument("--query", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--start-page", type=int, default=0)
    parser.add_argument("--end-page", type=int, default=1)
    parser.add_argument("--client-id", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--skill-download-url", default="")
    parser.add_argument("--mcp-url", default="")
    parser.add_argument("--include-comments", action="store_true")
    args = parser.parse_args()

    if args.env_file.exists():
        parse_env(args.env_file)

    try:
        if args.action == "status":
            print_status(check_status(args.base_url))
            return 0
        if args.action == "werss-login":
            emit_werss_login_hint(args.base_url, args.output_dir)
            return 0
        if args.action == "werss-status":
            print_subscriptions(get_werss_subscriptions(args.base_url))
            return 0
        if args.action == "werss-search":
            search_werss_subscriptions(args.base_url, args.query or args.target, args.cache_path)
            return 0
        if args.action == "werss-add":
            add_werss_subscription_from_value(args.base_url, args.query or args.target, args.cache_path)
            return 0
        if args.action == "werss-remove":
            remove_werss_subscription(args.base_url, args.query or args.target)
            return 0
        if args.action == "werss-backfill":
            backfill_werss_subscriptions(args.base_url, args.query or args.target or "全部", args.start_page, args.end_page)
            return 0
        if args.action == "configure-ima":
            configure_ima(args.base_url, args.client_id, args.api_key, args.skill_download_url)
            print("IMA 授权参数已保存。未回显 API Key。请运行 status 复查。")
            return 0
        if args.action == "configure-zsxq":
            configure_zsxq(args.base_url, args.mcp_url, args.include_comments)
            print("知识星球 MCP 授权参数已保存。未回显 MCP URL。请运行 status 复查。")
            return 0
    except Exception as exc:
        if args.action.startswith("werss-") and args.action != "werss-login" and is_werss_auth_error(exc):
            emit_werss_login_hint(args.base_url, args.output_dir)
            return 2
        raise
    raise SystemExit(f"Unsupported action: {args.action}")


if __name__ == "__main__":
    raise SystemExit(main())
