import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from core.maintenance import (
    DRIFT_ACCEPTED,
    DRIFT_ALIGNED,
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
    RISK_LOW,
    RISK_REVIEW,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    RenovateConfigInfo,
    RenovatePRInfo,
    RenovateUpdateItem,
    analyze_renovate_pr,
    audit_repository,
    build_environment_matrix,
    classify_repository_update_risks,
    collect_declared_versions,
    detect_project_stacks,
    extract_release_notes_signals,
    format_report_json,
    format_report_markdown,
    inspect_dependency_dashboard,
    inspect_git_state,
    inspect_renovate_config,
    parse_dashboard_pending_update,
    parse_renovate_pr_body,
    render_audit_report,
    save_audit_report,
    summarize_risk_assessments,
)
from core.ollama import OllamaError
from utils.common import set_dry_run


class MaintenanceDetectorTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_dry_run(False)

    def test_detects_python_unpinned_dependencies_and_pip_less_venv_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "requirements.txt").write_text(
                "feedparser\nrequests==2.32.3\n# comment\n-r other.txt\npsycopg2-binary>=2.9\n",
                encoding="utf-8",
            )
            venv_target = root / ".venv-315"
            (venv_target / "bin").mkdir(parents=True)
            (venv_target / "bin" / "python").write_text("", encoding="utf-8")
            os.symlink(".venv-315", root / ".venv")

            (root / "deploy.sh").write_text("#!/bin/bash\npip install -r requirements.txt\n", encoding="utf-8")

            with patch(
                "core.maintenance.detector.git_output",
                side_effect=[
                    "staging",
                    " M requirements.txt\n?? .venv\n?? .venv-315/\n",
                    "0\t2",
                ],
            ), patch(
                "core.maintenance.detector.resolve_repo_base_branch",
                return_value=("master", "resolved from origin/HEAD"),
            ):
                git_state = inspect_git_state(tmp_dir)
                stacks, deploy_files, reservations = detect_project_stacks(tmp_dir, "sjvtdm", git_state=git_state)

            self.assertFalse(git_state.is_clean)
            self.assertTrue(git_state.venv_exists)
            self.assertTrue(git_state.venv_is_symlink)
            self.assertEqual(git_state.venv_target, ".venv-315")
            self.assertFalse(git_state.venv_has_pip)
            self.assertEqual(git_state.ahead_of_base, 2)
            self.assertEqual(git_state.behind_base, 0)
            self.assertIn(".venv", git_state.untracked_venv_entries)

            py_stack = next(s for s in stacks if s.ecosystem == "python")
            self.assertEqual(py_stack.pinned_count, 1)
            self.assertEqual(py_stack.unpinned_dependencies, ["feedparser", "psycopg2-binary"])
            self.assertIn("deploy.sh", deploy_files)
            self.assertTrue(any("Unpinned Python dependencies" in r for r in reservations))
            self.assertTrue(any("no 'pip' binary" in r or "no pip binary" in r for r in reservations))
            self.assertTrue(any(".git/info/exclude" in r for r in reservations))

    def test_detects_multi_stack_python_node_ruby_and_docker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "api").mkdir()
            (root / "ui").mkdir()
            (root / "api" / "requirements.txt").write_text("fastapi==0.115.0\n", encoding="utf-8")
            (root / "ui" / "package.json").write_text(
                json.dumps(
                    {
                        "dependencies": {"nuxt": "^4.4.2", "vue": "^3.5.0"},
                        "devDependencies": {"@playwright/test": "^1.63.0"},
                    }
                ),
                encoding="utf-8",
            )
            (root / "ui" / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rails'\n", encoding="utf-8")
            (root / "Dockerfile").write_text("FROM ruby:3.4-slim\n", encoding="utf-8")
            (root / "Jenkinsfile").write_text("pipeline {}", encoding="utf-8")
            wf_dir = root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "ci.yml").write_text("name: CI\n", encoding="utf-8")

            with patch("core.maintenance.detector.git_output", return_value=""), patch(
                "core.maintenance.detector.resolve_repo_base_branch",
                return_value=("main", "resolved"),
            ):
                stacks, _, _ = detect_project_stacks(tmp_dir, "multi-app")

            ecosystems = [s.ecosystem for s in stacks]
            self.assertIn("python", ecosystems)
            self.assertIn("nodejs", ecosystems)
            self.assertIn("ruby", ecosystems)
            self.assertIn("docker", ecosystems)
            self.assertIn("ci", ecosystems)
            node_stack = next(s for s in stacks if s.ecosystem == "nodejs")
            self.assertIn("Nuxt", node_stack.frameworks)
            self.assertIn("Playwright", node_stack.frameworks)

    def test_inspect_renovate_config_handles_missing_preset_and_private_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            missing_cfg = inspect_renovate_config(tmp_dir)
            self.assertFalse(missing_cfg.exists)
            self.assertTrue(any("Missing renovate.json" in i for i in missing_cfg.issues))

            (root / ".npmrc").write_text("@nlabrazi:registry=https://npm.pkg.github.com\n", encoding="utf-8")
            (root / "renovate.json").write_text(
                json.dumps(
                    {
                        "extends": ["config:recommended"],
                        "prConcurrentLimit": 5,
                        "automerge": False,
                        "npmrc": "@nlabrazi:registry=https://npm.pkg.github.com/",
                    }
                ),
                encoding="utf-8",
            )
            custom_cfg = inspect_renovate_config(tmp_dir)
            self.assertTrue(custom_cfg.exists)
            self.assertTrue(custom_cfg.valid_json)
            self.assertFalse(custom_cfg.extends_dev_platform)
            self.assertTrue(custom_cfg.has_private_registry)
            self.assertEqual(custom_cfg.pr_concurrent_limit, 5)
            self.assertTrue(any("does not extend central preset" in i for i in custom_cfg.issues))

            (root / "renovate.json").write_text(
                json.dumps({"extends": ["github>nlabrazi/dev-platform//configs/renovate/base"]}),
                encoding="utf-8",
            )
            central_cfg = inspect_renovate_config(tmp_dir)
            self.assertTrue(central_cfg.extends_dev_platform)
            self.assertEqual(central_cfg.issues, [])


