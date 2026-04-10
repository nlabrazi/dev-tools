import subprocess
import unittest
from unittest.mock import patch

from utils.git import git_command, git_command_checked, git_output


class GitHelperTests(unittest.TestCase):
    def test_git_command_prefixes_git_and_uses_default_timeout(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("utils.git.run_command", return_value=completed) as run_command, patch(
            "utils.git.get_git_timeout",
            return_value=42,
        ):
            result = git_command("/tmp/repo", ["status"])

        self.assertEqual(result.returncode, 0)
        run_command.assert_called_once_with(
            ["git", "status"],
            cwd="/tmp/repo",
            silent=True,
            timeout=42,
        )

    def test_git_output_returns_empty_string_on_failure(self) -> None:
        failed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=1,
            stdout="fatal\n",
            stderr="fatal",
        )
        with patch("utils.git.git_command", return_value=failed):
            output = git_output("/tmp/repo", ["status"])

        self.assertEqual(output, "")

    def test_git_command_checked_prefixes_git_and_uses_context(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["git", "rev-parse", "HEAD"],
            returncode=0,
            stdout="abc123\n",
            stderr="",
        )
        with patch("utils.git.run_command_checked", return_value=completed) as run_command_checked, patch(
            "utils.git.get_git_timeout",
            return_value=15,
        ):
            result = git_command_checked("/tmp/repo", ["rev-parse", "HEAD"], context="resolve head")

        self.assertEqual(result.stdout, "abc123\n")
        run_command_checked.assert_called_once_with(
            ["git", "rev-parse", "HEAD"],
            cwd="/tmp/repo",
            silent=True,
            context="resolve head",
            timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
