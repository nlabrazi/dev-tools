import os
import unittest
from unittest.mock import patch

from core.config import describe_base_branch_strategy, resolve_repo_base_branch


class BaseBranchResolutionTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_resolve_repo_base_branch_prefers_remote_default_when_no_override(self) -> None:
        with patch("core.config.get_default_remote_branch", return_value="main"), patch(
            "core.config.remote_branch_exists",
            return_value=True,
        ):
            branch, reason = resolve_repo_base_branch("/tmp/repo", remote="origin")

        self.assertEqual(branch, "main")
        self.assertIn("origin/HEAD", reason)

    @patch.dict(os.environ, {}, clear=True)
    def test_resolve_repo_base_branch_uses_legacy_fallback_when_remote_head_is_missing(self) -> None:
        with patch("core.config.get_default_remote_branch", return_value=None), patch(
            "core.config.remote_branch_exists",
            side_effect=lambda repo_path, branch, remote="origin": branch == "master",
        ):
            branch, reason = resolve_repo_base_branch("/tmp/repo", remote="origin")

        self.assertEqual(branch, "master")
        self.assertIn("legacy fallback", reason)

    @patch.dict(os.environ, {"DEVTOOLS_BASE_BRANCH": "release"}, clear=True)
    def test_resolve_repo_base_branch_honors_explicit_override(self) -> None:
        with patch("core.config.remote_branch_exists", return_value=True) as remote_branch_exists:
            branch, reason = resolve_repo_base_branch("/tmp/repo", remote="fork")

        self.assertEqual(branch, "release")
        self.assertIn("DEVTOOLS_BASE_BRANCH", reason)
        remote_branch_exists.assert_called_once_with("/tmp/repo", "release", "fork")

    @patch.dict(os.environ, {"DEVTOOLS_BASE_BRANCH": "release"}, clear=True)
    def test_resolve_repo_base_branch_fails_when_explicit_override_is_missing(self) -> None:
        with patch("core.config.remote_branch_exists", return_value=False):
            branch, reason = resolve_repo_base_branch("/tmp/repo", remote="origin")

        self.assertIsNone(branch)
        self.assertIn("origin/release", reason)

    @patch.dict(os.environ, {}, clear=True)
    def test_describe_base_branch_strategy_mentions_remote_default_when_not_overridden(self) -> None:
        description = describe_base_branch_strategy("origin")

        self.assertIn("origin/HEAD", description)
        self.assertIn("master", description)

    @patch.dict(os.environ, {"DEVTOOLS_BASE_BRANCH": "main"}, clear=True)
    def test_describe_base_branch_strategy_mentions_configured_branch_when_overridden(self) -> None:
        description = describe_base_branch_strategy("origin")

        self.assertIn("configured branch", description)
        self.assertIn("main", description)


if __name__ == "__main__":
    unittest.main()
