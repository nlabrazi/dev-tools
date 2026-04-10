import subprocess
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from core.commit import (
    auto_commit_all_repos,
    commit_with_message,
    get_diff_content_cached,
    git_status_porcelain,
    push_head_to_branch,
    resolve_auto_commit_target,
)


class CommitTargetTests(unittest.TestCase):
    def test_resolve_auto_commit_target_rejects_unexpected_branch(self) -> None:
        with patch("core.commit.DEFAULT_HEAD_BRANCH", "staging"), patch(
            "core.commit.DEFAULT_REMOTE", "origin"
        ), patch("core.commit.get_current_branch", return_value="feat/api"), patch(
            "core.commit.git_command"
        ) as git_command, patch("core.commit.print"):
            target = resolve_auto_commit_target("/tmp/repo")

        self.assertIsNone(target)
        git_command.assert_not_called()

    def test_resolve_auto_commit_target_accepts_expected_branch_and_remote(self) -> None:
        remote_check = subprocess.CompletedProcess(
            args=["git", "remote", "get-url", "fork"],
            returncode=0,
            stdout="git@github.com:example/repo.git\n",
            stderr="",
        )
        with patch("core.commit.DEFAULT_HEAD_BRANCH", "staging"), patch(
            "core.commit.DEFAULT_REMOTE", "fork"
        ), patch("core.commit.get_current_branch", return_value="staging"), patch(
            "core.commit.git_command", return_value=remote_check
        ) as git_command, patch("core.commit.print"):
            target = resolve_auto_commit_target("/tmp/repo")

        self.assertEqual(target, ("fork", "staging"))
        git_command.assert_called_once_with(
            "/tmp/repo",
            ["remote", "get-url", "fork"],
            silent=True,
        )

    def test_push_head_to_branch_uses_explicit_refspec(self) -> None:
        push_result = subprocess.CompletedProcess(
            args=["push", "fork", "HEAD:refs/heads/staging"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with patch("core.commit.git_command", return_value=push_result) as git_command, patch("core.commit.print"):
            pushed = push_head_to_branch("/tmp/repo", "fork", "staging")

        self.assertEqual(pushed, "pushed")
        git_command.assert_called_once_with(
            "/tmp/repo",
            ["push", "fork", "HEAD:refs/heads/staging"],
        )

    def test_commit_with_message_returns_dry_run_without_git_commit(self) -> None:
        with patch("core.commit.is_dry_run", return_value=True), patch("core.commit.git_command") as git_command, patch(
            "core.commit.print"
        ):
            status = commit_with_message("/tmp/repo", "feat: add feature")

        self.assertEqual(status, "dry-run")
        git_command.assert_not_called()

    def test_push_head_to_branch_returns_dry_run_without_git_push(self) -> None:
        with patch("core.commit.is_dry_run", return_value=True), patch("core.commit.git_command") as git_command, patch(
            "core.commit.print"
        ):
            status = push_head_to_branch("/tmp/repo", "fork", "staging")

        self.assertEqual(status, "dry-run")
        git_command.assert_not_called()

    def test_get_diff_content_cached_limits_output_size(self) -> None:
        diff_result = subprocess.CompletedProcess(
            args=["diff", "--cached", "--no-ext-diff"],
            returncode=0,
            stdout="diff --git",
            stderr="",
        )
        with patch("core.commit.git_command", return_value=diff_result) as git_command, patch(
            "core.commit.get_commit_diff_max_chars",
            return_value=321,
        ):
            diff = get_diff_content_cached("/tmp/repo")

        self.assertEqual(diff, "diff --git")
        git_command.assert_called_once_with(
            "/tmp/repo",
            ["diff", "--cached", "--no-ext-diff"],
            silent=True,
            max_output_chars=321,
        )

    def test_git_status_porcelain_preserves_leading_space_for_unstaged_first_line(self) -> None:
        status_result = subprocess.CompletedProcess(
            args=["status", "--porcelain"],
            returncode=0,
            stdout=" M first.py\nM  second.py\n",
            stderr="",
        )
        with patch("core.commit.git_command", return_value=status_result):
            status_lines = git_status_porcelain("/tmp/repo")

        self.assertEqual(status_lines, [" M first.py", "M  second.py"])


class AutoCommitWorkflowTests(unittest.TestCase):
    def test_auto_commit_all_repos_skips_when_target_validation_fails(self) -> None:
        with patch("core.commit.os.path.isdir", return_value=True), patch(
            "core.commit.iter_git_repositories",
            return_value=[("repo", "/tmp/repo")],
        ), patch("core.commit.git_status_porcelain", return_value=["M  file.py"]), patch(
            "core.commit.resolve_auto_commit_target",
            return_value=None,
        ), patch("core.commit.commit_with_message") as commit_with_message, patch(
            "core.commit.console.print"
        ), patch("core.commit.print"):
            results = auto_commit_all_repos(["/tmp/root"])

        self.assertEqual(results, {"committed": 0, "pushed": 0})
        commit_with_message.assert_not_called()

    def test_auto_commit_all_repos_pushes_to_resolved_target(self) -> None:
        with patch("core.commit.os.path.isdir", return_value=True), patch(
            "core.commit.iter_git_repositories",
            return_value=[("repo", "/tmp/repo")],
        ), patch("core.commit.git_status_porcelain", return_value=["M  file.py"]), patch(
            "core.commit.resolve_auto_commit_target",
            return_value=("fork", "staging"),
        ), patch("core.commit.get_diff_content_cached", return_value="diff --git"), patch(
            "core.commit.get_modified_files_names_cached",
            return_value=["file.py"],
        ), patch(
            "core.commit.generate_commit_message",
            return_value="feat: add feature",
        ), patch(
            "core.commit.ask_yes_no",
            side_effect=[True, True],
        ), patch(
            "core.commit.commit_with_message",
            return_value="committed",
        ) as commit_with_message, patch(
            "core.commit.push_head_to_branch",
            return_value="pushed",
        ) as push_head_to_branch_mock, patch(
            "core.commit.console.status",
            return_value=nullcontext(),
        ), patch("core.commit.console.print"), patch("core.commit.print"):
            results = auto_commit_all_repos(["/tmp/root"])

        self.assertEqual(results, {"committed": 1, "pushed": 1})
        commit_with_message.assert_called_once_with("/tmp/repo", "feat: add feature")
        push_head_to_branch_mock.assert_called_once_with("/tmp/repo", "fork", "staging")


if __name__ == "__main__":
    unittest.main()
