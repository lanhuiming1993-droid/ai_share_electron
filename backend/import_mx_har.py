from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from backend.collectors import collect_mx
from backend.main import db, now, save_channel_request_config

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HAR = ROOT / "0fa900ba4663de533abd62dcd119b9e9.har"
CHANNEL_ID = "web-rumors"


def header_value(headers: list[dict], name: str) -> str:
    for item in headers:
        if str(item.get("name") or "").lower() == name.lower():
            return str(item.get("value") or "").strip()
    return ""


def response_json(entry: dict) -> dict:
    try:
        return json.loads(entry.get("response", {}).get("content", {}).get("text") or "{}")
    except json.JSONDecodeError:
        return {}


def read_authorized_config_data(har: dict) -> dict:
    entries = har.get("log", {}).get("entries", [])
    candidates: list[dict] = []
    for entry in entries:
        request = entry.get("request", {})
        parsed = urlparse(str(request.get("url") or ""))
        token = header_value(request.get("headers") or [], "token")
        room_list_suffix = "/api/room/list"
        if (
            request.get("method") != "POST"
            or parsed.scheme != "https"
            or not parsed.path.endswith(room_list_suffix)
            or not token
            or response_json(entry).get("code") != 200
        ):
            continue
        api_prefix = parsed.path[: -len(room_list_suffix)]
        candidates.append(
            {
                "adapter": "mx_authorized_request_replay",
                "base_url": f"{parsed.scheme}://{parsed.netloc}{api_prefix}",
                "token": token,
                "ad": header_value(request.get("headers") or [], "AD") or "1",
                "version": header_value(request.get("headers") or [], "version") or "4.3.3",
                "i": header_value(request.get("headers") or [], "i") or "qq",
                "referer": header_value(request.get("headers") or [], "Referer") or "https://mx.2026.naaifu.cn/",
                "room_ids": [20099, 29446],
                "page_size": 30,
                "max_pages_per_room": 500,
                "request_delay_seconds": 0.25,
            }
        )
    if not candidates:
        raise RuntimeError("No successful authorized MX room-list request was found in the HAR")
    return candidates[-1]


def read_authorized_config_text(har_text: str) -> dict:
    try:
        har = json.loads(har_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("The selected file is not a valid HAR JSON document") from exc
    return read_authorized_config_data(har)


def read_authorized_config(har_path: Path) -> dict:
    return read_authorized_config_text(har_path.read_text(encoding="utf-8-sig"))


def save_channel(status: str, detail: str) -> None:
    updated_at = now()
    with db() as conn:
        cursor = conn.execute(
            """
            UPDATE channels
            SET name=?,type=?,url=?,collection_mode=?,status=?,notes=?,parsing_strategy=?,
                normalization_quality_threshold=?,max_scrolls=?,updated_at=?,last_check=?
            WHERE id=?
            """,
            (
                "MX 小作文频道",
                "MX 授权请求回放",
                "https://mx.2026.naaifu.cn/",
                "requests",
                status,
                detail,
                "fixed",
                60,
                1,
                updated_at,
                updated_at,
                CHANNEL_ID,
            ),
        )
        if not cursor.rowcount:
            conn.execute(
                """
                INSERT INTO channels(
                  id,name,type,url,collection_mode,status,notes,parsing_strategy,
                  normalization_quality_threshold,max_scrolls,builtin,last_check,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    CHANNEL_ID,
                    "MX 小作文频道",
                    "MX 授权请求回放",
                    "https://mx.2026.naaifu.cn/",
                    "requests",
                    status,
                    detail,
                    "fixed",
                    60,
                    1,
                    0,
                    updated_at,
                    updated_at,
                ),
            )


def validate(config: dict) -> int:
    current = datetime.now().astimezone().replace(microsecond=0)
    snapshots = collect_mx(
        {
            "id": CHANNEL_ID,
            "collection_mode": "requests",
            "request_config": {
                **config,
                "room_ids": [20099],
                "page_size": 3,
                "max_pages_per_room": 1,
                "request_delay_seconds": 0,
                "allow_partial_window": True,
            },
        },
        {
            "window_start": (current - timedelta(days=30)).isoformat(timespec="seconds"),
            "window_end": current.isoformat(timespec="seconds"),
        },
    )
    return len(snapshots)


def import_authorized_config(config: dict) -> dict:
    snapshot_count = validate(config)
    save_channel_request_config(CHANNEL_ID, config)
    save_channel("online", "MX HAR 会话已导入并通过验活；会话过期后请重新登录并导入新的 HAR。")
    return {
        "channel_id": CHANNEL_ID,
        "imported": True,
        "status": "online",
        "validated_snapshot_count": snapshot_count,
    }


def import_har_text(har_text: str) -> dict:
    return import_authorized_config(read_authorized_config_text(har_text))


def main() -> None:
    parser = argparse.ArgumentParser(description="Import an authorized MX HAR session into AlphaDesk")
    parser.add_argument("har_path", nargs="?", type=Path, default=DEFAULT_HAR)
    args = parser.parse_args()

    try:
        result = import_authorized_config(read_authorized_config(args.har_path.resolve()))
    except Exception as exc:
        print(json.dumps({"channel_id": CHANNEL_ID, "imported": False, "status": "offline", "error": str(exc)[:300]}))
        raise SystemExit(1) from exc
    print(json.dumps(result))


if __name__ == "__main__":
    main()