class EnvironmentMatrixTests(unittest.TestCase):
    def test_builds_matrix_with_aligned_python_and_accepted_postgres_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "Jenkinsfile").write_text(
                "pipeline { agent { docker { image 'python:3.15.2' } } }\n",
                encoding="utf-8",
            )
            wf_dir = root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "tests.yml").write_text(
                'services:\n  postgres:\n    image: postgres:18\nsteps:\n  - with:\n      python-version: "3.15.2"\n',
                encoding="utf-8",
            )
            (root / "docker-compose.yml").write_text(
                "services:\n  db:\n    image: postgres:16\n",
                encoding="utf-8",
            )

            declared = collect_declared_versions(tmp_dir)
            matrix = build_environment_matrix(tmp_dir, declared, production_overrides={"PostgreSQL": "16.13"})

            by_comp = {row.component: row for row in matrix}
            self.assertIn("Python", by_comp)
            self.assertEqual(by_comp["Python"].github_actions, "3.15.2")
            self.assertEqual(by_comp["Python"].jenkins, "3.15.2")
            self.assertEqual(by_comp["Python"].production, STATUS_UNKNOWN)
            self.assertEqual(by_comp["Python"].drift_status, DRIFT_ALIGNED)

            self.assertIn("PostgreSQL", by_comp)
            self.assertEqual(by_comp["PostgreSQL"].github_actions, "18")
            self.assertEqual(by_comp["PostgreSQL"].docker, "16")
            self.assertEqual(by_comp["PostgreSQL"].production, "16.13")
            self.assertEqual(by_comp["PostgreSQL"].drift_status, DRIFT_ACCEPTED)

    def test_detects_problematic_runtime_drift_and_resolves_dockerfile_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".nvmrc").write_text("22\n", encoding="utf-8")
            (root / "Dockerfile").write_text(
                "ARG NODE_IMAGE=node:24-bookworm-slim\nFROM ${NODE_IMAGE} AS base\n",
                encoding="utf-8",
            )
            (root / "Jenkinsfile").write_text(
                "pipeline { agent { docker { image 'node:24-bookworm' } } }\n",
                encoding="utf-8",
            )

            declared = collect_declared_versions(tmp_dir)
            matrix = build_environment_matrix(tmp_dir, declared)
            node_row = next(r for r in matrix if r.component == "Node.js")

            self.assertEqual(node_row.local, "22")
            self.assertEqual(node_row.docker, "24")
            self.assertEqual(node_row.jenkins, "24")
            self.assertEqual(node_row.drift_status, DRIFT_PROBLEMATIC)


