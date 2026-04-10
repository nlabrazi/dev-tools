import unittest
from datetime import datetime
from unittest.mock import patch

from core.pr_message import (
    build_fallback_pr_text,
    extract_plain_pr,
    generate_pr_text,
    generate_pr_text_with_ollama,
)


class PRMessageGenerationTests(unittest.TestCase):
    def test_extract_plain_pr_supports_title_prefix_and_preserves_sections(self) -> None:
        raw = """
        TITLE: Release staging into main

        ## What
        - ship feature

        ## Why
        - align release branch

        ## Testing
        - not provided

        ## Notes
        - none
        """

        title, body = extract_plain_pr(raw)

        self.assertEqual(title, "Release staging into main")
        self.assertIn("## What", body)
        self.assertIn("## Notes", body)

    def test_generate_pr_text_with_ollama_builds_from_valid_json(self) -> None:
        raw = """
        {
          "mr": {
            "title": "Release staging into main",
            "description": "## What\\n- ship feature\\n\\n## Why\\n- align release\\n\\n## Testing\\n- not provided\\n\\n## Notes\\n- none"
          }
        }
        """

        with patch.dict(
            "os.environ",
            {"OLLAMA_HOST": "http://localhost:11434"},
            clear=True,
        ), patch("core.pr_message.chat_json", return_value=raw), patch("core.pr_message.print"):
            title, body = generate_pr_text_with_ollama("repo", "- feat(api): ship feature", "main")

        self.assertEqual(title, "Release staging into main")
        self.assertIn("## Testing", body)

    def test_generate_pr_text_with_ollama_uses_plain_text_fallback_from_model_output(self) -> None:
        raw = """
        TITLE: Release staging into main

        ## What
        - ship feature

        ## Why
        - align release

        ## Testing
        - not provided

        ## Notes
        - none
        """

        with patch.dict(
            "os.environ",
            {"OLLAMA_HOST": "http://localhost:11434"},
            clear=True,
        ), patch("core.pr_message.chat_json", return_value=raw), patch("core.pr_message.print"):
            title, body = generate_pr_text_with_ollama("repo", "- feat(api): ship feature", "main")

        self.assertEqual(title, "Release staging into main")
        self.assertIn("## What", body)

    def test_generate_pr_text_returns_fallback_when_ollama_fails(self) -> None:
        with patch("core.pr_message.generate_pr_text_with_ollama", return_value=(None, None)):
            title, body = generate_pr_text(
                "repo",
                "- feat(api): ship feature",
                "main",
                head_branch="staging",
                now=datetime(2026, 4, 10, 12, 34),
            )

        self.assertEqual(title, "🔀 chore: merge staging into main (2026-04-10 12:34)")
        self.assertIn("`staging`", body)
        self.assertIn("- feat(api): ship feature", body)

    def test_generate_pr_text_with_ollama_skips_remote_context_without_opt_in(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OLLAMA_HOST": "http://example.com:11434",
                "OLLAMA_ALLOW_REMOTE": "1",
            },
            clear=True,
        ), patch("core.pr_message.chat_json") as chat_json, patch("core.pr_message.print"):
            title, body = generate_pr_text_with_ollama("repo", "- feat(api): ship feature", "main")

        self.assertIsNone(title)
        self.assertIsNone(body)
        chat_json.assert_not_called()

    def test_build_fallback_pr_text_keeps_existing_merge_summary_shape(self) -> None:
        title, body = build_fallback_pr_text(
            "- feat(api): ship feature",
            "main",
            head_branch="staging",
            now=datetime(2026, 4, 10, 12, 34),
        )

        self.assertEqual(title, "🔀 chore: merge staging into main (2026-04-10 12:34)")
        self.assertIn("## 📦 Merge Summary", body)
        self.assertIn("_Auto-generated on 2026-04-10 12:34_", body)


if __name__ == "__main__":
    unittest.main()
