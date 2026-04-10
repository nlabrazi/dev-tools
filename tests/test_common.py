import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils.common import (
    is_dry_run,
    prepend_text_file,
    run_command,
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
        )

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
