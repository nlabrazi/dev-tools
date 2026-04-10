import subprocess
import unittest
from unittest.mock import patch

from core.sync import describe_sync_plan, ensure_local_branch_exists, pull_ff_only, sync_default_branch


class SyncDryRunTests(unittest.TestCase):
    def test_ensure_local_branch_exists_returns_dry_run_when_branch_is_missing(self) -> None:
        missing_branch = subprocess.CompletedProcess(
            args=["git", "show-ref", "--verify", "--quiet", "refs/heads/main"],
            returncode=1,
            stdout="",
            stderr="",
        )
        with patch("core.sync.run_command", return_value=missing_branch) as run_command, patch(
            "core.sync.is_dry_run",
            return_value=True,
        ), patch("core.sync.console.print"):
            status = ensure_local_branch_exists("/tmp/repo", "repo", "main")

        self.assertEqual(status, "dry-run")
        run_command.assert_called_once_with(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/main"],
            cwd="/tmp/repo",
            silent=True,
        )

    def test_pull_ff_only_returns_dry_run_without_git_pull(self) -> None:
        with patch("core.sync.is_dry_run", return_value=True), patch("core.sync.run_command") as run_command, patch(
            "core.sync.console.print"
        ):
            status = pull_ff_only("/tmp/repo", "repo", "main")

        self.assertEqual(status, "dry-run")
        run_command.assert_not_called()

    def test_describe_sync_plan_mentions_branch_strategy(self) -> None:
        with patch("core.sync.describe_base_branch_strategy", return_value="origin/HEAD default branch"):
            prompt = describe_sync_plan()

        self.assertIn("origin/HEAD default branch", prompt)
        self.assertIn("origin", prompt)

    def test_sync_default_branch_skips_when_base_branch_cannot_be_resolved(self) -> None:
        with patch("core.sync.repo_is_clean", return_value=True), patch(
            "core.sync.fetch",
            return_value=True,
        ), patch(
            "core.sync.resolve_repo_base_branch",
            return_value=(None, "origin/HEAD is unavailable"),
        ), patch("core.sync.console.print") as console_print:
            sync_default_branch("/tmp/repo", "repo")

        console_print.assert_called_once()
        message = console_print.call_args.args[0]
        self.assertIn("origin/HEAD is unavailable", message)


if __name__ == "__main__":
    unittest.main()
