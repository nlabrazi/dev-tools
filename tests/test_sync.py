import subprocess
import unittest
from unittest.mock import patch

from core.sync import (
    checkout_branch,
    describe_sync_plan,
    ensure_local_branch_exists,
    fetch,
    get_ahead_behind,
    main,
    pull_ff_only,
    repo_is_clean,
    sync_all_repos,
    sync_default_branch,
)


class SyncDryRunTests(unittest.TestCase):
    def test_fetch_returns_true_without_git_fetch_in_dry_run(self) -> None:
        with patch("core.sync.is_dry_run", return_value=True), patch("core.sync.git_command") as git_command, patch(
            "core.sync.console.print"
        ):
            ok = fetch("/tmp/repo", "repo")

        self.assertTrue(ok)
        git_command.assert_not_called()
    def test_ensure_local_branch_exists_returns_dry_run_when_branch_is_missing(self) -> None:
        missing_branch = subprocess.CompletedProcess(
            args=["show-ref", "--verify", "--quiet", "refs/heads/main"],
            returncode=1,
            stdout="",
            stderr="",
        )
        with patch("core.sync.git_command", return_value=missing_branch) as git_command, patch(
            "core.sync.is_dry_run",
            return_value=True,
        ), patch("core.sync.console.print"):
            status = ensure_local_branch_exists("/tmp/repo", "repo", "main")

        self.assertEqual(status, "dry-run")
        git_command.assert_called_once_with(
            "/tmp/repo",
            ["show-ref", "--verify", "--quiet", "refs/heads/main"],
            silent=True,
        )

    def test_pull_ff_only_returns_dry_run_without_git_pull(self) -> None:
        with patch("core.sync.is_dry_run", return_value=True), patch("core.sync.git_command") as git_command, patch(
            "core.sync.console.print"
        ):
            status = pull_ff_only("/tmp/repo", "repo", "main")

        self.assertEqual(status, "dry-run")
        git_command.assert_not_called()

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


