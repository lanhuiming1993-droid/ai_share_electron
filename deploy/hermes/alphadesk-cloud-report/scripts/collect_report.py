#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_ENV_FILE = Path("/opt/alphadesk/deploy/cloud.env")
DEFAULT_MAX_EVIDENCE_CHARS = 18_000
TERMINAL_FAILURES = {"failed", "cancelled", "report_failed"}
COLLECTION_READY = {"completed", "deduplicated", "partial_completed", "review", "partial_review"}


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


def format_runs_for_chat(latest_status: dict) -> list[str]:
    lines: list[str] = []
    for run in latest_status.get("runs") or []:
        channel_id = run.get("channel_id") or "unknown"
        status = run.get("status") or "unknown"
        duplicate_count = int(run.get("duplicate_count") or 0)
        snapshot_count = int(run.get("snapshot_count") or 0)
        used_count = duplicate_count + snapshot_count
        error = str(run.get("error") or "").strip()
        suffix = f"; used={used_count}" if used_count else ""
        if error:
            suffix += f"; note={error[:160]}"
        lines.append(f"- {channel_id}: {status}{suffix}")
    return lines


def _clean_text(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", str(value or ""))).strip()


def format_evidence_for_hermes(latest_status: dict, evidence: dict, max_evidence_chars: int) -> str:
    lines = [
        "AlphaDesk 三信源证据包已就绪。",
        "请你作为行业分析师，基于下列证据生成面向用户的中文分析报告；不要声称后端已经生成报告，也不要补写证据中不存在的事实。",
        f"Job: {latest_status.get('id') or latest_status.get('job_id')}",
        f"Status: {latest_status.get('status')}",
        f"Lookback days: {latest_status.get('lookback_days')}",
        "Source runs:",
    ]
    run_lines = format_runs_for_chat(latest_status)
    lines.extend(run_lines or ["- no source run details returned"])

    counts = evidence.get("attached_snapshot_counts") or []
    if counts:
        lines.append("")
        lines.append("Snapshot coverage:")
        for item in counts:
            lines.append(f"- {item.get('channel_id')}: {item.get('count', 0)} snapshots attached")

    lines.append("")
    lines.append("Selected evidence:")
    used_chars = 0
    for index, item in enumerate(evidence.get("selected_items") or [], start=1):
        title = _clean_text(item.get("title") or "")
        author = _clean_text(item.get("author") or item.get("source_label") or item.get("channel_name") or item.get("channel_id") or "")
        content = _clean_text(item.get("content_preview") or "")
        source_url = _clean_text(item.get("source_url") or "")
        header = f"[{index}] {item.get('channel_id')} | {item.get('occurred_at')} | {author}"
        if title:
            header += f" | {title}"
        block = f"{header}\n{content}"
        if source_url:
            block += f"\nSource: {source_url}"
        if max_evidence_chars > 0 and used_chars + len(block) > max_evidence_chars:
            remaining = max_evidence_chars - used_chars
            if remaining > 200:
                lines.append(block[:remaining].rstrip())
            lines.append(f"\n[Evidence truncated for Hermes context: showing {max_evidence_chars} chars.]")
            break
        lines.append(block)
        lines.append("")
        used_chars += len(block)

    lines.append("")
    lines.append("Report requirements:")
    lines.append("- 输出中文，先给核心结论，再按主题/产业链拆分。")
    lines.append("- 明确标注信息来自 WeRSS、IMA 知识库或知识星球；微信公众号内容尽量写出具体公众号/作者。")
    lines.append("- 对 IMA live 403 这类缓存兜底要如实说明为 cached evidence，不要包装成实时采集成功。")
    lines.append("- 给出待核验事项和风险提示。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger AlphaDesk cloud source report generation.")
    parser.add_argument("--days", type=int, default=30, help="lookback days, 1-30")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--interval", type=int, default=20, help="poll interval seconds")
    parser.add_argument("--timeout", type=int, default=1800, help="overall wait timeout seconds")
    parser.add_argument("--max-evidence-chars", type=int, default=DEFAULT_MAX_EVIDENCE_CHARS, help="max selected evidence chars returned to Hermes")
    parser.add_argument("--limit-per-channel", type=int, default=12, help="selected evidence items per source channel")
    parser.add_argument("--preview-chars", type=int, default=1200, help="max chars per evidence item")
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

    job = request_json("POST", args.base_url, "/api/agent/collect-report", token, {"lookback_days": args.days, "force_refresh": True})
    job_id = job["job_id"]
    print(json.dumps({"event": "queued", "job_id": job_id, "channel_ids": job.get("channel_ids")}, ensure_ascii=False))

    deadline = time.time() + args.timeout
    latest_status: dict = {}
    while time.time() < deadline:
        latest_status = request_json("GET", args.base_url, job["poll_url"], token)
        status = latest_status.get("status")
        print(json.dumps({"event": "poll", "job_id": job_id, "status": status, "collection_ready": status in COLLECTION_READY}, ensure_ascii=False))
        if status in COLLECTION_READY or status in TERMINAL_FAILURES:
            break
        time.sleep(args.interval)

    status = latest_status.get("status")
    if status in COLLECTION_READY:
        evidence_path = latest_status.get("evidence_url") or job.get("evidence_url") or f"/api/agent/jobs/{job_id}/evidence"
        evidence_path = f"{evidence_path}?limit_per_channel={args.limit_per_channel}&preview_chars={args.preview_chars}"
        evidence = request_json("GET", args.base_url, evidence_path, token)
        print(
            json.dumps(
                {
                    "event": "evidence",
                    "job_id": job_id,
                    "status": status,
                    "selected_items": len(evidence.get("selected_items") or []),
                    "attached_snapshot_counts": evidence.get("attached_snapshot_counts") or [],
                },
                ensure_ascii=False,
            )
        )
        print(format_evidence_for_hermes(latest_status, evidence, args.max_evidence_chars))
        return 0
    if status in TERMINAL_FAILURES:
        print(json.dumps({"event": "failed", "job_id": job_id, "status": status, "error": latest_status.get("error")}, ensure_ascii=False))
        return 2
    print(json.dumps({"event": "timeout", "job_id": job_id, "last_status": status}, ensure_ascii=False))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
