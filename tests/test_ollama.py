import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from core.ollama import OllamaError, debug_log_output, ensure_repo_context_allowed, get_ollama_host


class OllamaSecurityTests(unittest.TestCase):
    def test_get_ollama_host_accepts_default_localhost(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            host = get_ollama_host()

        self.assertEqual(host, "http://localhost:11434")

    def test_get_ollama_host_rejects_remote_without_opt_in(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_HOST": "http://example.com:11434"}, clear=True):
            with self.assertRaises(OllamaError):
                get_ollama_host()

    def test_ensure_repo_context_allowed_requires_dedicated_opt_in_for_remote_host(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OLLAMA_HOST": "http://example.com:11434",
                "OLLAMA_ALLOW_REMOTE": "1",
            },
            clear=True,
        ):
            with self.assertRaises(OllamaError):
                ensure_repo_context_allowed("git diff content")

    def test_debug_log_output_truncates_content_by_default(self) -> None:
        buffer = io.StringIO()
        with patch.dict(
            "os.environ",
            {
                "OLLAMA_DEBUG": "1",
                "OLLAMA_DEBUG_MAX_CHARS": "80",
            },
            clear=True,
        ), redirect_stdout(buffer):
            debug_log_output("Sample", "x" * 200)

        output = buffer.getvalue()
        self.assertIn("[DEBUG] Sample [truncated]:", output)
        self.assertNotIn("x" * 120, output)


if __name__ == "__main__":
    unittest.main()
