from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "logs"
LOG_PATH = LOG_DIR / "alphadesk.jsonl"
MAX_LOG_BYTES = 8 * 1024 * 1024
LOG_BACKUP_COUNT = 12

request_id_var: ContextVar[str] = ContextVar("alphadesk_request_id", default="")
_configured = False
_configure_lock = threading.Lock()

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "encrypted_api_key",
    "encrypted_config",
    "har_text",
    "password",
    "secret",
    "token",
    "tushare_token",
}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b", re.IGNORECASE),
    re.compile(r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)\b(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
)


def _redact_text(value: str) -> str:
    text = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(r"\1\2[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text[:12_000]


def redact(value: Any, *, key: str = "") -> Any:
    normalized_key = key.casefold().replace("-", "_")
    if (
        normalized_key in SENSITIVE_KEYS
        or any(part in normalized_key for part in ("password", "secret", "cookie", "api_key"))
        or normalized_key.endswith("_token")
        or normalized_key.startswith("token_")
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_text(str(value))


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "component": record.name.removeprefix("alphadesk."),
            "event": getattr(record, "event", record.getMessage()),
            "request_id": getattr(record, "request_id", "") or request_id_var.get(),
            "thread": record.threadName,
            "fields": redact(getattr(record, "fields", {})),
        }
        if record.exc_info:
            payload["exception"] = _redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    with _configure_lock:
        if _configured:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(JsonLogFormatter())
        logger = logging.getLogger("alphadesk")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True


def get_logger(component: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"alphadesk.{component}")


def set_request_id(request_id: str):
    return request_id_var.set(request_id)


def reset_request_id(token) -> None:
    request_id_var.reset(token)


def log_event(logger: logging.Logger, level: int | str, event: str, **fields: Any) -> None:
    level_value = logging.getLevelName(level.upper()) if isinstance(level, str) else level
    logger.log(level_value, event, extra={"event": event, "fields": fields})


def log_exception(logger: logging.Logger, event: str, exc: Exception, **fields: Any) -> None:
    logger.error(
        event,
        extra={"event": event, "fields": {**fields, "error_type": type(exc).__name__, "error": str(exc)}},
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def log_files() -> list[Path]:
    configure_logging()
    return sorted(
        [path for path in LOG_DIR.glob("*.jsonl*") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
    )


def diagnostics_config() -> dict[str, Any]:
    files = log_files()
    return {
        "directory": str(LOG_DIR),
        "active_file": str(LOG_PATH),
        "max_file_mb": MAX_LOG_BYTES // (1024 * 1024),
        "backup_count": LOG_BACKUP_COUNT,
        "files": [{"name": path.name, "size_bytes": path.stat().st_size} for path in reversed(files)],
    }


def recent_logs(*, limit: int = 200, level: str = "", component: str = "", search: str = "") -> list[dict]:
    limit = max(1, min(1_000, limit))
    selected: deque[dict] = deque(maxlen=limit)
    level = level.casefold().strip()
    component = component.casefold().strip()
    search = search.casefold().strip()
    for path in log_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if level and item.get("level", "").casefold() != level:
                continue
            if component and component not in item.get("component", "").casefold():
                continue
            if search and search not in line.casefold():
                continue
            selected.append(item)
    return list(reversed(selected))


def export_log_bundle() -> BytesIO:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path in log_files():
            archive.write(path, arcname=f"logs/{path.name}")
        archive.writestr(
            "diagnostics.json",
            json.dumps(
                {
                    "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "logging": diagnostics_config(),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    output.seek(0)
    return output
