import io
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from rich.console import Console

from core.maintenance.models import (
    DRIFT_PROBLEMATIC,
    RISK_BREAKING,
    AuditReport,
    CIStatusSummary,
    DependencyDashboardInfo,
    EnvironmentMatrixRow,
    GitStateInfo,
    ProductionEnvironmentInfo,
    RenovateConfigInfo,
)
from core.maintenance.workflow import (
    _audit_with_progress,
    _compute_overall_status,
    _is_safe_direct_update,
    _prompt_update_selection,
    ask_maintenance_action,
    audit_repository,
    render_maintenance_menu,
    run_dependency_maintenance_for_all_repos,
    run_dependency_maintenance_for_repo,
    run_guided_pr_bump_workflow,
    run_ruby_gem_bump_workflow,
)


def _make_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=120)


def _make_report(*, open_prs: list | None = None, migration_plan: object | None = None) -> AuditReport:
    return AuditReport(
        repo_name="demo-repo",
        repo_path="/tmp/demo-repo",
        generated_at="2026-11-19 14:00:00",
        git_state=GitStateInfo(
            branch="staging",
            base_branch="master",
            base_branch_resolution="origin/HEAD",
            is_clean=True,
        ),
        stacks=[],
        renovate_config=RenovateConfigInfo(exists=True, valid_json=True),
        dashboard=DependencyDashboardInfo(),
        open_prs=list(open_prs or []),
        ci_summary=CIStatusSummary(),
        env_matrix=[],
        deployment_files=[],
        reservations=[],
        overall_status="UP TO DATE",
        risk_assessments=[],
        migration_plan=migration_plan,
        production_info=ProductionEnvironmentInfo(configured=True, reachable=True),
    )