class RenovateAndOllamaPRTests(unittest.TestCase):
    def test_parse_renovate_pr_body_extracts_updates_warnings_and_breaking_notes(self) -> None:
        body = (
            "This PR contains the following updates:\n\n"
            "| Package | Change | Age | Confidence |\n"
            "|---|---|---|---|\n"
            "| [@playwright/test](https://playwright.dev) | [`1.63.0` → `1.65.0`](https://example.com) | age | conf |\n\n"
            "> [!WARNING]\n> Some dependencies could not be looked up.\n\n"
            "### Release Notes\n\n"
            "#### Breaking changes ⚠️\n- `screen` property is now forwarded.\n\n"
            "### Configuration\n"
        )
        updates, has_rn, has_warn, excerpt = parse_renovate_pr_body(
            "chore(deps): update dependency @playwright/test to v1.65.0",
            body,
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].package, "@playwright/test")
        self.assertEqual(updates[0].from_version, "1.63.0")
        self.assertEqual(updates[0].to_version, "1.65.0")
        self.assertEqual(updates[0].update_type, "minor")
        self.assertTrue(has_rn)
        self.assertTrue(has_warn)
        self.assertIn("Breaking changes", excerpt)

    def test_analyze_renovate_pr_uses_ollama_and_preserves_deterministic_drift_impacts(self) -> None:
        pr = RenovatePRInfo(
            number=23,
            title="chore(deps): update dependency @playwright/test to v1.65.0",
            head_branch="renovate/playwright-monorepo",
            base_branch="staging",
            ci_status=STATUS_PASS,
            merge_state="CLEAN",
            changed_files=["package-lock.json"],
            updates=[
                RenovateUpdateItem(
                    package="@playwright/test",
                    update_type="minor",
                    from_version="1.63.0",
                    to_version="1.65.0",
                    raw_change="1.63.0 -> 1.65.0",
                )
            ],
            has_release_notes=True,
            has_renovate_warning=True,
            release_notes_excerpt="### Release Notes\n#### Breaking changes ⚠️\n- screen forwarded",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "Jenkinsfile").write_text(
                "pipeline { agent { docker { image 'mcr.microsoft.com/playwright:v1.63.0-noble' } } }\n",
                encoding="utf-8",
            )
            env_matrix = build_environment_matrix(tmp_dir, collect_declared_versions(tmp_dir))
            renovate_cfg = RenovateConfigInfo(exists=True, valid_json=True, extends_dev_platform=True)

            ollama_json = json.dumps(
                {
                    "pr_analysis": {
                        "risk_level": "LOW RISK",
                        "breaking_change": True,
                        "breaking_summary": "Playwright 1.64 introduit un changement sur window.screen.",
                        "summary": "Mise à jour de @playwright/test de 1.63.0 vers 1.65.0.",
                        "key_changes": ["Support de Bun", "Changement sur window.screen"],
                        "project_impacts": ["Impact sur les tests E2E."],
                        "checks_before_merge": ["Vérifier la suite E2E."],
                        "verdict": "Adapter Jenkinsfile avant de merger.",
                    }
                }
            )

            with patch("core.maintenance.renovate.is_ollama_enabled", return_value=True), patch(
                "core.maintenance.renovate.ensure_repo_context_allowed"
            ), patch("core.maintenance.renovate.chat_json", return_value=ollama_json):
                analysis = analyze_renovate_pr("riftvision", tmp_dir, pr, env_matrix, renovate_cfg)

        self.assertEqual(analysis.source, "ollama")
        self.assertEqual(analysis.risk_level, RISK_BREAKING)
        self.assertTrue(analysis.breaking_change)
        self.assertIn("Playwright 1.64", analysis.breaking_summary)
        self.assertTrue(any("1.63.0" in imp for imp in analysis.project_impacts))

    def test_analyze_renovate_pr_falls_back_to_heuristic_when_ollama_fails(self) -> None:
        pr = RenovatePRInfo(
            number=56,
            title="chore(deps): update postgres docker tag to v18",
            head_branch="renovate/postgres-18.x",
            base_branch="staging",
            ci_status=STATUS_FAIL,
            merge_state="UNSTABLE",
            changed_files=[".github/workflows/tests.yml"],
            updates=[
                RenovateUpdateItem(
                    package="postgres",
                    update_type="major",
                    from_version="14",
                    to_version="18",
                    raw_change="14 -> 18",
                )
            ],
            has_release_notes=False,
            has_renovate_warning=False,
            release_notes_excerpt="",
        )
        renovate_cfg = RenovateConfigInfo(exists=True, valid_json=True, extends_dev_platform=True)

        with patch("core.maintenance.renovate.is_ollama_enabled", return_value=True), patch(
            "core.maintenance.renovate.ensure_repo_context_allowed"
        ), patch("core.maintenance.renovate.chat_json", side_effect=OllamaError("connection refused")), patch(
            "builtins.print"
        ):
            analysis = analyze_renovate_pr("sjvtdm", "/tmp/repo", pr, [], renovate_cfg)

        self.assertEqual(analysis.source, "heuristic")
        self.assertEqual(analysis.risk_level, RISK_BLOCKED)
        self.assertTrue(any("CI" in chk for chk in analysis.checks_before_merge))

    def test_inspect_dependency_dashboard_parses_clean_and_pending_states(self) -> None:
        dashboard_payload = [
            {
                "number": 52,
                "title": "Dependency Dashboard",
                "state": "OPEN",
                "body": (
                    "This repository currently has no open or pending branches.\n"
                    "<details><summary>github-actions (1)</summary></details>\n"
                    "<details><summary>pip_requirements (1)</summary></details>\n"
                    "- [ ] <!-- manual job -->Check this box\n"
                ),
            }
        ]
        completed = subprocess.CompletedProcess(
            args=["gh", "issue", "list"],
            returncode=0,
            stdout=json.dumps(dashboard_payload),
            stderr="",
        )
        with patch("core.maintenance.renovate.run_command", return_value=completed):
            info = inspect_dependency_dashboard("/tmp/repo")

        self.assertEqual(info.status, STATUS_PASS)
        self.assertEqual(info.issue_number, 52)
        self.assertTrue(info.no_open_or_pending)
        self.assertEqual(info.open_items, [])
        self.assertEqual(info.detected_managers, ["github-actions", "pip_requirements"])


