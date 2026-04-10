import unittest

from core.formatters import build_conventional_commit, build_pr, safe_parse_json


class FormatterTests(unittest.TestCase):
    def test_safe_parse_json_returns_none_for_invalid_types(self) -> None:
        self.assertIsNone(safe_parse_json(None))
        self.assertIsNone(safe_parse_json(""))
        self.assertIsNone(safe_parse_json("   "))
        self.assertIsNone(safe_parse_json(123))  # type: ignore[arg-type]

    def test_safe_parse_json_normalizes_top_level_commit_payload(self) -> None:
        parsed = safe_parse_json({"type": "feat", "subject": "ship feature"})  # type: ignore[arg-type]

        self.assertEqual(
            parsed,
            {"commit": {"type": "feat", "subject": "ship feature"}},
        )

    def test_build_conventional_commit_rejects_malformed_payload_with_value_error(self) -> None:
        with self.assertRaises(ValueError):
            build_conventional_commit(None)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            build_conventional_commit({"commit": {"type": 1, "subject": True}})

    def test_build_conventional_commit_ignores_invalid_optional_body(self) -> None:
        commit = build_conventional_commit({"commit": {"type": "feat", "subject": "ok", "body": ["x"]}})

        self.assertEqual(commit, "feat: ok")

    def test_build_conventional_commit_parses_string_breaking_flag(self) -> None:
        commit = build_conventional_commit(
            {
                "commit": {
                    "type": "feat",
                    "subject": "ship feature",
                    "breaking": "true",
                }
            }
        )

        self.assertIn("BREAKING CHANGE: yes", commit)

    def test_build_pr_rejects_malformed_payload_with_value_error(self) -> None:
        with self.assertRaises(ValueError):
            build_pr(None)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            build_pr({"mr": {"title": 1, "description": []}})

        with self.assertRaises(ValueError):
            build_pr({"mr": {"title": "ok", "description": {"a": 1}}})


if __name__ == "__main__":
    unittest.main()
