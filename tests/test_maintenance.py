import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.maintenance import (
    DRIFT_ACCEPTED,
    DRIFT_ALIGNED,
    DRIFT_PROBLEMATIC,
    STATUS_UNKNOWN,
    build_environment_matrix,
    collect_declared_versions,
    detect_project_stacks,
    inspect_git_state,
    inspect_renovate_config,
)
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


if __name__ == "__main__":
    unittest.main()