class MaintenanceWorkflowHelpersTests(unittest.TestCase):
    def test_compute_overall_status_covers_all_branches(self) -> None:
        self.assertEqual(
            _compute_overall_status(
                renovate_exists=False,
                has_problematic_drift=False,
                has_blocked_or_breaking_pr=False,
                open_prs_count=0,
                pending_updates_count=0,
                has_reservations=False,
            ),
            "RENOVATE NOT CONFIGURED",
        )
        self.assertEqual(
            _compute_overall_status(
                renovate_exists=True,
                has_problematic_drift=True,
                has_blocked_or_breaking_pr=False,
                open_prs_count=0,
                pending_updates_count=0,
                has_reservations=False,
            ),
            "ACTION REQUIRED (DRIFT OR HIGH-RISK PR)",
        )
        self.assertEqual(
            _compute_overall_status(
                renovate_exists=True,
                has_problematic_drift=False,
                has_blocked_or_breaking_pr=False,
                open_prs_count=1,
                pending_updates_count=0,
                has_reservations=False,
            ),
            "MAINTENANCE IN PROGRESS (OPEN OR PENDING UPDATES)",
        )
        self.assertEqual(
            _compute_overall_status(
                renovate_exists=True,
                has_problematic_drift=False,
                has_blocked_or_breaking_pr=False,
                open_prs_count=0,
                pending_updates_count=0,
                has_reservations=True,
            ),
            "AWAITING FINAL RUNTIME / CI CHECK",
        )
        self.assertEqual(
            _compute_overall_status(
                renovate_exists=True,
                has_problematic_drift=False,
                has_blocked_or_breaking_pr=False,
                open_prs_count=0,
                pending_updates_count=0,
                has_reservations=False,
            ),
            "UP TO DATE",
        )

    def test_prompt_update_selection_handles_all_selection_modes(self) -> None:
        updates = [
            SimpleNamespace(package="@nlabrazi/config-vitest", from_version="0.2.0", to_version="0.2.2", update_type="major"),
            SimpleNamespace(package="vitest", from_version="5.1.0", to_version="5.1.1", update_type="patch"),
            SimpleNamespace(package="nuxt", from_version="4.6.4", to_version="5.0.0", update_type="major"),
        ]
        self.assertTrue(_is_safe_direct_update("@nlabrazi/config-vitest", "major"))
        self.assertTrue(_is_safe_direct_update("vitest", "patch"))
        self.assertFalse(_is_safe_direct_update("nuxt", "major"))

        kwargs = {
            "item_label": "Paquet",
            "title": "Updates",
            "caption": "Caption",
            "prompt_text": "Choose",
            "confirm_text_template": "Update {package} ({from_version} -> {to_version}, {update_type})?",
            "target_console": _make_console(),
        }

        with patch("core.maintenance.workflow.Prompt.ask", return_value="q"):
            self.assertIsNone(_prompt_update_selection(updates, **kwargs))

        with patch("core.maintenance.workflow.Prompt.ask", return_value="1"):
            safe = _prompt_update_selection(updates, **kwargs)
        self.assertEqual([u.package for u in safe or []], ["@nlabrazi/config-vitest", "vitest"])

        with patch("core.maintenance.workflow.Prompt.ask", return_value="2"), patch(
            "core.maintenance.workflow.ask_yes_no", side_effect=[False, True, False]
        ):
            picked = _prompt_update_selection(updates, **kwargs)
        self.assertEqual([u.package for u in picked or []], ["vitest"])

        with patch("core.maintenance.workflow.Prompt.ask", return_value="3"):
            all_items = _prompt_update_selection(updates, **kwargs)
        self.assertEqual(len(all_items or []), 3)

    def test_audit_repository_collects_reservations_progress_and_prod_overrides(self) -> None:
        progress_events: list[tuple[int, str]] = []
        prod_info = ProductionEnvironmentInfo(
            configured=True,
            reachable=True,
            runtime_versions={"Node.js": "26.13.0"},
            issues=["prod notice"],
        )
        dashboard = DependencyDashboardInfo(
            issue_number=12,
            state="CLOSED",
            pending_or_rate_limited_items=["Update vite"],
        )
        drift_row = EnvironmentMatrixRow(
            component="Node.js",
            drift_status=DRIFT_PROBLEMATIC,
            drift_details="mismatch 24 vs 26",
        )
        breaking_pr = SimpleNamespace(analysis=SimpleNamespace(risk_level=RISK_BREAKING))

        def fake_open_prs(_path, _name, _matrix, _cfg, *, include_ollama_analysis, progress_callback):
            self.assertTrue(include_ollama_analysis)
            if progress_callback is not None:
                progress_callback("analyzing PR #1")
            return [breaking_pr], ["pr warning"]

        with patch(
            "core.maintenance.workflow.inspect_git_state",
            return_value=GitStateInfo("staging", "master", "origin/HEAD", True),
        ), patch(
            "core.maintenance.workflow.detect_project_stacks",
            return_value=([], [], ["detector note"]),
        ), patch(
            "core.maintenance.workflow.collect_declared_versions",
            return_value={},
        ), patch(
            "core.maintenance.workflow.inspect_production_environment",
            return_value=prod_info,
        ), patch(
            "core.maintenance.workflow.build_environment_matrix",
            return_value=[drift_row],
        ) as build_matrix, patch(
            "core.maintenance.workflow.inspect_renovate_config",
            return_value=RenovateConfigInfo(exists=True, valid_json=True, issues=["renovate note"]),
        ), patch(
            "core.maintenance.workflow.inspect_dependency_dashboard",
            return_value=dashboard,
        ), patch(
            "core.maintenance.workflow.inspect_open_renovate_prs",
            side_effect=fake_open_prs,
        ), patch(
            "core.maintenance.workflow.inspect_ci_status",
            return_value=CIStatusSummary(),
        ), patch(
            "core.maintenance.workflow.classify_repository_update_risks",
            return_value=[],
        ), patch(
            "core.maintenance.workflow.generate_migration_plan",
            return_value=None,
        ):
            report = audit_repository(
                "/tmp/demo",
                "demo",
                now=datetime(2026, 11, 19, 14, 0, 0),
                progress_callback=lambda step, msg: progress_events.append((step, msg)),
            )

        self.assertEqual(report.overall_status, "ACTION REQUIRED (DRIFT OR HIGH-RISK PR)")
        self.assertEqual(build_matrix.call_args.kwargs["production_overrides"], {"Node.js": "26.13.0"})
        self.assertTrue(any("analyzing PR #1" in msg for _, msg in progress_events))
        self.assertTrue(any("issue #12 is CLOSED" in r for r in report.reservations))
        self.assertTrue(any("Node.js: mismatch 24 vs 26" in r for r in report.reservations))

    def test_audit_with_progress_menu_and_prompt(self) -> None:
        fake_report = _make_report()
        buf_console = _make_console()

        def fake_audit(_path, _name, *, include_ollama_analysis, progress_callback):
            progress_callback(1, "step 1")
            return fake_report

        with patch("core.maintenance.workflow.console", buf_console), patch(
            "core.maintenance.workflow.audit_repository", side_effect=fake_audit
        ):
            result = _audit_with_progress("/tmp/demo", "demo")
            render_maintenance_menu("demo")

        self.assertIs(result, fake_report)

        with patch("core.maintenance.workflow.Prompt.ask", return_value="R"):
            self.assertEqual(ask_maintenance_action(), "r")


