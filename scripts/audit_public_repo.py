from __future__ import annotations

import re
import subprocess
from pathlib import Path


FORBIDDEN_EXACT_PATHS = {".env"}
FORBIDDEN_PREFIXES = (
    ".playwright-cli/",
    ".compose-smoke-data/",
    "backups/",
    "data/",
    "output/",
)
FORBIDDEN_SUFFIXES = (
    ".db",
    ".har",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
)
SECRET_PATTERNS = {
    "OpenAI API key": re.compile(r"\bsk-(?!test-)[A-Za-z0-9_-]{16,}\b"),
    "GitHub token": re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "IMA API key": re.compile(r"\b(?:IMA_OPENAPI_APIKEY\s*=|APIKEY\s*:)\s*[A-Za-z0-9+/=]{32,}\b"),
    "private key": re.compile(r"BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY"),
}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"])
    return [Path(value.decode("utf-8")) for value in output.split(b"\0") if value]


def forbidden_path(path: Path) -> bool:
    value = path.as_posix()
    if value == ".env.example":
        return False
    return (
        value in FORBIDDEN_EXACT_PATHS
        or value.startswith(".env.")
        or value.startswith(FORBIDDEN_PREFIXES)
        or value.lower().endswith(FORBIDDEN_SUFFIXES)
    )


def secret_matches(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    matches = []
    for label, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            line_number = text.count("\n", 0, match.start()) + 1
            matches.append(f"{label}: {path.as_posix()}:{line_number}")
    return matches


def main() -> int:
    files = tracked_files()
    problems = [f"forbidden tracked path: {path.as_posix()}" for path in files if forbidden_path(path)]
    for path in files:
        problems.extend(secret_matches(path))
    if problems:
        print("Public repository audit failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"Public repository audit passed: {len(files)} tracked files checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
