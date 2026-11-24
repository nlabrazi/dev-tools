import subprocess
import unittest
from unittest.mock import patch

from core.merge import (
    checkout_update_base_branch,
    create_and_merge_pr,
    ensure_clean_worktree,
    existing_pr_number,
    get_commit_summary,
    get_current_branch,
    get_pr_number_from_url,
    get_pr_status,
    main,
    merge_pr_with_retry,
    repo_has_branch_diff,
    resolve_merge_base_branch,
    sync_head_branch_after_merge,
    tag_release_interactive,
    wait_for_pr_merge,
)


class MergeSelectionTests(unittest.TestCase):
    def test_merge_checks_and_processes_only_the_selected_repository(self) -> None:
        with patch("core.merge.os.path.isdir", return_value=True), patch(
            "core.merge.iter_git_repositories",
            return_value=[("repo", "/tmp/first/repo"), ("repo", "/tmp/second/repo")],
        ), patch(
            "core.merge.resolve_merge_base_branch", return_value=("main", "resolved")
        ) as resolve, patch("core.merge.repo_has_branch_diff", return_value=True) as has_diff, patch(
            "core.merge.create_and_merge_pr"
        ) as merge, patch("core.merge.console.print"), patch("core.merge.print"):
            main(["/tmp/root"], selected_repo_path="/tmp/second/repo")

        resolve.assert_called_once_with("/tmp/second/repo")
        has_diff.assert_called_once_with("/tmp/second/repo", "main")
        merge.assert_called_once_with("/tmp/second/repo", "repo", "main")


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
            "core.merge.ask_yes_no",
            return_value=True,
        ) as ask_yes_no, patch(
            "core.merge.is_dry_run",
            return_value=True,
        ), patch(
            "core.merge.resolve_merge_base_branch",
            return_value=("main", "resolved from origin/HEAD"),
        ), patch("core.merge.merge_pr_with_retry") as merge_pr_with_retry, patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo")

        ask_yes_no.assert_called_once_with(
            "🚀 Do you want to enable auto-merge for existing PR #42?",
            default="n",
        )
        merge_pr_with_retry.assert_not_called()

    def test_create_and_merge_pr_existing_pr_requires_confirmation_before_merge(self) -> None:
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
            return_value="42",
        ), patch(
            "core.merge.ask_yes_no",
            return_value=False,
        ) as ask_yes_no, patch(
            "core.merge.is_dry_run",
            return_value=False,
        ), patch("core.merge.merge_pr_with_retry") as merge_pr_with_retry, patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo")

        ask_yes_no.assert_called_once_with(
            "🚀 Do you want to enable auto-merge for existing PR #42?",
            default="n",
        )
        merge_pr_with_retry.assert_not_called()

    def test_resolve_merge_base_branch_skips_fetch_in_dry_run(self) -> None:
        with patch("core.merge.is_dry_run", return_value=True), patch(
            "core.merge.git_command"
        ) as git_command, patch(
            "core.merge.resolve_repo_base_branch",
            return_value=("main", "resolved"),
        ):
            branch, reason = resolve_merge_base_branch("/tmp/repo")

        self.assertEqual((branch, reason), ("main", "resolved"))
        git_command.assert_not_called()

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


