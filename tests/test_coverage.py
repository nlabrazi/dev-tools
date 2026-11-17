import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console

from core.coverage import (
    OK,
    TODO,
    INSTALL,
    Project,
    Unit,
    analyze_js,
    analyze_project,
    analyze_py,
    analyze_rb,
    audit_coverage,
    audit_coverage_with_progress,
    fix_missing_gitignores,
    render_coverage_audit,
    resolve_project_coverage_command,
    run_coverage_workflow,
    run_repo_coverage,
)


class CoverageCoreTests(unittest.TestCase):
    def test_analyze_js_detects_vitest_and_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            pkg = {
                "name": "my-app",
                "scripts": {"test": "vitest run", "test:coverage": "vitest run --coverage"},
                "devDependencies": {
                    "@nlabrazi/config-vitest": "^0.2.0",
                    "vitest": "4.1.12",
                },
            }
            (root / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "sample.test.ts").write_text("// test", encoding="utf-8")

            node_modules = root / "node_modules" / "@vitest" / "coverage-v8"
            node_modules.mkdir(parents=True)
            (node_modules / "package.json").write_text(json.dumps({"version": "4.1.12"}), encoding="utf-8")

            unit = analyze_js(root, root, [], set())
            self.assertIsNotNone(unit)
            self.assertEqual(unit.status, OK)
            self.assertEqual(unit.how, "npm run test:coverage")
            self.assertIn("coverage/", unit.artefacts)

    def test_analyze_py_detects_pytest_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (root / "requirements-test.txt").write_text("pytest\npytest-cov\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_api.py").write_text("# py test", encoding="utf-8")

            unit = analyze_py(root, root, set())
            self.assertIsNotNone(unit)
            self.assertEqual(unit.status, OK)
            self.assertEqual(unit.how, "pytest --cov")

    def test_analyze_rb_detects_simplecov(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "Gemfile").write_text('gem "simplecov", require: false\n', encoding="utf-8")
            (root / "spec").mkdir()
            (root / "spec" / "spec_helper.rb").write_text("require 'simplecov'\nSimpleCov.start\n", encoding="utf-8")
            (root / "spec" / "model_spec.rb").write_text("# rspec", encoding="utf-8")

            unit = analyze_rb(root, root, set())
            self.assertIsNotNone(unit)
            self.assertEqual(unit.status, OK)
            self.assertIn("SimpleCov", unit.how)

    def test_resolve_project_coverage_command(self) -> None:
        p = Project(path=Path("/tmp/my-proj"))
        u = Unit(
            rel=Path("."),
            stack="Vitest 4.1.12",
            tests=10,
            status=OK,
            how="npm run test:coverage",
        )
        p.units = [u]

        resolved = resolve_project_coverage_command(p)
        self.assertIsNotNone(resolved)
        cmd, cwd = resolved
        self.assertEqual(cmd, "npm run test:coverage")
        self.assertEqual(cwd, Path("/tmp/my-proj"))

    def test_render_coverage_audit_outputs_summary_and_sections(self) -> None:
        buffer = io.StringIO()
        test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)

        p1 = Project(path=Path("/tmp/proj-ok"))
        p1.units = [Unit(rel=Path("."), stack="Vitest", tests=5, status=OK, how="npm run test:coverage")]

        p2 = Project(path=Path("/tmp/proj-install"))
        p2.units = [Unit(rel=Path("."), stack="node:test", tests=2, status=INSTALL, action="npm i -D c8")]
        p2.missing_ignore = ["coverage/"]

        render_coverage_audit([p1, p2], details=True, target_console=test_console)
        out = buffer.getvalue()

        self.assertIn("AUDIT COVERAGE", out)
        self.assertIn("proj-ok", out)
        self.assertIn("proj-install", out)
        self.assertIn("GITIGNORE À COMPLÉTER", out)

    def test_fix_missing_gitignores_appends_rules(self) -> None:
        buffer = io.StringIO()
        test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            gi = root / ".gitignore"
            gi.write_text("node_modules/\n", encoding="utf-8")

            proj = Project(path=root, git=True)
            proj.missing_ignore = ["coverage/", ".coverage"]

            with patch("core.coverage.ask_yes_no", return_value=True):
                updated = fix_missing_gitignores([proj], target_console=test_console)

            self.assertEqual(updated, 1)
            content = gi.read_text(encoding="utf-8")
            self.assertIn("coverage/", content)
            self.assertIn(".coverage", content)

    def test_audit_coverage_calls_progress_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            p1 = root / "repo-a"
            p1.mkdir()
            (p1 / "package.json").write_text("{}", encoding="utf-8")
            p2 = root / "repo-b"
            p2.mkdir()
            (p2 / "package.json").write_text("{}", encoding="utf-8")

            calls: list[tuple[int, int, str]] = []

            def on_progress(cur: int, tot: int, name: str) -> None:
                calls.append((cur, tot, name))

            projects = audit_coverage([root], progress_callback=on_progress)
            self.assertEqual(len(projects), 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], (1, 2, "repo-a"))
            self.assertEqual(calls[1], (2, 2, "repo-b"))

    def test_audit_coverage_with_progress_runs_successfully(self) -> None:
        buffer = io.StringIO()
        test_console = Console(file=buffer, force_terminal=False, color_system=None, width=120)

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            p1 = root / "my-repo"
            p1.mkdir()
            (p1 / "package.json").write_text("{}", encoding="utf-8")

            projects = audit_coverage_with_progress(
                [root],
                description="Test Audit",
                target_console=test_console,
            )
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].name, "my-repo")

    def test_find_projects_excludes_unity_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            web_repo = root / "web-app"
            web_repo.mkdir()
            (web_repo / "package.json").write_text("{}", encoding="utf-8")

            unity_repo = root / "unity-game"
            unity_repo.mkdir()
            (unity_repo / ".git").mkdir()
            settings = unity_repo / "ProjectSettings"
            settings.mkdir()
            (settings / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3\n", encoding="utf-8")

            # Default: excluded
            projects = audit_coverage([root], exclude_unity=True)
            names = [p.name for p in projects]
            self.assertIn("web-app", names)
            self.assertNotIn("unity-game", names)

            # Explicit: included
            projects_included = audit_coverage([root], exclude_unity=False)
            names_included = [p.name for p in projects_included]
            self.assertIn("web-app", names_included)
            self.assertIn("unity-game", names_included)

    def test_run_coverage_workflow_quits_cleanly(self) -> None:
        with patch("core.coverage.Prompt.ask", return_value="q"), patch(
            "run.section_title"
        ):
            run_coverage_workflow(["/tmp/root"])


if __name__ == "__main__":
    unittest.main()
