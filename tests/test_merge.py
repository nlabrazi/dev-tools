import unittest
from unittest.mock import patch

from core.merge import create_and_merge_pr


class MergeDryRunTests(unittest.TestCase):
    def test_create_and_merge_pr_dry_run_skips_pr_creation(self) -> None:
        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.get_current_branch",
            return_value="staging",
        ), patch(
            "core.merge.get_commit_summary",
            return_value="- feat(api): ship feature",
        ), patch(
            "core.merge.generate_pr_text_with_ollama",
            return_value=("Test PR", "## What\n- Item\n\n## Why\n- Item\n\n## Testing\n- N/A\n\n## Notes\n- N/A"),
        ), patch(
            "core.merge.existing_pr_number",
            return_value="",
        ), patch(
            "core.merge.ask_yes_no",
            return_value=True,
        ), patch(
            "core.merge.is_dry_run",
            return_value=True,
        ), patch("core.merge.run_command") as run_command, patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo")

        run_command.assert_not_called()

    def test_create_and_merge_pr_dry_run_skips_auto_merge_for_existing_pr(self) -> None:
        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.get_current_branch",
            return_value="staging",
        ), patch(
            "core.merge.get_commit_summary",
            return_value="- feat(api): ship feature",
        ), patch(
            "core.merge.generate_pr_text_with_ollama",
            return_value=("Test PR", "Body"),
        ), patch(
            "core.merge.existing_pr_number",
            return_value="42",
        ), patch(
            "core.merge.is_dry_run",
            return_value=True,
        ), patch("core.merge.merge_pr_with_retry") as merge_pr_with_retry, patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo")

        merge_pr_with_retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
