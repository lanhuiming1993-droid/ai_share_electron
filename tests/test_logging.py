from __future__ import annotations

import unittest

from backend.logging_config import redact


class LoggingBehaviorTests(unittest.TestCase):
    def test_redaction_hides_credentials_but_preserves_token_usage_metrics(self) -> None:
        mock_key = "sk-" + "test-secret-123456789"
        mock_phone = "150" + "78045240"
        payload = redact(
            {
                "api_key": mock_key,
                "authorization": "Bearer private",
                "message": f"token=top-secret {mock_key} {mock_phone}",
                "input_tokens": 176,
                "output_tokens": 78,
            }
        )

        self.assertEqual(payload["api_key"], "[REDACTED]")
        self.assertEqual(payload["authorization"], "[REDACTED]")
        self.assertEqual(payload["message"], "token=[REDACTED] [REDACTED] [REDACTED]")
        self.assertEqual(payload["input_tokens"], 176)
        self.assertEqual(payload["output_tokens"], 78)


if __name__ == "__main__":
    unittest.main()