class AuditAndExportDryRunTests(unittest.TestCase):
    def tearDown(self) -> None:
        set_dry_run(False)

    def test_save_audit_report_respects_dry_run_and_writes_files_in_prod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "renovate.json").write_text(
                json.dumps({"extends": ["github>nlabrazi/dev-platform//configs/renovate/base"]}),
                encoding="utf-8",
            )
            (root / "Jenkinsfile").write_text(
                "pipeline { agent { docker { image 'python:3.15.2' } } }\n",
                encoding="utf-8",
            )

            empty_gh = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")
            with patch("core.maintenance.detector.git_output", return_value="staging"), patch(
                "core.maintenance.detector.resolve_repo_base_branch",
                return_value=("master", "resolved"),
            ), patch("core.maintenance.renovate.run_command", return_value=empty_gh):
                report = audit_repository(tmp_dir, "demo", include_ollama_analysis=False)

            set_dry_run(True)
            status_dry, paths_dry = save_audit_report(report, output_dir=tmp_dir)
            self.assertEqual(status_dry, "dry-run")
            for p in paths_dry:
                self.assertFalse(Path(p).exists())

            set_dry_run(False)
            status_prod, paths_prod = save_audit_report(report, output_dir=tmp_dir)
            self.assertEqual(status_prod, "saved")
            for p in paths_prod:
                self.assertTrue(Path(p).is_file())

            parsed_json = json.loads(format_report_json(report))
            self.assertEqual(parsed_json["repo_name"], "demo")
            md_text = format_report_markdown(report)
            self.assertIn("# Dependency Maintenance Audit — `demo`", md_text)

            buffer = io.StringIO()
            test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)
            render_audit_report(report, target_console=test_console)
            rendered = buffer.getvalue()
            self.assertIn("Environment Version Matrix", rendered)
            self.assertIn("Final Maintenance Summary", rendered)


