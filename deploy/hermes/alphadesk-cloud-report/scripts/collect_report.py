#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_ENV_FILE = Path("/opt/alphadesk/deploy/cloud.env")
TERMINAL_FAILURES = {"failed", "report_failed"}
TERMINAL_PARTIAL = {"review", "partial_review"}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            if value[0] == '"':
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value[1:-1]
            else:
                value = value[1:-1]
        values[key.strip()] = value
    return values


def request_json(method: str, base_url: str, path: str, token: str, payload: dict | None = None) -> dict:
    body = None
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger AlphaDesk cloud source report generation.")
    parser.add_argument("--days", type=int, default=30, help="lookback days, 1-30")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--interval", type=int, default=20, help="poll interval seconds")
    parser.add_argument("--timeout", type=int, default=1800, help="overall wait timeout seconds")
    parser.add_argument("--check", action="store_true", help="only check backend readiness")
    args = parser.parse_args()

    if not 1 <= args.days <= 30:
        raise SystemExit("--days must be between 1 and 30")
    env = parse_env(args.env_file)
    token = env.get("ALPHADESK_AGENT_TOKEN", "").strip()
    if not token:
        raise SystemExit(f"ALPHADESK_AGENT_TOKEN is missing in {args.env_file}")

    if args.check:
        ready = request_json("GET", args.base_url, "/health/ready", token)
        print(json.dumps({"ready": ready.get("status"), "service": ready.get("service"), "version": ready.get("version")}, ensure_ascii=False))
        return 0

    job = request_json("POST", args.base_url, "/api/agent/collect-report", token, {"lookback_days": args.days})
    job_id = job["job_id"]
    print(json.dumps({"event": "queued", "job_id": job_id, "channel_ids": job.get("channel_ids")}, ensure_ascii=False))

    deadline = time.time() + args.timeout
    latest_status: dict = {}
    while time.time() < deadline:
        latest_status = request_json("GET", args.base_url, job["poll_url"], token)
        status = latest_status.get("status")
        print(json.dumps({"event": "poll", "job_id": job_id, "status": status, "report_ready": latest_status.get("report_ready")}, ensure_ascii=False))
        if latest_status.get("report_ready") or status in TERMINAL_FAILURES or status in TERMINAL_PARTIAL:
            break
        time.sleep(args.interval)

    status = latest_status.get("status")
    if latest_status.get("report_ready"):
        report = request_json("GET", args.base_url, latest_status.get("report_url") or job["report_url"], token)
        print(json.dumps({"event": "report", "job_id": job_id, "status": report.get("status"), "snapshot_count": report.get("snapshot_count")}, ensure_ascii=False))
        print(report.get("report", ""))
        return 0
    if status in TERMINAL_FAILURES:
        print(json.dumps({"event": "failed", "job_id": job_id, "status": status, "error": latest_status.get("error")}, ensure_ascii=False))
        return 2
    print(json.dumps({"event": "timeout", "job_id": job_id, "last_status": status}, ensure_ascii=False))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
