from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from backend.browser_session import (
    check,
    cleanup_stale_chromium_singleton,
    evaluate_login_url,
    read_login_state,
    write_login_state,
)


class BrowserSessionTests(unittest.TestCase):
    def test_zsxq_group_page_is_accepted_as_authenticated(self) -> None:
        available, message = evaluate_login_url(
            "https://wx.zsxq.com/group/28888222124181",
            "/legacy-path",
            "zsxq",
        )
        self.assertTrue(available)
        self.assertIn("知识星球", message)

    def test_login_state_round_trip_tracks_active_browser(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir)
            write_login_state(profile, "https://wx.zsxq.com/group/28888222124181", active=True)
            state = read_login_state(profile)
        self.assertTrue(state["active"])
        self.assertEqual(state["url"], "https://wx.zsxq.com/group/28888222124181")
        self.assertLess(time.time() - state["updated_at"], 2)

    def test_check_reads_live_state_when_login_browser_owns_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir)
            write_login_state(profile, "https://wx.zsxq.com/group/28888222124181", active=True)
            playwright = MagicMock()
            playwright.chromium.launch_persistent_context.side_effect = PlaywrightError(
                "The profile appears to be in use by another Chromium process"
            )
            manager = MagicMock()
            manager.__enter__.return_value = playwright
            output = StringIO()
            with patch("backend.browser_session.sync_playwright", return_value=manager), redirect_stdout(output):
                check(profile, "https://wx.zsxq.com/group/28888222124181", "/group", "", "zsxq")
        result = json.loads(output.getvalue())
        self.assertTrue(result["available"])
        self.assertIn("知识星球", result["message"])

    def test_cleanup_removes_singleton_files_left_by_old_container(self) -> None:
        with patch("backend.browser_session.os.readlink", return_value="old-container-42"), patch(
            "backend.browser_session.socket.gethostname", return_value="new-container"
        ), patch.object(Path, "unlink") as unlink:
            removed = cleanup_stale_chromium_singleton(Path("/profile"))
        self.assertTrue(removed)
        self.assertEqual(unlink.call_count, 3)

    def test_cleanup_preserves_singleton_files_for_running_local_browser(self) -> None:
        with patch("backend.browser_session.os.readlink", return_value="current-container-42"), patch(
            "backend.browser_session.socket.gethostname", return_value="current-container"
        ), patch("backend.browser_session.os.kill"), patch.object(Path, "unlink") as unlink:
            removed = cleanup_stale_chromium_singleton(Path("/profile"))
        self.assertFalse(removed)
        unlink.assert_not_called()


if __name__ == "__main__":
    unittest.main()
