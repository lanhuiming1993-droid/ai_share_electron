from __future__ import annotations

import asyncio
import re
from pathlib import Path


TRIGGER = "采集近30天数据并生成报告"
COMMAND = "alphadesk-report"
SUPPORTED_PLATFORMS = {"weixin", "lightclawbot"}
REPORT_SCRIPT = Path.home() / ".hermes" / "skills" / "alphadesk-cloud-report" / "scripts" / "collect_report.py"
REPORT_REQUEST_RE = re.compile(r"采集近?(?P<days>\d{1,2})天(?:的)?数据.*生成(?:分析)?报告")


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _match_report_request(value: str) -> int | None:
    compact = _compact_text(value)
    match = REPORT_REQUEST_RE.search(compact)
    if not match:
        return None
    return max(1, min(30, int(match.group("days"))))


def _extract_days(raw_args: str) -> int:
    match = re.search(r"(?:--days\s+)?(\d{1,2})", raw_args or "")
    if not match:
        return 30
    return max(1, min(30, int(match.group(1))))


async def _run_report(raw_args: str) -> str:
    days = _extract_days(raw_args)
    if not REPORT_SCRIPT.exists():
        return f"AlphaDesk report script is missing: {REPORT_SCRIPT}"

    proc = await asyncio.create_subprocess_exec(
        "python3",
        str(REPORT_SCRIPT),
        "--days",
        str(days),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return output or "AlphaDesk report completed, but the script returned no output."
    return f"AlphaDesk report failed with exit code {proc.returncode}.\n\n{output}"


def _pre_gateway_dispatch(event, **_kwargs):
    source = getattr(event, "source", None)
    platform = str(getattr(getattr(source, "platform", None), "value", "") or "").lower()
    if platform not in SUPPORTED_PLATFORMS:
        return None

    days = _match_report_request(getattr(event, "text", ""))
    if days is None:
        return None

    return {
        "action": "rewrite",
        "text": f"/{COMMAND} --days {days} --original {getattr(event, 'text', '')}",
    }


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_command(
        COMMAND,
        _run_report,
        description="Generate an AlphaDesk three-source report.",
        args_hint="[--days 1-30]",
    )
