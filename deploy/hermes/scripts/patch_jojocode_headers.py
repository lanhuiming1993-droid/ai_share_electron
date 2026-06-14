#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


JOJO_HEADER_BLOCK_AGENT_INIT = """\
            elif (
                base_url_host_matches(effective_base, "jojocode.com")
                or base_url_host_matches(effective_base, "max.jojocode.com")
            ):
                client_kwargs["default_headers"] = {
                    "User-Agent": "curl/8.5.0",
                    "Accept": "*/*",
                }
"""

JOJO_HEADER_BLOCK_RUN_AGENT = """\
        elif (
            base_url_host_matches(base_url, "jojocode.com")
            or base_url_host_matches(base_url, "max.jojocode.com")
        ):
            self._client_kwargs["default_headers"] = {
                "User-Agent": "curl/8.5.0",
                "Accept": "*/*",
            }
"""


def patch_once(path: Path, marker: str, insert_before: str, block: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return False
    if insert_before not in text:
        raise RuntimeError(f"Could not find insertion point in {path}: {insert_before!r}")
    path.write_text(text.replace(insert_before, block + insert_before), encoding="utf-8")
    return True


def patch_hermes(root: Path) -> dict[str, bool]:
    agent_init = root / "agent" / "agent_init.py"
    run_agent = root / "run_agent.py"
    changed = {
        str(agent_init): patch_once(
            agent_init,
            'base_url_host_matches(effective_base, "jojocode.com")',
            '            elif base_url_host_matches(effective_base, "portal.qwen.ai"):\n',
            JOJO_HEADER_BLOCK_AGENT_INIT,
        ),
        str(run_agent): patch_once(
            run_agent,
            'base_url_host_matches(base_url, "jojocode.com")',
            '        elif base_url_host_matches(base_url, "portal.qwen.ai"):\n',
            JOJO_HEADER_BLOCK_RUN_AGENT,
        ),
    }
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch Hermes OpenAI clients for JOJO Code Cloudflare headers.")
    parser.add_argument(
        "--hermes-agent-root",
        type=Path,
        default=Path.home() / ".hermes" / "hermes-agent",
        help="Path to the hermes-agent source directory.",
    )
    args = parser.parse_args()
    changed = patch_hermes(args.hermes_agent_root)
    for path, did_change in changed.items():
        print(f"{path}: {'patched' if did_change else 'already patched'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
