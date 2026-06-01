from __future__ import annotations

import unittest

import requests

from backend.http_policy import BROWSER_USER_AGENT, browser_headers, browser_http_session
from backend.industry_news_sources import PublicIndustryNewsCollector, ThrottledSession


class HttpPolicyTests(unittest.TestCase):
    def test_browser_helpers_use_shared_chrome_user_agent(self) -> None:
        self.assertEqual(browser_headers()["User-Agent"], BROWSER_USER_AGENT)
        self.assertIn("Chrome/148.0.0.0", BROWSER_USER_AGENT)
        self.assertEqual(browser_http_session().headers["User-Agent"], BROWSER_USER_AGENT)

    def test_industry_news_sessions_apply_shared_browser_user_agent(self) -> None:
        throttled_requests_session = requests.Session()
        throttled = ThrottledSession(session=throttled_requests_session)
        self.assertEqual(throttled.session.headers["User-Agent"], BROWSER_USER_AGENT)

        collector_requests_session = requests.Session()
        collector = PublicIndustryNewsCollector(session=collector_requests_session)
        self.assertEqual(collector.session.headers["User-Agent"], BROWSER_USER_AGENT)


if __name__ == "__main__":
    unittest.main()