class UpdateRiskClassificationTests(unittest.TestCase):
    def test_extracts_release_notes_signals_and_parses_dashboard_pending_items(self) -> None:
        signals_empty = extract_release_notes_signals("")
        self.assertFalse(signals_empty["has_breaking_marker"])
        self.assertTrue(signals_empty["uncertainties"])

        notes = (
            "### Release Notes\n"
            "#### Breaking changes ⚠️\n"
            "- Requires Node.js >= 20.0.0\n"
            "- Deprecated oldConfig option\n"
        )
        signals = extract_release_notes_signals(notes)
        self.assertTrue(signals["has_breaking_marker"])
        self.assertTrue(any("Node.js" in c for c in signals["runtime_constraints"]))
        self.assertTrue(any("Deprecated" in d for d in signals["deprecation_lines"]))

        pkg, from_v, to_v, u_type = parse_dashboard_pending_update(
            "chore(deps): update dependency @playwright/test from v1.63.0 to v1.65.0"
        )
        self.assertEqual(pkg, "@playwright/test")
        self.assertEqual(from_v, "1.63.0")
        self.assertEqual(to_v, "1.65.0")
        self.assertEqual(u_type, "minor")

    def test_classifies_risks_with_sources_uncertainties_and_accepted_postgres_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "requirements.txt").write_text("feedparser\n", encoding="utf-8")
            (root / "Jenkinsfile").write_text(
                "pipeline { agent { docker { image 'python:3.11' } } }\n",
                encoding="utf-8",
            )
            wf_dir = root / ".github" / "workflows"
            wf_dir.mkdir(parents=True)
            (wf_dir / "tests.yml").write_text(
                'services:\n  postgres:\n    image: postgres:18\nsteps:\n  - with:\n      python-version: "3.15.2"\n',
                encoding="utf-8",
            )
            (root / "docker-compose.yml").write_text(
                "services:\n  db:\n    image: postgres:16\n",
                encoding="utf-8",
            )
            venv_target = root / ".venv-315"
            (venv_target / "bin").mkdir(parents=True)
            (venv_target / "bin" / "python").write_text("", encoding="utf-8")
            os.symlink(".venv-315", root / ".venv")
            (root / "deploy.sh").write_text("#!/bin/bash\npip install -r requirements.txt\n", encoding="utf-8")

            empty_gh = subprocess.CompletedProcess(args=["gh"], returncode=0, stdout="[]", stderr="")
            with patch("core.maintenance.detector.git_output", return_value="staging"), patch(
                "core.maintenance.detector.resolve_repo_base_branch",
                return_value=("master", "resolved"),
            ), patch("core.maintenance.renovate.run_command", return_value=empty_gh):
                report = audit_repository(tmp_dir, "sjvtdm", include_ollama_analysis=False)

            assessments = classify_repository_update_risks(report)
            summary = summarize_risk_assessments(assessments)

            self.assertGreaterEqual(summary[RISK_BREAKING], 1)
            self.assertGreaterEqual(summary[RISK_REVIEW], 2)

            by_subject = {item.subject: item for item in assessments}
            # Pip-less venv + pip install in deploy.sh -> BREAKING CHANGE
            pip_risk = by_subject["Virtualenv & Deployment Pip Compatibility"]
            self.assertEqual(pip_risk.risk_level, RISK_BREAKING)
            self.assertTrue(pip_risk.breaking_change)
            self.assertIn("deploy.sh", pip_risk.affected_files)

            # Python 3.11 in Jenkins vs 3.15.2 in GHA -> REVIEW REQUIRED with target 3.15.2
            py_risk = by_subject["Runtime Alignment · Python"]
            self.assertEqual(py_risk.risk_level, RISK_REVIEW)
            self.assertEqual(py_risk.target_version, "3.15.2")
            self.assertTrue(py_risk.sources)
            self.assertTrue(any("UNKNOWN" in u for u in py_risk.uncertainties))

            # PostgreSQL 18 in CI vs 16 in Docker -> LOW RISK (accepted difference, never forced)
            pg_risk = by_subject["Service Compatibility · PostgreSQL"]
            self.assertEqual(pg_risk.risk_level, RISK_LOW)
            self.assertFalse(pg_risk.breaking_change)
            self.assertTrue(pg_risk.sources)
            self.assertTrue(pg_risk.uncertainties)


if __name__ == "__main__":
    unittest.main()
