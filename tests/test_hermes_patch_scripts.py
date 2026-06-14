from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


def load_patch_module():
    script_path = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "hermes"
        / "scripts"
        / "patch_jojocode_headers.py"
    )
    spec = importlib.util.spec_from_file_location("patch_jojocode_headers", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesPatchScriptsTests(unittest.TestCase):
    def test_patch_jojocode_headers_is_idempotent(self) -> None:
        script = load_patch_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent").mkdir()
            (root / "agent" / "agent_init.py").write_text(
                '            elif base_url_host_matches(effective_base, "portal.qwen.ai"):\n'
                '                client_kwargs["default_headers"] = _ra()._qwen_portal_headers()\n',
                encoding="utf-8",
            )
            (root / "run_agent.py").write_text(
                '        elif base_url_host_matches(base_url, "portal.qwen.ai"):\n'
                '            self._client_kwargs["default_headers"] = _qwen_portal_headers()\n',
                encoding="utf-8",
            )

            first = script.patch_hermes(root)
            second = script.patch_hermes(root)

            self.assertTrue(all(first.values()))
            self.assertFalse(any(second.values()))
            agent_init = (root / "agent" / "agent_init.py").read_text(encoding="utf-8")
            run_agent = (root / "run_agent.py").read_text(encoding="utf-8")
            self.assertEqual(agent_init.count('"User-Agent": "curl/8.5.0"'), 1)
            self.assertEqual(run_agent.count('"User-Agent": "curl/8.5.0"'), 1)
            self.assertIn('base_url_host_matches(effective_base, "jojocode.com")', agent_init)
            self.assertIn('base_url_host_matches(base_url, "max.jojocode.com")', run_agent)
            self.assertIn('"User-Agent": "curl/8.5.0"', agent_init)
            self.assertIn('"Accept": "*/*"', run_agent)


if __name__ == "__main__":
    unittest.main()