class MergeWorkflowTests(unittest.TestCase):
    def test_worktree_and_git_helpers(self) -> None:
        with patch("core.merge.git_output", return_value="staging"):
            self.assertEqual(get_current_branch("/tmp/repo"), "staging")

        with patch("core.merge.git_output", side_effect=[" M app.py"]):
            with self.assertRaisesRegex(RuntimeError, "not clean"):
                ensure_clean_worktree("/tmp/repo")

        with patch("core.merge.git_output", side_effect=["", ".git/MERGE_HEAD"]), patch(
            "core.merge.os.path.exists", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "Merge in progress"):
                ensure_clean_worktree("/tmp/repo")

        with patch("core.merge.git_output", side_effect=["", ""]), patch(
            "core.merge.os.path.exists", return_value=False
        ):
            ensure_clean_worktree("/tmp/repo")

        with patch("core.merge.git_command_checked") as checked:
            checkout_update_base_branch("/tmp/repo", "main")
        self.assertEqual(checked.call_count, 4)

        with patch("core.merge.is_dry_run", return_value=False), patch(
            "core.merge.git_command"
        ) as git_cmd, patch(
            "core.merge.resolve_repo_base_branch", return_value=("main", "resolved")
        ):
            self.assertEqual(resolve_merge_base_branch("/tmp/repo"), ("main", "resolved"))
        git_cmd.assert_called_once()

        with patch("core.merge.git_output", side_effect=["aaa", "bbb", "aaa", "aaa", "", "bbb"]):
            self.assertTrue(repo_has_branch_diff("/tmp/repo", "main"))
            self.assertFalse(repo_has_branch_diff("/tmp/repo", "main"))
            self.assertFalse(repo_has_branch_diff("/tmp/repo", "main"))

        with patch("core.merge.git_output", return_value="- feat: add tests"):
            self.assertEqual(get_commit_summary("/tmp/repo", "main"), "- feat: add tests")

    def test_gh_cli_helpers(self) -> None:
        ok_num = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=" 42 \n", stderr="")
        fail_res = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="err")
        list_json = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")

        with patch("core.merge.run_command", side_effect=[ok_num, ok_num, fail_res, list_json]):
            self.assertEqual(existing_pr_number("/tmp/repo", "main"), "42")
            self.assertEqual(get_pr_number_from_url("/tmp/repo", "https://github.com/o/r/pull/42"), "42")
            self.assertIsNone(get_pr_status("/tmp/repo", "42"))
            self.assertIsNone(get_pr_status("/tmp/repo", "42"))

    def test_merge_pr_with_retry_handles_success_transient_non_transient_and_exhaustion(self) -> None:
        ok = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="", stderr="")
        transient = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="PR is not mergeable yet"
        )
        fatal = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="Permission denied"
        )

        with patch("core.merge.run_command", side_effect=[transient, ok]), patch(
            "core.merge.time.sleep"
        ) as sleep_mock, patch("core.merge.print"):
            self.assertTrue(merge_pr_with_retry("/tmp/repo", "repo", "42", max_attempts=3))
        sleep_mock.assert_called_once_with(2)

        with patch("core.merge.run_command", return_value=fatal), patch(
            "core.merge.time.sleep"
        ) as sleep_mock, patch("core.merge.print"):
            self.assertFalse(merge_pr_with_retry("/tmp/repo", "repo", "42", max_attempts=3))
        sleep_mock.assert_not_called()

        with patch("core.merge.run_command", return_value=transient), patch(
            "core.merge.time.sleep"
        ) as sleep_mock, patch("core.merge.print"):
            self.assertFalse(merge_pr_with_retry("/tmp/repo", "repo", "42", max_attempts=2))
        self.assertEqual(sleep_mock.call_count, 2)

    def test_wait_for_pr_merge_handles_merged_closed_and_timeout(self) -> None:
        with patch("core.merge.time.time", side_effect=[0, 1]), patch(
            "core.merge.get_pr_status", return_value={"state": "MERGED"}
        ):
            self.assertTrue(wait_for_pr_merge("/tmp/repo", "repo", "42", timeout_seconds=10))

        with patch("core.merge.time.time", side_effect=[0, 1]), patch(
            "core.merge.get_pr_status", return_value={"state": "CLOSED"}
        ), patch("core.merge.print"):
            self.assertFalse(wait_for_pr_merge("/tmp/repo", "repo", "42", timeout_seconds=10))

        with patch("core.merge.time.time", side_effect=[0, 1, 20]), patch(
            "core.merge.get_pr_status", return_value={"state": "OPEN"}
        ), patch("core.merge.time.sleep") as sleep_mock, patch("core.merge.print"):
            self.assertFalse(wait_for_pr_merge("/tmp/repo", "repo", "42", timeout_seconds=10))
        sleep_mock.assert_called_once_with(5)

    def test_tag_release_interactive_skips_or_creates_tag(self) -> None:
        with patch("core.merge.get_last_semver_tag", return_value="v1.0.0"), patch(
            "core.merge.determine_bump_from_commits", return_value="minor"
        ), patch("core.merge.compute_next_version", side_effect=["v1.1.0", "v1.1.0"]), patch(
            "builtins.input", return_value=""
        ), patch("core.merge.ask_yes_no", return_value=False), patch(
            "core.merge.create_and_push_tag"
        ) as tag_mock, patch("core.merge.print"):
            tag_release_interactive("/tmp/repo", "repo", "- feat: item")
        tag_mock.assert_not_called()

        with patch("core.merge.get_last_semver_tag", return_value=None), patch(
            "core.merge.determine_bump_from_commits", return_value="patch"
        ), patch("core.merge.compute_next_version", side_effect=["v0.1.0", "v1.0.0"]), patch(
            "builtins.input", return_value="major"
        ), patch("core.merge.ask_yes_no", return_value=True), patch(
            "core.merge.create_and_push_tag"
        ) as tag_mock, patch("core.merge.print"):
            tag_release_interactive("/tmp/repo", "repo", "- feat!: breaking")
        tag_mock.assert_called_once_with("/tmp/repo", "v1.0.0", message="Release v1.0.0")

    def test_create_and_merge_pr_error_paths_and_full_release_flow(self) -> None:
        with patch("core.merge.ensure_clean_worktree", side_effect=RuntimeError("dirty")), patch(
            "core.merge.print"
        ) as print_mock:
            create_and_merge_pr("/tmp/repo", "repo")
        self.assertIn("dirty", print_mock.call_args.args[0])

        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.resolve_merge_base_branch", return_value=(None, "no base")
        ), patch("core.merge.print") as print_mock:
            create_and_merge_pr("/tmp/repo", "repo")
        self.assertIn("no base", print_mock.call_args.args[0])

        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.get_current_branch", return_value="staging"
        ), patch("core.merge.get_commit_summary", return_value=""), patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo", base_branch="main")

        # Declined new PR, failed create, missing URL, unresolved number
        failed_create = subprocess.CompletedProcess(args=["gh"], returncode=1, stdout="", stderr="denied")
        no_url = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="created\n", stderr="")
        ok_create = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="https://github.com/o/r/pull/9\n", stderr=""
        )

        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.get_current_branch", return_value="staging"
        ), patch("core.merge.get_commit_summary", return_value="- fix: bug"), patch(
            "core.merge.generate_pr_text", return_value=("Title", "Body")
        ), patch("core.merge.existing_pr_number", return_value=""), patch(
            "core.merge.ask_yes_no", return_value=False
        ), patch("core.merge.run_command") as run_cmd, patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo", base_branch="main")
        run_cmd.assert_not_called()

        for create_res, pr_num in [(failed_create, ""), (no_url, ""), (ok_create, "")]:
            with patch("core.merge.ensure_clean_worktree"), patch(
                "core.merge.get_current_branch", return_value="staging"
            ), patch("core.merge.get_commit_summary", return_value="- fix: bug"), patch(
                "core.merge.generate_pr_text", return_value=("Title", "Body")
            ), patch("core.merge.existing_pr_number", return_value=""), patch(
                "core.merge.ask_yes_no", return_value=True
            ), patch("core.merge.is_dry_run", return_value=False), patch(
                "core.merge.run_command", return_value=create_res
            ), patch("core.merge.get_pr_number_from_url", return_value=pr_num), patch(
                "core.merge.merge_pr_with_retry"
            ) as merge_mock, patch("core.merge.console.status"), patch("core.merge.print"):
                create_and_merge_pr("/tmp/repo", "repo", base_branch="main")
            merge_mock.assert_not_called()

        # Merge succeeds, wait times out -> no checkout/tagging
        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.get_current_branch", return_value="staging"
        ), patch("core.merge.get_commit_summary", return_value="- fix: bug"), patch(
            "core.merge.generate_pr_text", return_value=("Title", "Body")
        ), patch("core.merge.existing_pr_number", return_value="9"), patch(
            "core.merge.ask_yes_no", return_value=True
        ), patch("core.merge.is_dry_run", return_value=False), patch(
            "core.merge.merge_pr_with_retry", return_value=True
        ), patch("core.merge.wait_for_pr_merge", return_value=False), patch(
            "core.merge.checkout_update_base_branch"
        ) as checkout_mock, patch("core.merge.console.status"), patch("core.merge.print"):
            create_and_merge_pr("/tmp/repo", "repo", base_branch="main")
        checkout_mock.assert_not_called()

        # Full flow including tagging exception fallback & post-merge staging auto-sync
        with patch("core.merge.ensure_clean_worktree"), patch(
            "core.merge.get_current_branch", return_value="staging"
        ), patch("core.merge.get_commit_summary", return_value="- feat: ship"), patch(
            "core.merge.generate_pr_text", return_value=("Title", "Body")
        ), patch("core.merge.existing_pr_number", return_value="9"), patch(
            "core.merge.ask_yes_no", return_value=True
        ), patch("core.merge.is_dry_run", return_value=False), patch(
            "core.merge.merge_pr_with_retry", return_value=True
        ), patch("core.merge.wait_for_pr_merge", return_value=True), patch(
            "core.merge.checkout_update_base_branch"
        ) as checkout_mock, patch(
            "core.merge.tag_release_interactive", side_effect=RuntimeError("tag boom")
        ) as tag_mock, patch(
            "core.merge.sync_head_branch_after_merge", return_value=True
        ) as sync_mock, patch("core.merge.console.status"), patch("core.merge.print") as print_mock:
            create_and_merge_pr("/tmp/repo", "repo", base_branch="main")
        checkout_mock.assert_called_once_with("/tmp/repo", "main")
        tag_mock.assert_called_once_with("/tmp/repo", "repo", "- feat: ship")
        sync_mock.assert_called_once_with("/tmp/repo", "repo", "main")
        self.assertTrue(any("tag boom" in str(c.args[0]) for c in print_mock.call_args_list if c.args))

    def test_sync_head_branch_after_merge_handles_ff_dry_run_and_noop(self) -> None:
        with patch("core.merge.is_dry_run", return_value=True), patch(
            "core.merge.git_command_checked"
        ) as checked, patch("core.merge.print"):
            self.assertTrue(sync_head_branch_after_merge("/tmp/repo", "repo", "master"))
        checked.assert_not_called()

        with patch("core.merge.is_dry_run", return_value=False), patch(
            "core.merge.git_command_checked"
        ) as checked, patch("core.merge.print"):
            self.assertTrue(sync_head_branch_after_merge("/tmp/repo", "repo", "master"))
        self.assertEqual(
            [c.args[1] for c in checked.call_args_list],
            [
                ["checkout", "staging"],
                ["merge", "--ff-only", "origin/master"],
                ["push", "origin", "staging"],
            ],
        )

        # Same branch -> immediate no-op
        with patch("core.merge.git_command_checked") as checked:
            self.assertTrue(sync_head_branch_after_merge("/tmp/repo", "repo", "staging"))
        checked.assert_not_called()

    def test_main_handles_missing_empty_unresolved_and_up_to_date_repos(self) -> None:
        with patch("core.merge.os.path.isdir", side_effect=[False, True, True]), patch(
            "core.merge.iter_git_repositories",
            side_effect=[[], [("repo-a", "/r/a"), ("repo-b", "/r/b")]],
        ), patch(
            "core.merge.resolve_merge_base_branch",
            side_effect=[(None, "no origin/HEAD"), ("main", "ok")],
        ), patch("core.merge.repo_has_branch_diff", return_value=False), patch(
            "core.merge.create_and_merge_pr"
        ) as create_mock, patch("core.merge.console.print"), patch("core.merge.print") as print_mock:
            main(["/missing", "/empty", "/ok"])

        create_mock.assert_not_called()
        output = " ".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Root directory not found", output)
        self.assertIn("No repositories found", output)
        self.assertIn("up to date", output)


if __name__ == "__main__":
    unittest.main()
