import unittest
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import patch

from core.commit_message import (
    build_fallback_commit_message,
    extract_plain_commit,
    generate_commit_message,
    generate_commit_message_with_ollama,
)


class CommitMessageGenerationTests(unittest.TestCase):
    def test_extract_plain_commit_supports_commit_prefix_and_body(self) -> None:
        raw = """
        Commit: feat(api): ship endpoint

        body: add release route
        body: cover error path
        """

        commit_message = extract_plain_commit(raw)

        self.assertEqual(
            commit_message,
            "feat(api): ship endpoint\n\n- add release route\n- cover error path",
        )

    def test_generate_commit_message_with_ollama_builds_from_valid_json(self) -> None:
        raw = """
        {
          "commit": {
            "type": "feat",
            "scope": "api",
            "subject": "ship endpoint",
            "body": "- add route",
            "breaking": false
          }
        }
        """

        with patch("core.commit_message.chat_json", return_value=raw), patch(
            "core.commit_message.console.status",
            return_value=nullcontext(),
        ), patch("core.commit_message.print"):
            commit_message = generate_commit_message_with_ollama("repo", ["api.py"], "diff --git")

        self.assertEqual(commit_message, "feat(api): ship endpoint\n\n- add route")

    def test_generate_commit_message_with_ollama_uses_plain_text_fallback_from_model_output(self) -> None:
        raw = "feat(api): ship endpoint\n\nadd route"

        with patch("core.commit_message.chat_json", return_value=raw), patch(
            "core.commit_message.console.status",
            return_value=nullcontext(),
        ), patch("core.commit_message.print"):
            commit_message = generate_commit_message_with_ollama("repo", ["api.py"], "diff --git")

        self.assertEqual(commit_message, "feat(api): ship endpoint\n\n- add route")

    def test_generate_commit_message_returns_heuristic_fallback_when_ollama_fails(self) -> None:
        with patch("core.commit_message.generate_commit_message_with_ollama", return_value=None):
            commit_message = generate_commit_message(
                "repo",
                ["api.py", "worker.py"],
                "+ def ship_release():\n+     return True",
                now=datetime(2026, 4, 10, 12, 34),
            )

        self.assertEqual(commit_message, "feat: update api.py and worker.py (2026-04-10 12:34)")

    def test_build_fallback_commit_message_uses_generic_title_without_files(self) -> None:
        commit_message = build_fallback_commit_message(
            [],
            "+ fix crash\n- error path",
            now=datetime(2026, 4, 10, 12, 34),
        )

        self.assertEqual(commit_message, "fix: auto commit based on diff analysis (2026-04-10 12:34)")


if __name__ == "__main__":
    unittest.main()