class SyncWorkflowTests(unittest.TestCase):
    def test_repo_is_clean_checks_porcelain_output(self) -> None:
        clean = subprocess.CompletedProcess(args=["status"], returncode=0, stdout="  \n", stderr="")
        dirty = subprocess.CompletedProcess(args=["status"], returncode=0, stdout=" M run.py\n", stderr="")

        with patch("core.sync.git_command", side_effect=[clean, dirty]):
            self.assertTrue(repo_is_clean("/tmp/repo"))
            self.assertFalse(repo_is_clean("/tmp/repo"))

    def test_fetch_handles_success_and_failure_in_prod(self) -> None:
        ok_res = subprocess.CompletedProcess(args=["fetch"], returncode=0, stdout="", stderr="")
        err_res = subprocess.CompletedProcess(args=["fetch"], returncode=1, stdout="", stderr="network error")

        with patch("core.sync.is_dry_run", return_value=False), patch(
            "core.sync.git_command", side_effect=[ok_res, err_res]
        ), patch("core.sync.console.print") as console_print:
            self.assertTrue(fetch("/tmp/repo", "repo"))
            self.assertFalse(fetch("/tmp/repo", "repo"))

        console_print.assert_called_once()
        self.assertIn("network error", console_print.call_args.args[0])

    def test_ensure_local_branch_exists_handles_existing_created_and_failed(self) -> None:
        exists = subprocess.CompletedProcess(args=["show-ref"], returncode=0, stdout="", stderr="")
        missing = subprocess.CompletedProcess(args=["show-ref"], returncode=1, stdout="", stderr="")
        created = subprocess.CompletedProcess(args=["checkout"], returncode=0, stdout="", stderr="")
        failed = subprocess.CompletedProcess(args=["checkout"], returncode=1, stdout="", stderr="err")

        with patch("core.sync.is_dry_run", return_value=False), patch(
            "core.sync.git_command", side_effect=[exists, missing, created, missing, failed]
        ):
            self.assertEqual(ensure_local_branch_exists("/tmp/repo", "repo", "main"), "ready")
            self.assertEqual(ensure_local_branch_exists("/tmp/repo", "repo", "main"), "ready")
            self.assertEqual(ensure_local_branch_exists("/tmp/repo", "repo", "main"), "failed")

    def test_checkout_branch_handles_current_dry_run_success_and_failure(self) -> None:
        with patch("core.sync.git_output", return_value="main"):
            self.assertEqual(checkout_branch("/tmp/repo", "repo", "main"), "ready")

        with patch("core.sync.git_output", return_value="staging"), patch(
            "core.sync.is_dry_run", return_value=True
        ), patch("core.sync.console.print"):
            self.assertEqual(checkout_branch("/tmp/repo", "repo", "main"), "dry-run")

        ok_res = subprocess.CompletedProcess(args=["checkout"], returncode=0, stdout="", stderr="")
        err_res = subprocess.CompletedProcess(args=["checkout"], returncode=1, stdout="", stderr="conflict")
        with patch("core.sync.git_output", return_value="staging"), patch(
            "core.sync.is_dry_run", return_value=False
        ), patch("core.sync.git_command", side_effect=[ok_res, err_res]), patch("core.sync.console.print"):
            self.assertEqual(checkout_branch("/tmp/repo", "repo", "main"), "ready")
            self.assertEqual(checkout_branch("/tmp/repo", "repo", "main"), "failed")

    def test_get_ahead_behind_parses_counts_and_rejects_malformed_output(self) -> None:
        with patch("core.sync.git_output", side_effect=["2\t5", "", "onlyone", "a b"]):
            self.assertEqual(get_ahead_behind("/tmp/repo", "main"), (2, 5))
            self.assertIsNone(get_ahead_behind("/tmp/repo", "main"))
            self.assertIsNone(get_ahead_behind("/tmp/repo", "main"))
            self.assertIsNone(get_ahead_behind("/tmp/repo", "main"))

    def test_pull_ff_only_handles_success_and_failure_in_prod(self) -> None:
        ok_res = subprocess.CompletedProcess(args=["pull"], returncode=0, stdout="", stderr="")
        err_res = subprocess.CompletedProcess(args=["pull"], returncode=1, stdout="", stderr="non-ff")

        with patch("core.sync.is_dry_run", return_value=False), patch(
            "core.sync.git_command", side_effect=[ok_res, err_res]
        ), patch("core.sync.console.print") as console_print:
            self.assertEqual(pull_ff_only("/tmp/repo", "repo", "main"), "pulled")
            self.assertEqual(pull_ff_only("/tmp/repo", "repo", "main"), "failed")

        console_print.assert_called_once()
        self.assertIn("non-ff", console_print.call_args.args[0])

    def test_sync_default_branch_early_exit_guards(self) -> None:
        with patch("core.sync.repo_is_clean", return_value=False), patch("core.sync.fetch") as mock_fetch, patch(
            "core.sync.console.print"
        ) as console_print:
            sync_default_branch("/tmp/repo", "repo")
        mock_fetch.assert_not_called()
        self.assertIn("not clean", console_print.call_args.args[0])

        with patch("core.sync.repo_is_clean", return_value=True), patch(
            "core.sync.fetch", return_value=False
        ), patch("core.sync.resolve_repo_base_branch") as resolve_mock:
            sync_default_branch("/tmp/repo", "repo")
        resolve_mock.assert_not_called()

        for branch_status in ("failed", "dry-run"):
            with patch("core.sync.repo_is_clean", return_value=True), patch(
                "core.sync.fetch", return_value=True
            ), patch("core.sync.resolve_repo_base_branch", return_value=("main", "ok")), patch(
                "core.sync.ensure_local_branch_exists", return_value=branch_status
            ), patch("core.sync.checkout_branch") as checkout_mock, patch("core.sync.console.print"):
                sync_default_branch("/tmp/repo", "repo")
            checkout_mock.assert_not_called()

        with patch("core.sync.repo_is_clean", return_value=True), patch(
            "core.sync.fetch", return_value=True
        ), patch("core.sync.resolve_repo_base_branch", return_value=("main", "ok")), patch(
            "core.sync.ensure_local_branch_exists", return_value="ready"
        ), patch("core.sync.checkout_branch", return_value="failed"), patch(
            "core.sync.get_ahead_behind"
        ) as counts_mock:
            sync_default_branch("/tmp/repo", "repo")
        counts_mock.assert_not_called()

    def test_sync_default_branch_handles_counts_and_pull_confirmation(self) -> None:
        base_patches = [
            patch("core.sync.repo_is_clean", return_value=True),
            patch("core.sync.fetch", return_value=True),
            patch("core.sync.resolve_repo_base_branch", return_value=("main", "ok")),
            patch("core.sync.ensure_local_branch_exists", return_value="ready"),
            patch("core.sync.checkout_branch", return_value="ready"),
        ]

        # None counts, up-to-date, ahead-only, diverged
        for counts, expected_fragment in [
            (None, "cannot compute ahead/behind"),
            ((0, 0), "up-to-date"),
            ((2, 0), "is ahead of"),
            ((1, 3), "diverged"),
        ]:
            with base_patches[0], base_patches[1], base_patches[2], base_patches[3], base_patches[4], patch(
                "core.sync.get_ahead_behind", return_value=counts
            ), patch("core.sync.git_output", return_value="abc1234"), patch(
                "core.sync.ask_yes_no"
            ) as ask_mock, patch("core.sync.console.print") as console_print:
                sync_default_branch("/tmp/repo", "repo")
            ask_mock.assert_not_called()
            self.assertIn(expected_fragment, console_print.call_args.args[0])

        # Behind, user declines pull
        with base_patches[0], base_patches[1], base_patches[2], base_patches[3], base_patches[4], patch(
            "core.sync.get_ahead_behind", return_value=(0, 4)
        ), patch("core.sync.ask_yes_no", return_value=False), patch(
            "core.sync.pull_ff_only"
        ) as pull_mock, patch("core.sync.console.print") as console_print:
            sync_default_branch("/tmp/repo", "repo")
        pull_mock.assert_not_called()
        self.assertIn("skipped pull", console_print.call_args.args[0])

        # Behind, user accepts pull
        with base_patches[0], base_patches[1], base_patches[2], base_patches[3], base_patches[4], patch(
            "core.sync.get_ahead_behind", return_value=(0, 4)
        ), patch("core.sync.ask_yes_no", return_value=True), patch(
            "core.sync.pull_ff_only", return_value="pulled"
        ) as pull_mock, patch("core.sync.git_output", return_value="def5678"), patch(
            "core.sync.console.print"
        ) as console_print:
            sync_default_branch("/tmp/repo", "repo")
        pull_mock.assert_called_once_with("/tmp/repo", "repo", "main")
        self.assertIn("pulled main (HEAD def5678)", console_print.call_args.args[0])

    def test_sync_all_repos_and_main_handle_missing_empty_and_populated_roots(self) -> None:
        with patch("core.sync.os.path.isdir", side_effect=[False, True, True]), patch(
            "core.sync.iter_git_repositories",
            side_effect=[[], [("repo-a", "/roots/ok/repo-a")]],
        ), patch("core.sync.sync_default_branch") as sync_mock, patch(
            "core.sync.console.print"
        ) as console_print:
            sync_all_repos(["/roots/missing", "/roots/empty", "/roots/ok"])

        sync_mock.assert_called_once_with("/roots/ok/repo-a", "repo-a")
        messages = [call.args[0] for call in console_print.call_args_list]
        self.assertTrue(any("Root directory not found" in msg for msg in messages))
        self.assertTrue(any("No repositories found" in msg for msg in messages))

        with patch("core.sync.sync_all_repos") as sync_all_mock:
            main(["/roots/custom"])
        sync_all_mock.assert_called_once_with(["/roots/custom"])


if __name__ == "__main__":
    unittest.main()
