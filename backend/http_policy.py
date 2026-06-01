from __future__ import annotations

import requests

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)


def browser_headers(**headers: str) -> dict[str, str]:
    return {"User-Agent": BROWSER_USER_AGENT, **headers}


def browser_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(browser_headers())
    return session
