import unittest
from unittest.mock import ANY, call, patch

from core.changelog import classify_commits, commit_and_push_changelog, generate_changelog, get_commits_since_tag


class ChangelogTests(unittest.TestCase):
    def test_get_commits_since_tag_filters_maintenance_noise(self) -> None:
        with patch(
            "core.changelog.run_git_command",
            return_value="\n".join(
                [
                    "feat(api): add release endpoint",
                    "docs: update changelog",
                    "fix(worker): guard empty jobs",
                    "Initial commit",
                ]
            ),
        ) as run_git_command:
            commits = get_commits_since_tag("/tmp/repo", "v1.2.3")

        self.assertEqual(
            commits,
            [
                "feat(api): add release endpoint",
                "fix(worker): guard empty jobs",
            ],
        )
        run_git_command.assert_called_once_with(
            "/tmp/repo",
            ["log", "v1.2.3..HEAD", "--pretty=format:%s", "--no-merges"],
        )

    def test_classify_commits_keeps_uncategorized_messages(self) -> None:
        categorized, uncategorized = classify_commits(
            [
                "feat(api): add release endpoint",
                "ui(header): align navigation spacing",
                "merge branch staging into master",
            ]
        )

        self.assertEqual(categorized["feat"], ["add release endpoint"])
        self.assertEqual(categorized["style"], ["align navigation spacing"])
        self.assertEqual(uncategorized, ["merge branch staging into master"])

    def test_generate_changelog_groups_known_and_unknown_commits(self) -> None:
        output = generate_changelog(
            [
                "feat(api): add release endpoint",
                "fix(worker): guard empty jobs",
                "misc cleanup",
            ],
            "v1.2.3",
        )

        self.assertIn("## [v1.2.3] - ", output)
        self.assertIn("### ✨ Feat", output)
        self.assertIn("- add release endpoint", output)
        self.assertIn("### 🐛 Fix", output)
        self.assertIn("- guard empty jobs", output)
        self.assertIn("### 🔖 Others", output)
        self.assertIn("- misc cleanup", output)

    def test_commit_and_push_changelog_aborts_when_other_files_are_staged(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.get_staged_files",
            return_value=["CHANGELOG.md", "README.md"],
        ), patch("core.changelog.run_command_checked") as run_command_checked, patch(
            "core.changelog.print"
        ):
            result = commit_and_push_changelog("/tmp/repo")

        self.assertFalse(result)
        run_command_checked.assert_called_once_with(
            ["git", "add", "--", "CHANGELOG.md"],
            cwd="/tmp/repo",
            context="stage CHANGELOG.md",
        )

    def test_commit_and_push_changelog_runs_git_flow_for_changelog_only(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.get_staged_files",
            return_value=["CHANGELOG.md"],
        ), patch("core.changelog.run_command_checked") as run_command_checked, patch(
            "core.changelog.datetime"
        ) as mocked_datetime, patch("core.changelog.print"):
            mocked_datetime.now.return_value.strftime.return_value = "2026-04-10 12:34"

            result = commit_and_push_changelog("/tmp/repo")

        self.assertTrue(result)
        self.assertEqual(
            run_command_checked.call_args_list,
            [
                call(
                    ["git", "add", "--", "CHANGELOG.md"],
                    cwd="/tmp/repo",
                    context="stage CHANGELOG.md",
                ),
                call(
                    ["git", "commit", "-m", "docs: update changelog (2026-04-10 12:34)"],
                    cwd="/tmp/repo",
                    context="commit changelog",
                ),
                call(
                    ["git", "push", "origin", "staging"],
                    cwd="/tmp/repo",
                    context="push changelog to origin/staging",
                ),
            ],
        )

    def test_commit_and_push_changelog_dry_run_reports_simulation(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.is_dry_run",
            return_value=True,
        ), patch("core.changelog.run_command_checked") as run_command_checked, patch("core.changelog.print"):
            result = commit_and_push_changelog("/tmp/repo")

        self.assertFalse(result)
        run_command_checked.assert_not_called()
