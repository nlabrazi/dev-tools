import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.maintenance import (
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


if __name__ == "__main__":
    unittest.main()