class MaintenanceBumpWorkflowsTests(unittest.TestCase):
    def test_run_guided_pr_bump_workflow_without_open_prs(self) -> None:
        report = _make_report(open_prs=[])
        out = _make_console()

        with patch("core.maintenance.workflow.collect_direct_staging_updates", return_value=[]):
            self.assertEqual(
                run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out),
                "no-prs",
            )

        updates = [SimpleNamespace(package="vitest", from_version="5.1.0", to_version="5.1.1", update_type="patch")]

        with patch("core.maintenance.workflow.collect_direct_staging_updates", return_value=updates), patch(
            "core.maintenance.workflow._prompt_update_selection", side_effect=[None, []]
        ):
            self.assertEqual(
                run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out),
                "skipped",
            )
            self.assertEqual(
                run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out),
                "skipped",
            )

        with patch("core.maintenance.workflow.collect_direct_staging_updates", return_value=updates), patch(
            "core.maintenance.workflow._prompt_update_selection", return_value=updates
        ), patch("core.maintenance.workflow.apply_direct_staging_updates", return_value="applied"), patch(
            "core.maintenance.workflow.ask_yes_no", return_value=True
        ), patch("core.maintenance.workflow.run_migration_validations", return_value=object()) as val_mock, patch(
            "core.maintenance.workflow.render_validation_report"
        ) as render_val, patch("core.maintenance.workflow.run_maintenance_auto_commit") as commit_mock, patch(
            "core.maintenance.workflow.run_maintenance_promote_to_base"
        ) as promote_mock:
            status = run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out)

        self.assertEqual(status, "processed")
        val_mock.assert_called_once()
        render_val.assert_called_once()
        commit_mock.assert_called_once()
        promote_mock.assert_called_once()

        with patch("core.maintenance.workflow.collect_direct_staging_updates", return_value=updates), patch(
            "core.maintenance.workflow._prompt_update_selection", return_value=updates
        ), patch("core.maintenance.workflow.apply_direct_staging_updates", return_value="failed"):
            self.assertEqual(
                run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out),
                "failed",
            )

    def test_run_guided_pr_bump_workflow_with_open_prs(self) -> None:
        pr = SimpleNamespace(number=10)
        report = _make_report(open_prs=[pr])
        out = _make_console()

        with patch("core.maintenance.workflow.render_renovate_pr_analysis"), patch(
            "core.maintenance.workflow.merge_renovate_pr_to_staging", return_value="skipped"
        ):
            self.assertEqual(
                run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out),
                "reviewed",
            )

        with patch("core.maintenance.workflow.render_renovate_pr_analysis"), patch(
            "core.maintenance.workflow.merge_renovate_pr_to_staging", return_value="merged"
        ), patch("core.maintenance.workflow.ask_yes_no", return_value=True), patch(
            "core.maintenance.workflow.run_migration_validations", return_value=object()
        ) as val_mock, patch("core.maintenance.workflow.render_validation_report"), patch(
            "core.maintenance.workflow.run_maintenance_promote_to_base"
        ) as promote_mock:
            status = run_guided_pr_bump_workflow("/tmp/demo", "demo", report, target_console=out)

        self.assertEqual(status, "processed")
        val_mock.assert_called_once()
        promote_mock.assert_called_once()

    def test_run_ruby_gem_bump_workflow_covers_all_paths(self) -> None:
        report = _make_report()
        out = _make_console()

        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(
                run_ruby_gem_bump_workflow(tmpdir, "demo", report, target_console=out),
                "not-ruby",
            )

            (Path(tmpdir) / "Gemfile").write_text('source "https://rubygems.org"\n', encoding="utf-8")

            with patch("core.maintenance.workflow.collect_ruby_gem_updates", return_value=[]):
                self.assertEqual(
                    run_ruby_gem_bump_workflow(tmpdir, "demo", report, target_console=out),
                    "up-to-date",
                )

            gems = [SimpleNamespace(package="rails", from_version="8.1.0", to_version="8.1.4", update_type="patch")]

            with patch("core.maintenance.workflow.collect_ruby_gem_updates", return_value=gems), patch(
                "core.maintenance.workflow._prompt_update_selection", side_effect=[None, []]
            ):
                self.assertEqual(
                    run_ruby_gem_bump_workflow(tmpdir, "demo", report, target_console=out),
                    "skipped",
                )
                self.assertEqual(
                    run_ruby_gem_bump_workflow(tmpdir, "demo", report, target_console=out),
                    "skipped",
                )

            with patch("core.maintenance.workflow.collect_ruby_gem_updates", return_value=gems), patch(
                "core.maintenance.workflow._prompt_update_selection", return_value=gems
            ), patch("core.maintenance.workflow.apply_ruby_gem_updates", return_value="applied"), patch(
                "core.maintenance.workflow.ask_yes_no", return_value=True
            ), patch("core.maintenance.workflow.run_migration_validations", return_value=object()) as val_mock, patch(
                "core.maintenance.workflow.render_validation_report"
            ), patch("core.maintenance.workflow.run_maintenance_auto_commit") as commit_mock, patch(
                "core.maintenance.workflow.run_maintenance_promote_to_base"
            ) as promote_mock:
                status = run_ruby_gem_bump_workflow(tmpdir, "demo", report, target_console=out)

            self.assertEqual(status, "processed")
            val_mock.assert_called_once()
            commit_mock.assert_called_once()
            promote_mock.assert_called_once()

            with patch("core.maintenance.workflow.collect_ruby_gem_updates", return_value=gems), patch(
                "core.maintenance.workflow._prompt_update_selection", return_value=gems
            ), patch("core.maintenance.workflow.apply_ruby_gem_updates", return_value="failed"):
                self.assertEqual(
                    run_ruby_gem_bump_workflow(tmpdir, "demo", report, target_console=out),
                    "failed",
                )


