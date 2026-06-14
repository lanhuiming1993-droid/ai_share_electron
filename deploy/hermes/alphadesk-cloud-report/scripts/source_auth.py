#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path

from collect_report import DEFAULT_BASE_URL, DEFAULT_ENV_FILE, parse_env

DEFAULT_OUTPUT_DIR = Path.home() / ".hermes" / "alphadesk-auth"
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
    parser.add_argument("action", choices=("status", "werss-login", "configure-ima", "configure-zsxq"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--client-id", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--skill-download-url", default="")
    parser.add_argument("--mcp-url", default="")
    parser.add_argument("--include-comments", action="store_true")
    args = parser.parse_args()

    if args.env_file.exists():
        parse_env(args.env_file)

    if args.action == "status":
        print_status(check_status(args.base_url))
        return 0
    if args.action == "werss-login":
        qr_path = start_werss_login(args.base_url, args.output_dir)
        print("WeRSS 微信授权二维码已生成，请在微信中打开图片并扫码授权。")
        print(f"MEDIA:{qr_path}")
        return 0
    if args.action == "configure-ima":
        configure_ima(args.base_url, args.client_id, args.api_key, args.skill_download_url)
        print("IMA 授权参数已保存。未回显 API Key。请运行 status 复查。")
        return 0
    if args.action == "configure-zsxq":
        configure_zsxq(args.base_url, args.mcp_url, args.include_comments)
        print("知识星球 MCP 授权参数已保存。未回显 MCP URL。请运行 status 复查。")
        return 0
    raise SystemExit(f"Unsupported action: {args.action}")


if __name__ == "__main__":
    raise SystemExit(main())
