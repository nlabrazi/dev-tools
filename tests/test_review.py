import subprocess
import unittest
from unittest.mock import call, patch

from core.review import (
    DEFAULT_REVIEW_DIFF_MAX_CHARS,
    REVIEW_TARGET_BRANCH,
    REVIEW_TARGET_COMMIT,
    REVIEW_TARGET_STAGED,
    REVIEW_TARGET_WORKTREE,
    ReviewContext,
    ReviewContextError,
    ReviewCommentFormatError,
    build_review_comment_plan,
    collect_review_context,
    generate_review_comments,
    generate_review_comments_with_ollama,
    get_review_diff_max_chars,
    is_commentable_source_file,
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


class ReviewCommentGenerationTests(unittest.TestCase):
    def test_is_commentable_source_file_filters_sensitive_and_binary_files(self) -> None:
        self.assertTrue(is_commentable_source_file("src/app.ts"))
        self.assertTrue(is_commentable_source_file("components/Card.vue"))
        self.assertFalse(is_commentable_source_file(".env"))
        self.assertFalse(is_commentable_source_file("package-lock.json"))
        self.assertFalse(is_commentable_source_file("public/logo.png"))
        self.assertFalse(is_commentable_source_file("README.md"))

    def test_build_review_comment_plan_filters_invalid_and_out_of_scope_comments(self) -> None:
        data = {
            "review": {
                "summary": "Clarify non-obvious validation behavior.",
                "comments": [
                    {
                        "file": "src/app.ts",
                        "anchor": "function buildPayload(input) {",
                        "placement": "after",
                        "comment": "// Normalization must happen before signing the payload.",
                        "reason": "Clarifies order-sensitive behavior.",
                    },
                    {
                        "file": "package-lock.json",
                        "anchor": "lockfileVersion",
                        "placement": "before",
                        "comment": "// Do not add comments here.",
                        "reason": "Lockfile must be ignored.",
                    },
                    {
                        "file": "src/other.ts",
                        "anchor": "const other = true",
                        "placement": "before",
                        "comment": "// Out of scope.",
                        "reason": "Not in changed files.",
                    },
                    {
                        "file": "src/app.ts",
                        "anchor": "",
                        "placement": "before",
                        "comment": "// Missing anchor.",
                        "reason": "Invalid.",
                    },
                ],
            }
        }

        plan = build_review_comment_plan(data, allowed_files=["src/app.ts"])

        self.assertEqual(plan.summary, "Clarify non-obvious validation behavior.")
        self.assertTrue(plan.has_comments)
        self.assertEqual(len(plan.comments), 1)
        self.assertEqual(plan.comments[0].file, "src/app.ts")
        self.assertEqual(plan.comments[0].placement, "after")

    def test_build_review_comment_plan_rejects_missing_review_object(self) -> None:
        with self.assertRaises(ReviewCommentFormatError):
            build_review_comment_plan({"comments": []})

    def test_generate_review_comments_with_ollama_builds_from_valid_json(self) -> None:
        raw = """
        {
          "review": {
            "summary": "Clarify payload normalization.",
            "comments": [
              {
                "file": "src/app.ts",
                "anchor": "function buildPayload(input) {",
                "placement": "before",
                "comment": "// Normalize before signing so retries produce the same payload.",
                "reason": "Explains an order-sensitive constraint."
              }
            ]
          }
        }
        """
        context = ReviewContext(
            repo_path="/tmp/repo",
            target=REVIEW_TARGET_WORKTREE,
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git a/src/app.ts b/src/app.ts\n+function buildPayload(input) {\n",
        )

        with patch.dict(
            "os.environ",
            {"OLLAMA_HOST": "http://localhost:11434"},
            clear=True,
        ), patch("core.review.chat_json", return_value=raw) as chat_json, patch("core.review.print"):
            plan = generate_review_comments_with_ollama("repo", context)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.summary, "Clarify payload normalization.")
        self.assertEqual(len(plan.comments), 1)
        self.assertEqual(plan.comments[0].comment, "// Normalize before signing so retries produce the same payload.")
        chat_json.assert_called_once()

    def test_generate_review_comments_with_ollama_returns_none_when_disabled(self) -> None:
        context = ReviewContext(
            repo_path="/tmp/repo",
            target=REVIEW_TARGET_WORKTREE,
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )

        with patch.dict("os.environ", {"ENABLE_OLLAMA": "0"}, clear=True), patch(
            "core.review.chat_json"
        ) as chat_json:
            plan = generate_review_comments_with_ollama("repo", context)

        self.assertIsNone(plan)
        chat_json.assert_not_called()

    def test_generate_review_comments_with_ollama_skips_remote_context_without_opt_in(self) -> None:
        context = ReviewContext(
            repo_path="/tmp/repo",
            target=REVIEW_TARGET_WORKTREE,
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )

        with patch.dict(
            "os.environ",
            {
                "OLLAMA_HOST": "http://example.com:11434",
                "OLLAMA_ALLOW_REMOTE": "1",
            },
            clear=True,
        ), patch("core.review.chat_json") as chat_json, patch("core.review.print"):
            plan = generate_review_comments_with_ollama("repo", context)

        self.assertIsNone(plan)
        chat_json.assert_not_called()

    def test_generate_review_comments_with_ollama_retries_invalid_json_once(self) -> None:
        invalid = "not json"
        valid_retry = """
        {
          "review": {
            "summary": "Retry succeeded.",
            "comments": []
          }
        }
        """
        context = ReviewContext(
            repo_path="/tmp/repo",
            target=REVIEW_TARGET_WORKTREE,
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git a/src/app.ts b/src/app.ts\n+const enabled = true\n",
        )

        with patch.dict(
            "os.environ",
            {"OLLAMA_HOST": "http://localhost:11434"},
            clear=True,
        ), patch("core.review.chat_json", side_effect=[invalid, valid_retry]) as chat_json, patch("core.review.print"):
            plan = generate_review_comments_with_ollama("repo", context)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.summary, "Retry succeeded.")
        self.assertEqual(chat_json.call_count, 2)

    def test_generate_review_comments_with_ollama_does_not_call_ollama_for_empty_diff(self) -> None:
        context = ReviewContext(
            repo_path="/tmp/repo",
            target=REVIEW_TARGET_WORKTREE,
            label="worktree changes",
            files=[],
            diff="",
        )

        with patch.dict(
            "os.environ",
            {"OLLAMA_HOST": "http://localhost:11434"},
            clear=True,
        ), patch("core.review.chat_json") as chat_json:
            plan = generate_review_comments_with_ollama("repo", context)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertFalse(plan.has_comments)
        self.assertIn("No changes found", plan.summary)
        chat_json.assert_not_called()

    def test_generate_review_comments_returns_empty_fallback_when_ollama_fails(self) -> None:
        context = ReviewContext(
            repo_path="/tmp/repo",
            target=REVIEW_TARGET_WORKTREE,
            label="worktree changes",
            files=["src/app.ts"],
            diff="diff --git",
        )

        with patch("core.review.generate_review_comments_with_ollama", return_value=None):
            plan = generate_review_comments("repo", context)

        self.assertEqual(plan.summary, "No AI code comments generated.")
        self.assertFalse(plan.has_comments)


if __name__ == "__main__":
    unittest.main()
