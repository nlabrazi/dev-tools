import subprocess
import unittest
from unittest.mock import patch

from core.sync import ensure_local_branch_exists, pull_ff_only


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


if __name__ == "__main__":
    unittest.main()
