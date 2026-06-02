from __future__ import annotations

import subprocess


def hidden_window_creationflags() -> int:
    """Hide helper windows on Windows while remaining valid on Unix containers."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
