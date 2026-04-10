import subprocess
import unittest
from unittest.mock import patch

from core.merge import create_and_merge_pr, get_pr_status


class MergeDryRunTests(unittest.TestCase):
    def test_get_pr_status_parses_github_cli_json(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "pr", "view"],
            returncode=0,
            stdout='{"state":"MERGED","mergedAt":"2026-04-10T10:00:00Z","mergeStateStatus":"CLEAN","isDraft":false}',
            stderr="",
        )

        with patch("core.merge.run_command", return_value=result) as run_command:
            status = get_pr_status("/tmp/repo", "42")

        self.assertEqual(
            status,
            {
                "state": "MERGED",
                "mergedAt": "2026-04-10T10:00:00Z",
                "mergeStateStatus": "CLEAN",
                "isDraft": False,
            },
        )
        run_command.assert_called_once_with(
            [
                "gh",
                "pr",
                "view",
                "42",
                "--json",
                "state,mergedAt,mergeStateStatus,isDraft",
            ],
            cwd="/tmp/repo",
        )

    def test_create_and_merge_pr_dry_run_skips_pr_creation(self) -> None:
        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.resolve_merge_base_branch",
            return_value=("main", "resolved from origin/HEAD"),
        ), patch(
            "core.merge.get_current_branch",
            return_value="staging",
        ), patch(
            "core.merge.get_commit_summary",
            return_value="- feat(api): ship feature",
        ), patch(
            "core.merge.generate_pr_text",
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
            "core.merge.generate_pr_text",
            return_value=("Test PR", "Body"),
        ), patch(
            "core.merge.existing_pr_number",
            return_value="42",
        ), patch(
            "core.merge.is_dry_run",
            return_value=True,
        ), patch(
            "core.merge.resolve_merge_base_branch",
            return_value=("main", "resolved from origin/HEAD"),
        ), patch("core.merge.merge_pr_with_retry") as merge_pr_with_retry, patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo")

        merge_pr_with_retry.assert_not_called()

    def test_create_and_merge_pr_uses_resolved_base_branch_for_pr_creation(self) -> None:
        create_result = subprocess.CompletedProcess(
            args=["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/example/repo/pull/42\n",
            stderr="",
        )
        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.resolve_merge_base_branch",
            return_value=("main", "resolved from origin/HEAD"),
        ), patch(
            "core.merge.get_current_branch",
            return_value="staging",
        ), patch(
            "core.merge.get_commit_summary",
            return_value="- feat(api): ship feature",
        ), patch(
            "core.merge.generate_pr_text",
            return_value=("Test PR", "Body"),
        ), patch(
            "core.merge.existing_pr_number",
            return_value="",
        ), patch(
            "core.merge.ask_yes_no",
            return_value=True,
        ), patch(
            "core.merge.is_dry_run",
            return_value=False,
        ), patch(
            "core.merge.run_command",
            return_value=create_result,
        ) as run_command, patch(
            "core.merge.get_pr_number_from_url",
            return_value="42",
        ), patch(
            "core.merge.merge_pr_with_retry",
            return_value=False,
        ), patch("core.merge.print"), patch("core.merge.console.status"):
            create_and_merge_pr("/tmp/repo", "repo")

        run_command.assert_called_once_with(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                "staging",
                "--title",
                "Test PR",
                "--body",
                "Body",
            ],
            cwd="/tmp/repo",
        )


if __name__ == "__main__":
    unittest.main()
