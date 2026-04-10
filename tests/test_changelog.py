import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from core.changelog import (
    CHANGELOG_FILENAME,
    CHANGELOG_TITLE,
    classify_commits,
    commit_and_push_changelog,
    generate_changelog,
    get_commits_since_tag,
    resolve_changelog_version_label,
    update_changelog,
    upsert_changelog_section,
)


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

    def test_generate_changelog_uses_unreleased_without_date(self) -> None:
        output = generate_changelog(
            [
                "feat(api): add release endpoint",
                "fix(worker): guard empty jobs",
                "misc cleanup",
            ],
            "Unreleased",
        )

        self.assertTrue(output.startswith("## [Unreleased]\n"))
        self.assertNotIn("## [Unreleased] - ", output)
        self.assertIn("### ✨ Feat", output)
        self.assertIn("- add release endpoint", output)
        self.assertIn("### 🐛 Fix", output)
        self.assertIn("- guard empty jobs", output)
        self.assertIn("### 🔖 Others", output)
        self.assertIn("- misc cleanup", output)

    def test_generate_changelog_dates_release_versions(self) -> None:
        with patch("core.changelog.datetime") as mocked_datetime:
            mocked_datetime.now.return_value.strftime.return_value = "2026-04-10"
            output = generate_changelog(["feat(api): add release endpoint"], "v1.3.0")

        self.assertTrue(output.startswith("## [v1.3.0] - 2026-04-10\n"))

    def test_upsert_changelog_section_is_idempotent_and_deduplicates_same_label(self) -> None:
        existing = (
            "## [Unreleased]\n\n- old entry\n\n"
            "## [Unreleased]\n\n- older duplicate\n\n"
            f"{CHANGELOG_TITLE}\n\n"
            "## [v1.2.3] - 2026-04-01\n\n- shipped\n"
        )
        section = "## [Unreleased]\n\n### ✨ Feat\n- add release endpoint\n"

        updated_once = upsert_changelog_section(existing, section, "Unreleased")
        updated_twice = upsert_changelog_section(updated_once, section, "Unreleased")

        self.assertEqual(updated_once, updated_twice)
        self.assertEqual(updated_once.count("## [Unreleased]"), 1)
        self.assertTrue(updated_once.startswith(f"{CHANGELOG_TITLE}\n\n## [Unreleased]"))

    def test_resolve_changelog_version_label_prefers_existing_unreleased(self) -> None:
        label = resolve_changelog_version_label(
            "/tmp/repo",
            ["feat(api): add release endpoint"],
            f"{CHANGELOG_TITLE}\n\n## [Unreleased]\n\n- existing\n",
        )

        self.assertEqual(label, "Unreleased")

    def test_resolve_changelog_version_label_uses_next_semver_without_unreleased(self) -> None:
        with patch("core.changelog.get_last_tag", return_value="v1.2.3"), patch(
            "core.changelog.compute_next_version_from_messages",
            return_value="v1.3.0",
        ) as compute_next_version:
            label = resolve_changelog_version_label(
                "/tmp/repo",
                ["feat(api): add release endpoint"],
                f"{CHANGELOG_TITLE}\n\n## [v1.2.3] - 2026-04-01\n\n- shipped\n",
            )

        self.assertEqual(label, "v1.3.0")
        compute_next_version.assert_called_once_with(
            "/tmp/repo",
            ["feat(api): add release endpoint"],
            default_first="v0.1.0",
        )

    def test_update_changelog_writes_new_section_and_then_becomes_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            target = repo_path / CHANGELOG_FILENAME
            target.write_text(f"{CHANGELOG_TITLE}\n\n## [v1.2.3] - 2026-04-01\n\n- shipped\n", encoding="utf-8")
            section = "## [v1.3.0] - 2026-04-10\n\n### ✨ Feat\n- add release endpoint\n"

            first_status = update_changelog(str(repo_path), section, "v1.3.0")
            second_status = update_changelog(str(repo_path), section, "v1.3.0")

            self.assertEqual(first_status, "updated")
            self.assertEqual(second_status, "noop")
            content = target.read_text(encoding="utf-8")
            self.assertIn("## [v1.3.0] - 2026-04-10", content)
            self.assertEqual(content.count("## [v1.3.0]"), 1)

    def test_update_changelog_dry_run_reports_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_path = Path(tmp_dir)
            target = repo_path / CHANGELOG_FILENAME
            original = f"{CHANGELOG_TITLE}\n"
            target.write_text(original, encoding="utf-8")
            section = "## [Unreleased]\n\n### ✨ Feat\n- add release endpoint\n"

            with patch("core.changelog.is_dry_run", return_value=True):
                status = update_changelog(str(repo_path), section, "Unreleased")

            self.assertEqual(status, "dry-run")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_commit_and_push_changelog_aborts_before_staging_when_other_files_are_staged(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.get_staged_files",
            return_value=["README.md"],
        ), patch("core.changelog.run_command_checked") as run_command_checked, patch("core.changelog.print"):
            result = commit_and_push_changelog("/tmp/repo")

        self.assertFalse(result)
        run_command_checked.assert_not_called()

    def test_commit_and_push_changelog_stages_file_only_when_needed(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.get_staged_files",
            side_effect=[[], ["CHANGELOG.md"]],
        ), patch(
            "core.changelog.changelog_has_any_changes",
            return_value=True,
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

    def test_commit_and_push_changelog_reuses_existing_staged_changelog(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.get_staged_files",
            return_value=["CHANGELOG.md"],
        ), patch(
            "core.changelog.run_command_checked"
        ) as run_command_checked, patch(
            "core.changelog.datetime"
        ) as mocked_datetime, patch("core.changelog.print"):
            mocked_datetime.now.return_value.strftime.return_value = "2026-04-10 12:34"

            result = commit_and_push_changelog("/tmp/repo")

        self.assertTrue(result)
        self.assertEqual(
            run_command_checked.call_args_list,
            [
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

    def test_commit_and_push_changelog_unstages_if_it_staged_the_file_and_commit_fails(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.get_staged_files",
            side_effect=[[], ["CHANGELOG.md"]],
        ), patch(
            "core.changelog.changelog_has_any_changes",
            return_value=True,
        ), patch(
            "core.changelog.run_command_checked",
            side_effect=[None, RuntimeError("commit failed")],
        ), patch("core.changelog.unstage_changelog") as unstage_changelog, patch("core.changelog.print"):
            result = commit_and_push_changelog("/tmp/repo")

        self.assertFalse(result)
        unstage_changelog.assert_called_once_with("/tmp/repo")

    def test_commit_and_push_changelog_dry_run_reports_simulation(self) -> None:
        with patch("core.changelog.get_current_branch", return_value="staging"), patch(
            "core.changelog.is_dry_run",
            return_value=True,
        ), patch("core.changelog.run_command_checked") as run_command_checked, patch("core.changelog.print"):
            result = commit_and_push_changelog("/tmp/repo")

        self.assertFalse(result)
        run_command_checked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