class MaintenanceRepoMenuLoopTests(unittest.TestCase):
    def test_run_dependency_maintenance_for_repo_dispatches_all_menu_actions(self) -> None:
        report = _make_report(migration_plan=object())
        incomplete_journal = SimpleNamespace(is_complete=False)
        checklist = SimpleNamespace(ready_to_deploy=True)
        post_report = SimpleNamespace(
            overall_status="VERIFIED",
            scheduled_jobs_status="OK",
            notes=["all green"],
        )

        actions = ["r", "1", "2", "3", "3", "3", "3", "3", "3", "4", "5", "q"]
        # Sub-menu prompts for the six "3" selections:
        # 1) sub="1" + invalid PR "abc"
        # 2) sub="1" + missing PR "#99"
        # 3) sub="1" + valid PR "42"
        # 4) sub="2" (risks & migration plan)
        # 5) sub="3" (full audit report)
        # 6) sub="4" (export report)
        prompt_answers = ["1", "abc", "1", "#99", "1", "42", "2", "3", "4"]

        with ExitStack() as stack:
            stack.enter_context(patch("core.maintenance.workflow.console", _make_console()))
            stack.enter_context(
                patch("core.maintenance.workflow.load_maintenance_journal", return_value=incomplete_journal)
            )
            render_journal = stack.enter_context(patch("core.maintenance.workflow.render_maintenance_journal"))
            audit_mock = stack.enter_context(
                patch("core.maintenance.workflow._audit_with_progress", return_value=report)
            )
            render_dash = stack.enter_context(patch("core.maintenance.workflow.render_project_dashboard"))
            record_step = stack.enter_context(patch("core.maintenance.workflow.record_maintenance_step"))
            stack.enter_context(patch("core.maintenance.workflow.render_maintenance_menu"))
            stack.enter_context(patch("core.maintenance.workflow.ask_maintenance_action", side_effect=actions))
            stack.enter_context(patch("core.maintenance.workflow.Prompt.ask", side_effect=prompt_answers))
            onboarding_mock = stack.enter_context(
                patch("core.maintenance.workflow.run_renovate_onboarding_workflow", return_value="applied")
            )
            bump_mock = stack.enter_context(
                patch("core.maintenance.workflow.run_guided_pr_bump_workflow", return_value="processed")
            )
            inspect_pr = stack.enter_context(
                patch(
                    "core.maintenance.workflow.inspect_single_renovate_pr",
                    side_effect=[None, SimpleNamespace(number=42)],
                )
            )
            render_pr = stack.enter_context(patch("core.maintenance.workflow.render_renovate_pr_analysis"))
            render_risks = stack.enter_context(patch("core.maintenance.workflow.render_risk_assessments"))
            render_plan = stack.enter_context(patch("core.maintenance.workflow.render_migration_plan"))
            render_audit = stack.enter_context(patch("core.maintenance.workflow.render_audit_report"))
            save_report = stack.enter_context(
                patch("core.maintenance.workflow.save_audit_report", return_value=("saved", ["out.md", "out.json"]))
            )
            stack.enter_context(patch("core.maintenance.workflow.render_production_environment_info"))
            stack.enter_context(
                patch("core.maintenance.workflow.prepare_deployment_and_rollback", return_value=checklist)
            )
            stack.enter_context(patch("core.maintenance.workflow.render_deployment_checklist"))
            stack.enter_context(patch("core.maintenance.workflow.ask_yes_no", return_value=True))
            stack.enter_context(
                patch("core.maintenance.workflow.verify_post_deployment", return_value=post_report)
            )
            stack.enter_context(patch("core.maintenance.workflow.render_post_deploy_verification"))
            ruby_mock = stack.enter_context(
                patch("core.maintenance.workflow.run_ruby_gem_bump_workflow", return_value="processed")
            )

            run_dependency_maintenance_for_repo("demo-repo", "/tmp/demo-repo")

        render_journal.assert_called_once_with(incomplete_journal, target_console=ANY)
        self.assertEqual(audit_mock.call_count, 2)
        self.assertEqual(render_dash.call_count, 2)
        onboarding_mock.assert_called_once()
        bump_mock.assert_called_once()
        self.assertEqual(inspect_pr.call_count, 2)
        render_pr.assert_called_once()
        render_risks.assert_called_once()
        render_plan.assert_called_once()
        render_audit.assert_called_once()
        save_report.assert_called_once()
        ruby_mock.assert_called_once()
        # audit + onboarding + bump + export + production_check + bump_gems
        self.assertEqual(record_step.call_count, 6)
        self.assertTrue(
            any(
                call.kwargs.get("is_complete") is True and call.args[2] == "production_check"
                for call in record_step.call_args_list
            )
        )

    def test_run_dependency_maintenance_for_all_repos_scans_without_ollama_and_renders_summary(self) -> None:
        from dataclasses import replace

        buffer = io.StringIO()
        out = Console(file=buffer, force_terminal=False, color_system=None, width=140)

        report_ok = replace(_make_report(), repo_name="alpha", overall_status="UP TO DATE")
        report_urgent = replace(
            _make_report(),
            repo_name="beta",
            overall_status="ACTION REQUIRED (DRIFT OR HIGH-RISK PR)",
            env_matrix=[
                EnvironmentMatrixRow(component="Node.js", drift_status=DRIFT_PROBLEMATIC, drift_details="24 vs 26")
            ],
        )

        seen_ollama_flags: list[bool] = []

        def fake_audit(repo_path, repo_name, *, include_ollama_analysis, prefix="", target_console=None):
            seen_ollama_flags.append(include_ollama_analysis)
            if repo_name == "broken":
                raise RuntimeError("git boom")
            return report_ok if repo_name == "alpha" else report_urgent

        with patch("core.maintenance.workflow._audit_with_progress", side_effect=fake_audit), patch(
            "core.maintenance.workflow.render_project_dashboard"
        ) as render_dash:
            reports = run_dependency_maintenance_for_all_repos(
                [("alpha", "/tmp/alpha"), ("broken", "/tmp/broken"), ("beta", "/tmp/beta")],
                target_console=out,
            )

        self.assertEqual([r.repo_name for r in reports], ["alpha", "beta"])
        self.assertEqual(seen_ollama_flags, [False, False, False])
        self.assertEqual(render_dash.call_count, 2)
        output = buffer.getvalue()
        self.assertIn("Bilan Global · Dependency Maintenance (3 dépôts)", output)
        self.assertIn("Synthèse Globale Dépendances", output)
        self.assertIn("ERREUR SCAN", output)
        self.assertIn("ACTION REQUISE", output)
        self.assertIn("À JOUR", output)

    def test_run_dependency_maintenance_for_all_repos_handles_empty_list(self) -> None:
        buffer = io.StringIO()
        out = Console(file=buffer, force_terminal=False, color_system=None, width=120)

        reports = run_dependency_maintenance_for_all_repos([], target_console=out)
        self.assertEqual(reports, [])
        self.assertIn("Aucun dépôt Git", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
