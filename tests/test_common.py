import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.common import (
    CommandTimedOutError,
    DryRunBlockedError,
    describe_command_failure,
    is_dry_run,
    is_timeout_result,
    prepend_text_file,
    run_command,
    run_command_checked,
    set_dry_run,
)


class DryRunTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_dry_run(False)

    def test_run_command_blocks_mutations_in_dry_run(self) -> None:
        set_dry_run(True)

        with patch("utils.common.subprocess.run") as subprocess_run, patch("builtins.print"):
            result = run_command(["git", "commit", "-m", "test"], cwd="/tmp/repo")

        self.assertEqual(result.returncode, 99)
        self.assertIn("blocked mutating command", result.stderr)
        subprocess_run.assert_not_called()

    def test_run_command_allows_readonly_git_commands_in_dry_run(self) -> None:
        set_dry_run(True)

        completed = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("utils.common.subprocess.run", return_value=completed) as subprocess_run, patch(
            "builtins.print"
        ):
            result = run_command(["git", "status"], cwd="/tmp/repo", silent=True)

        self.assertEqual(result.returncode, 0)
        self.assertTrue(is_dry_run())
        subprocess_run.assert_called_once_with(
            ["git", "status"],
            cwd="/tmp/repo",
            capture_output=True,
            text=True,
            timeout=None,
        )

    def test_run_command_allows_fetch_and_readonly_gh_commands_in_dry_run(self) -> None:
        set_dry_run(True)

        fetch_completed = subprocess.CompletedProcess(
            args=["git", "fetch", "--all", "--prune"],
            returncode=0,
            stdout="",
            stderr="",
        )
        gh_completed = subprocess.CompletedProcess(
            args=["gh", "pr", "view", "42", "--json", "state"],
            returncode=0,
            stdout='{"state":"OPEN"}',
            stderr="",
        )
        with patch("utils.common.subprocess.run", side_effect=[fetch_completed, gh_completed]) as subprocess_run, patch(
            "builtins.print"
        ):
            fetch_result = run_command(["git", "fetch", "--all", "--prune"], cwd="/tmp/repo", silent=True)
            gh_result = run_command(["gh", "pr", "view", "42", "--json", "state"], cwd="/tmp/repo", silent=True)

        self.assertEqual(fetch_result.returncode, 0)
        self.assertEqual(gh_result.returncode, 0)
        self.assertEqual(subprocess_run.call_count, 2)

    def test_run_command_checked_raises_dry_run_blocked_error_for_mutations(self) -> None:
        set_dry_run(True)

        with patch("builtins.print"), self.assertRaises(DryRunBlockedError):
            run_command_checked(["git", "commit", "-m", "test"], cwd="/tmp/repo", context="commit changes")

    def test_run_command_returns_timeout_result_when_subprocess_times_out(self) -> None:
        timeout_error = subprocess.TimeoutExpired(cmd=["git", "status"], timeout=5)

        with patch("utils.common.subprocess.run", side_effect=timeout_error):
            result = run_command(["git", "status"], cwd="/tmp/repo", timeout=5)

        self.assertTrue(is_timeout_result(result))
        self.assertIn("command timed out after 5s", result.stderr)

    def test_run_command_checked_raises_command_timeout_error(self) -> None:
        timeout_error = subprocess.TimeoutExpired(cmd=["git", "status"], timeout=3)

        with patch("utils.common.subprocess.run", side_effect=timeout_error), self.assertRaises(CommandTimedOutError):
            run_command_checked(["git", "status"], cwd="/tmp/repo", context="git status", timeout=3)

    def test_run_command_limits_large_stdout_without_loading_full_result(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('A' * 6000)",
            ],
            max_output_chars=240,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("[truncated]", result.stdout)
        self.assertLessEqual(len(result.stdout), 240)

    def test_run_command_checked_trims_large_failure_details(self) -> None:
        failed = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=1,
            stdout="",
            stderr="fatal: " + ("x" * 5000),
        )

        with patch("utils.common.run_command", return_value=failed), self.assertRaises(RuntimeError) as ctx:
            run_command_checked(["git", "diff"], cwd="/tmp/repo", context="collect diff")

        self.assertIn("collect diff failed:", str(ctx.exception))
        self.assertIn("[truncated]", str(ctx.exception))

    def test_describe_command_failure_strips_timeout_marker(self) -> None:
        timed_out = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=124,
            stdout="",
            stderr="COMMAND_TIMEOUT: command timed out after 3s\npartial stderr",
        )

        details = describe_command_failure(timed_out)

        self.assertFalse(details.startswith("COMMAND_TIMEOUT:"))
        self.assertIn("command timed out after 3s", details)

    def test_prepend_text_file_is_blocked_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "CHANGELOG.md"
            target.write_text("existing\n", encoding="utf-8")
            set_dry_run(True)

            with patch("builtins.print"):
                updated = prepend_text_file(str(target), "new header\n")

            self.assertFalse(updated)
            self.assertEqual(target.read_text(encoding="utf-8"), "existing\n")

    def test_prepend_text_file_prepends_content_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "CHANGELOG.md"
            target.write_text("existing\n", encoding="utf-8")
            set_dry_run(False)

            updated = prepend_text_file(str(target), "new header\n")

            self.assertTrue(updated)
            self.assertEqual(target.read_text(encoding="utf-8"), "new header\nexisting\n")
