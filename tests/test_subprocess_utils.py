from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from backend.subprocess_utils import hidden_window_creationflags


class SubprocessUtilsTests(unittest.TestCase):
    def test_hidden_window_flags_are_zero_when_platform_does_not_define_windows_constant(self) -> None:
        with patch("backend.subprocess_utils.subprocess", Mock(spec=[])):
            self.assertEqual(hidden_window_creationflags(), 0)

    def test_hidden_window_flags_use_windows_constant_when_available(self) -> None:
        with patch("backend.subprocess_utils.subprocess", Mock(CREATE_NO_WINDOW=123)):
            self.assertEqual(hidden_window_creationflags(), 123)


if __name__ == "__main__":
    unittest.main()
