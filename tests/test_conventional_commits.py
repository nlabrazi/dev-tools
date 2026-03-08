import unittest

from core.conventional_commits import determine_bump_from_messages, parse_conventional_commit
from core.versioning import determine_bump_from_commits, parse_semver


class ConventionalCommitTests(unittest.TestCase):
    def test_parse_scoped_commit(self) -> None:
        parsed = parse_conventional_commit("feat(api): expose health endpoint")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.type, "feat")
        self.assertEqual(parsed.scope, "api")
        self.assertEqual(parsed.subject, "expose health endpoint")
        self.assertFalse(parsed.breaking)

    def test_parse_bullet_prefixed_commit(self) -> None:
        parsed = parse_conventional_commit("- fix(parser)!: reject empty payloads")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.type, "fix")
        self.assertTrue(parsed.breaking)

    def test_ui_type_is_normalized_to_style(self) -> None:
        parsed = parse_conventional_commit("ui(header): align navigation spacing")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.normalized_type, "style")

    def test_determine_bump_prefers_major(self) -> None:
        bump = determine_bump_from_messages(
            [
                "- fix(api): harden retry loop",
                "- feat(ui): add keyboard navigation",
                "- refactor!: remove legacy sync mode",
            ]
        )

        self.assertEqual(bump, "major")

    def test_versioning_detects_scoped_feature_commits(self) -> None:
        bump = determine_bump_from_commits(
            "- feat(api): add release endpoint\n"
            "- fix(worker): guard empty jobs\n"
        )

        self.assertEqual(bump, "minor")

    def test_parse_semver_supports_v_prefix(self) -> None:
        version = parse_semver("v1.2.3")

        self.assertIsNotNone(version)
        assert version is not None
        self.assertEqual(str(version), "v1.2.3")


if __name__ == "__main__":
    unittest.main()
