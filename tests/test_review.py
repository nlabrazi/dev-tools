import subprocess
import unittest
from unittest.mock import call, patch

from core.review import (
    DEFAULT_REVIEW_DIFF_MAX_CHARS,
    REVIEW_TARGET_BRANCH,
    REVIEW_TARGET_COMMIT,
    REVIEW_TARGET_STAGED,
    REVIEW_TARGET_WORKTREE,
    ReviewContextError,
    collect_review_context,
    get_review_diff_max_chars,
)


def completed(args: list[str], stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class ReviewContextCollectionTests(unittest.TestCase):
    def test_get_review_diff_max_chars_uses_environment_with_minimum(self) -> None:
        with patch.dict("os.environ", {"OLLAMA_MAX_REVIEW_DIFF_CHARS": "16000"}, clear=True):
            configured = get_review_diff_max_chars()
        with patch.dict("os.environ", {"OLLAMA_MAX_REVIEW_DIFF_CHARS": "999"}, clear=True):
            too_small = get_review_diff_max_chars()

        self.assertEqual(configured, 16000)
        self.assertEqual(too_small, DEFAULT_REVIEW_DIFF_MAX_CHARS)

    def test_collect_worktree_context_uses_git_diff_and_file_list(self) -> None:
        diff_result = completed(["git", "diff"], "diff --git a/app.py b/app.py\n+print('hello')\n")
        files_result = completed(["git", "diff", "--name-only"], "app.py\napp.py\n")

        with patch("core.review.get_review_diff_max_chars", return_value=4096), patch(
            "core.review.git_command",
            side_effect=[diff_result, files_result],
        ) as git_command:
            context = collect_review_context("/tmp/repo", REVIEW_TARGET_WORKTREE)

        self.assertEqual(context.target, REVIEW_TARGET_WORKTREE)
        self.assertEqual(context.label, "worktree changes")
        self.assertEqual(context.files, ["app.py"])
        self.assertTrue(context.has_changes)
        git_command.assert_has_calls(
            [
                call(
                    "/tmp/repo",
                    ["diff", "--no-ext-diff"],
                    silent=True,
                    max_output_chars=4096,
                ),
                call(
                    "/tmp/repo",
                    ["diff", "--name-only"],
                    silent=True,
                    max_output_chars=None,
                ),
            ]
        )

    def test_collect_staged_context_uses_cached_diff(self) -> None:
        diff_result = completed(["git", "diff", "--cached"], "diff --git a/app.py b/app.py\n")
        files_result = completed(["git", "diff", "--cached", "--name-only"], "app.py\n")

        with patch("core.review.git_command", side_effect=[diff_result, files_result]) as git_command:
            context = collect_review_context("/tmp/repo", REVIEW_TARGET_STAGED)

        self.assertEqual(context.target, REVIEW_TARGET_STAGED)
        self.assertEqual(context.label, "staged changes")
        self.assertEqual(context.files, ["app.py"])
        git_command.assert_any_call(
            "/tmp/repo",
            ["diff", "--cached", "--no-ext-diff"],
            silent=True,
            max_output_chars=DEFAULT_REVIEW_DIFF_MAX_CHARS,
        )
        git_command.assert_any_call(
            "/tmp/repo",
            ["diff", "--cached", "--name-only"],
            silent=True,
            max_output_chars=None,
        )

    def test_collect_branch_context_uses_branch_range(self) -> None:
        diff_result = completed(["git", "diff", "main...HEAD"], "diff --git a/app.py b/app.py\n")
        files_result = completed(["git", "diff", "--name-only", "main...HEAD"], "app.py\n")

        with patch("core.review.git_command", side_effect=[diff_result, files_result]) as git_command:
            context = collect_review_context("/tmp/repo", REVIEW_TARGET_BRANCH, ref=" main ")

        self.assertEqual(context.target, REVIEW_TARGET_BRANCH)
        self.assertEqual(context.ref, "main")
        self.assertEqual(context.label, "diff against main...HEAD")
        git_command.assert_any_call(
            "/tmp/repo",
            ["diff", "--no-ext-diff", "main...HEAD"],
            silent=True,
            max_output_chars=DEFAULT_REVIEW_DIFF_MAX_CHARS,
        )
        git_command.assert_any_call(
            "/tmp/repo",
            ["diff", "--name-only", "main...HEAD"],
            silent=True,
            max_output_chars=None,
        )

    def test_collect_commit_context_uses_git_show(self) -> None:
        show_result = completed(["git", "show", "HEAD~1"], "commit abc123\n\ndiff --git a/app.py b/app.py\n")
        files_result = completed(["git", "show", "--name-only", "HEAD~1"], "\napp.py\n\n")

        with patch("core.review.git_command", side_effect=[show_result, files_result]) as git_command:
            context = collect_review_context("/tmp/repo", REVIEW_TARGET_COMMIT, ref="HEAD~1")

        self.assertEqual(context.target, REVIEW_TARGET_COMMIT)
        self.assertEqual(context.ref, "HEAD~1")
        self.assertEqual(context.label, "commit HEAD~1")
        self.assertEqual(context.files, ["app.py"])
        git_command.assert_any_call(
            "/tmp/repo",
            ["show", "--stat", "--patch", "--no-ext-diff", "HEAD~1"],
            silent=True,
            max_output_chars=DEFAULT_REVIEW_DIFF_MAX_CHARS,
        )
        git_command.assert_any_call(
            "/tmp/repo",
            ["show", "--name-only", "--format=", "HEAD~1"],
            silent=True,
            max_output_chars=None,
        )

    def test_collect_review_context_rejects_missing_ref_for_branch_and_commit(self) -> None:
        with self.assertRaises(ReviewContextError):
            collect_review_context("/tmp/repo", REVIEW_TARGET_BRANCH)

        with self.assertRaises(ReviewContextError):
            collect_review_context("/tmp/repo", REVIEW_TARGET_COMMIT, ref=" ")

    def test_collect_review_context_rejects_unknown_target(self) -> None:
        with self.assertRaises(ReviewContextError):
            collect_review_context("/tmp/repo", "unknown")

    def test_collect_review_context_raises_clear_error_on_git_failure(self) -> None:
        failed = completed(["git", "diff"], returncode=128, stderr="fatal: bad revision 'main...HEAD'\n")

        with patch("core.review.git_command", return_value=failed):
            with self.assertRaisesRegex(ReviewContextError, "bad revision"):
                collect_review_context("/tmp/repo", REVIEW_TARGET_BRANCH, ref="main")

    def test_collect_review_context_allows_empty_diff(self) -> None:
        with patch(
            "core.review.git_command",
            side_effect=[
                completed(["git", "diff"], ""),
                completed(["git", "diff", "--name-only"], ""),
            ],
        ):
            context = collect_review_context("/tmp/repo", REVIEW_TARGET_WORKTREE)

        self.assertEqual(context.diff, "")
        self.assertEqual(context.files, [])
        self.assertFalse(context.has_changes)


if __name__ == "__main__":
    unittest.main()
