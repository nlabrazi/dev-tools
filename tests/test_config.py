import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import (
    describe_base_branch_strategy,
    is_unity_excluded,
    is_unity_project,
    resolve_repo_base_branch,
    resolve_root_dirs,
    set_exclude_unity,
)


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


class UnityExclusionConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_exclude_unity(None)

    def test_is_unity_project_detects_project_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.assertFalse(is_unity_project(root))

            settings = root / "ProjectSettings"
            settings.mkdir()
            (settings / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.0f1\n", encoding="utf-8")
            self.assertTrue(is_unity_project(root))

    def test_is_unity_excluded_defaults_to_true(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            set_exclude_unity(None)
            self.assertTrue(is_unity_excluded())

    def test_is_unity_excluded_honors_env_vars(self) -> None:
        with patch.dict(os.environ, {"DEVTOOLS_INCLUDE_UNITY": "1"}, clear=True):
            set_exclude_unity(None)
            self.assertFalse(is_unity_excluded())

        with patch.dict(os.environ, {"DEVTOOLS_EXCLUDE_UNITY": "0"}, clear=True):
            set_exclude_unity(None)
            self.assertFalse(is_unity_excluded())

        with patch.dict(os.environ, {"DEVTOOLS_EXCLUDE_UNITY": "1"}, clear=True):
            set_exclude_unity(None)
            self.assertTrue(is_unity_excluded())

    def test_set_exclude_unity_runtime_override(self) -> None:
        with patch.dict(os.environ, {"DEVTOOLS_INCLUDE_UNITY": "1"}, clear=True):
            set_exclude_unity(True)
            self.assertTrue(is_unity_excluded())
            set_exclude_unity(False)
            self.assertFalse(is_unity_excluded())

    def test_resolve_root_dirs_excludes_unity_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch("os.path.isdir", return_value=True):
            set_exclude_unity(None)
            roots = resolve_root_dirs()
            self.assertFalse(any("Unity" in r for r in roots))

    def test_resolve_root_dirs_includes_unity_when_enabled(self) -> None:
        with patch.dict(os.environ, {"DEVTOOLS_INCLUDE_UNITY": "1"}, clear=True), patch("os.path.isdir", return_value=True):
            set_exclude_unity(None)
            roots = resolve_root_dirs()
            self.assertTrue(any("Unity" in r for r in roots))


if __name__ == "__main__":
    unittest.main()
