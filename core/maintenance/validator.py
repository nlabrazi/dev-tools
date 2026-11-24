import json
import re
from datetime import datetime
from pathlib import Path

from core.maintenance.models import (
    DRIFT_PROBLEMATIC,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_UNKNOWN,
    AuditReport,
    ValidationCheckResult,
    ValidationReport,
)
from utils.common import describe_command_failure, is_dry_run, run_command

UNITTEST_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?", re.IGNORECASE)
UNITTEST_SKIPPED_RE = re.compile(r"skipped\s*=\s*(\d+)", re.IGNORECASE)
UNITTEST_FAILED_RE = re.compile(r"(?:failures|errors)\s*=\s*(\d+)", re.IGNORECASE)
PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
PYTEST_FAILED_RE = re.compile(r"(\d+)\s+(?:failed|error)", re.IGNORECASE)
PYTEST_SKIPPED_RE = re.compile(r"(\d+)\s+skipped", re.IGNORECASE)
NODE_TEST_PASS_RE = re.compile(r"(?:ℹ|i)\s+pass\s+(\d+)")
NODE_TEST_FAIL_RE = re.compile(r"(?:ℹ|i)\s+fail\s+(\d+)")
NODE_TEST_SKIP_RE = re.compile(r"(?:ℹ|i)\s+skipped\s+(\d+)")
VITEST_LINE_RE = re.compile(r"Tests\s+([^\n]+)")
RSPEC_RE = re.compile(r"(\d+)\s+examples?,\s*(\d+)\s+failures?(?:,\s*(\d+)\s+pending)?", re.IGNORECASE)
BIOME_CHECKED_RE = re.compile(r"Checked\s+(\d+)\s+files?", re.IGNORECASE)


def _failure_excerpt(output: str, max_lines: int = 6) -> str:
    lines = [line.rstrip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return " | ".join(lines[-max_lines:])[:280]


def parse_validation_output(output: str, returncode: int, *, category: str = "tests") -> tuple[str, int, int, int, str]:
    text = (output or "").strip()

    if category == "lint":
        biome_m = BIOME_CHECKED_RE.search(text)
        if returncode == 0:
            checked = int(biome_m.group(1)) if biome_m else 1
            msg = f"Biome OK ({checked} fichier(s) vérifié(s))." if biome_m else "Check OK (exit 0)."
            return STATUS_PASS, checked, 0, 0, msg
        excerpt = _failure_excerpt(text, max_lines=4)
        return STATUS_FAIL, 0, 1, 0, f"Échec lint/format (exit {returncode}){': ' + excerpt if excerpt else ''}"

    # Node native test runner (node --test)
    nt_pass = NODE_TEST_PASS_RE.search(text)
    nt_fail = NODE_TEST_FAIL_RE.search(text)
    nt_skip = NODE_TEST_SKIP_RE.search(text)
    if nt_pass or nt_fail:
        passed = int(nt_pass.group(1)) if nt_pass else 0
        failed = int(nt_fail.group(1)) if nt_fail else (1 if returncode != 0 else 0)
        skipped = int(nt_skip.group(1)) if nt_skip else 0
        status = STATUS_FAIL if (failed > 0 or returncode != 0) else (STATUS_SKIPPED if skipped > 0 else STATUS_PASS)
        return status, passed, failed, skipped, f"Node test: {passed} passed, {failed} failed, {skipped} skipped."

    # Vitest summary line ("Tests  42 passed (42)")
    vitest_m = VITEST_LINE_RE.search(text)
    if vitest_m:
        line = vitest_m.group(1)
        p_m = PYTEST_PASSED_RE.search(line)
        f_m = PYTEST_FAILED_RE.search(line)
        s_m = PYTEST_SKIPPED_RE.search(line)
        passed = int(p_m.group(1)) if p_m else 0
        failed = int(f_m.group(1)) if f_m else (1 if returncode != 0 else 0)
        skipped = int(s_m.group(1)) if s_m else 0
        status = STATUS_FAIL if (failed > 0 or returncode != 0) else (STATUS_SKIPPED if skipped > 0 else STATUS_PASS)
        return status, passed, failed, skipped, f"Vitest: {passed} passed, {failed} failed, {skipped} skipped."

    # RSpec summary
    rspec_m = RSPEC_RE.search(text)
    if rspec_m:
        total = int(rspec_m.group(1))
        failed = int(rspec_m.group(2))
        skipped = int(rspec_m.group(3) or 0)
        passed = max(0, total - failed - skipped)
        status = STATUS_FAIL if (failed > 0 or returncode != 0) else (STATUS_SKIPPED if skipped > 0 else STATUS_PASS)
        return status, passed, failed, skipped, f"RSpec: {passed} passed, {failed} failed, {skipped} pending."

    return parse_python_test_output(output, returncode)


def parse_python_test_output(output: str, returncode: int) -> tuple[str, int, int, int, str]:
    text = (output or "").strip()
    ran_m = UNITTEST_RAN_RE.search(text)
    if ran_m:
        total = int(ran_m.group(1))
        skipped = sum(int(m.group(1)) for m in UNITTEST_SKIPPED_RE.finditer(text))
        failed = sum(int(m.group(1)) for m in UNITTEST_FAILED_RE.finditer(text))
        if returncode != 0 and failed == 0:
            failed = 1
        passed = max(0, total - failed - skipped)
        if failed > 0 or returncode != 0:
            status = STATUS_FAIL
        elif skipped > 0:
            status = STATUS_SKIPPED
        elif total == 0:
            status = STATUS_UNKNOWN
        else:
            status = STATUS_PASS
        summary = f"Ran {total} test(s): {passed} passed, {failed} failed, {skipped} skipped."
        return status, passed, failed, skipped, summary

    py_pass = PYTEST_PASSED_RE.search(text)
    py_fail = PYTEST_FAILED_RE.search(text)
    py_skip = PYTEST_SKIPPED_RE.search(text)
    if py_pass or py_fail or py_skip:
        passed = int(py_pass.group(1)) if py_pass else 0
        failed = int(py_fail.group(1)) if py_fail else (1 if returncode != 0 else 0)
        skipped = int(py_skip.group(1)) if py_skip else 0
        if failed > 0 or returncode != 0:
            status = STATUS_FAIL
        elif skipped > 0:
            status = STATUS_SKIPPED
        else:
            status = STATUS_PASS
        summary = f"Pytest: {passed} passed, {failed} failed, {skipped} skipped."
        return status, passed, failed, skipped, summary

    if returncode == 0:
        return STATUS_PASS, 1, 0, 0, "Test command exited with 0."
    return STATUS_FAIL, 0, 1, 0, f"Test command failed (exit {returncode})."


def _has_python_tests(directory: Path) -> bool:
    if any(directory.glob("test_*.py")) or any(directory.glob("*_test.py")):
        return True
    tests_dir = directory / "tests"
    if tests_dir.is_dir():
        if any(tests_dir.rglob("*.py")):
            return True
    return False


def _uses_pytest(directory: Path) -> bool:
    if (directory / "pytest.ini").is_file() or (directory / "tests" / "conftest.py").is_file() or (directory / "conftest.py").is_file():
        return True
    for manifest in (directory / "pyproject.toml", directory / "requirements.txt", directory / "requirements-dev.txt"):
        if manifest.is_file():
            try:
                if "pytest" in manifest.read_text(encoding="utf-8", errors="ignore").lower():
                    return True
            except OSError:
                pass
    return False


def _detect_validation_commands(repo_root: Path) -> list[tuple[str, str, list[str], Path]]:
    """Detect all real local check/test commands across Node.js, Python, and Ruby stacks."""
    commands: list[tuple[str, str, list[str], Path]] = []

    # 1. Node.js checks & tests (root + common frontend subdirs like ui/)
    for directory in (repo_root, repo_root / "ui", repo_root / "frontend"):
        pkg_json = directory / "package.json"
        if not pkg_json.is_file():
            continue
        suffix = f" ({directory.name})" if directory != repo_root else ""
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        if not isinstance(scripts, dict):
            continue
        if isinstance(scripts.get("check"), str):
            commands.append((f"Biome Check{suffix}", "lint", ["npm", "run", "check"], directory))
        if isinstance(scripts.get("test"), str):
            commands.append((f"Node / Vitest Tests{suffix}", "tests", ["npm", "test"], directory))
        if isinstance(scripts.get("pack:check"), str):
            commands.append((f"Package Pack Check{suffix}", "build", ["npm", "run", "pack:check"], directory))

    # 2. Python tests (only when actual .py tests exist, OR fallback when tests/ exists without Node/Ruby)
    has_non_python_stack = (repo_root / "package.json").is_file() or (repo_root / "Gemfile").is_file()
    for directory in (repo_root, repo_root / "api", repo_root / "backend"):
        if not directory.is_dir():
            continue
        py_tests_present = _has_python_tests(directory)
        empty_tests_fallback = directory == repo_root and (repo_root / "tests").is_dir() and not has_non_python_stack
        if not (py_tests_present or empty_tests_fallback):
            continue
        suffix = f" ({directory.name})" if directory != repo_root else ""
        venv_py = directory / ".venv" / "bin" / "python"
        root_venv_py = repo_root / ".venv" / "bin" / "python"
        py_bin = str(venv_py) if venv_py.is_file() else (str(root_venv_py) if root_venv_py.is_file() else "python3")
        if _uses_pytest(directory):
            commands.append((f"Pytest{suffix}", "tests", [py_bin, "-m", "pytest", "-q"], directory))
        else:
            commands.append((f"Python Unittest{suffix}", "tests", [py_bin, "-m", "unittest", "discover", "-v"], directory))

    # 3. Ruby / Rails checks & specs
    if (repo_root / "Gemfile").is_file():
        if (repo_root / "spec").is_dir():
            commands.append(("Ruby RSpec", "tests", ["bundle", "exec", "rspec"], repo_root))
        if (repo_root / ".rubocop.yml").is_file():
            commands.append(("RuboCop", "lint", ["bundle", "exec", "rubocop"], repo_root))

    return commands


def _detect_test_command(repo_root: Path) -> list[str] | None:
    detected = _detect_validation_commands(repo_root)
    for _name, category, cmd, _cwd in detected:
        if category == "tests":
            return cmd
    return detected[0][2] if detected else None


def _run_local_checks(repo_path: str, execute_local_tests: bool) -> list[ValidationCheckResult]:
    repo_root = Path(repo_path)
    detected = _detect_validation_commands(repo_root)
    if not detected:
        return [
            ValidationCheckResult(
                name="Local Unit / Integration Tests",
                category="tests",
                status=STATUS_UNKNOWN,
                details="No standard local check or test runner detected in repository.",
            )
        ]

    results: list[ValidationCheckResult] = []
    for name, category, cmd, cwd in detected:
        cmd_str = " ".join(cmd)
        if not execute_local_tests or is_dry_run():
            reason = (
                "[DRY-RUN] Local validation skipped in dry-run mode."
                if is_dry_run()
                else "Local validation skipped by user."
            )
            results.append(
                ValidationCheckResult(
                    name=name,
                    category=category,
                    status=STATUS_SKIPPED,
                    command=cmd_str,
                    details=reason,
                )
            )
            continue

        res = run_command(cmd, cwd=str(cwd), silent=True, timeout=180)
        combined = f"{res.stdout or ''}\n{res.stderr or ''}".strip()
        if res.returncode == 124:
            results.append(
                ValidationCheckResult(
                    name=name,
                    category=category,
                    status=STATUS_FAIL,
                    command=cmd_str,
                    failed_count=1,
                    details=describe_command_failure(res),
                )
            )
            continue

        status, passed, failed, skipped, summary = parse_validation_output(
            combined, res.returncode, category=category
        )
        results.append(
            ValidationCheckResult(
                name=name,
                category=category,
                status=status,
                passed_count=passed,
                failed_count=failed,
                skipped_count=skipped,
                command=cmd_str,
                details=summary,
            )
        )
    return results


def _run_local_test_check(repo_path: str, execute_local_tests: bool) -> ValidationCheckResult:
    checks = _run_local_checks(repo_path, execute_local_tests=execute_local_tests)
    for chk in checks:
        if chk.category == "tests":
            return chk
    return checks[0]


def run_migration_validations(
    report: AuditReport,
    *,
    execute_local_tests: bool = True,
    now: datetime | None = None,
) -> ValidationReport:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    checks: list[ValidationCheckResult] = []
    reservations: list[str] = []

    # 1. Local check & test execution (stack-aware: Biome, Vitest/Node, Pytest/Unittest, RSpec)
    local_checks = _run_local_checks(report.repo_path, execute_local_tests=execute_local_tests)
    for chk in local_checks:
        checks.append(chk)
        if chk.status == STATUS_SKIPPED and chk.skipped_count > 0:
            reservations.append(
                f"{chk.name}: {chk.skipped_count} test(s) were SKIPPED (skipped tests never count as a full pass)."
            )
        elif chk.status == STATUS_SKIPPED:
            reservations.append(f"{chk.name}: {chk.details}")
        elif chk.status == STATUS_FAIL:
            reservations.append(f"{chk.name} FAILED: {chk.details}")

    # 2. GitHub Actions CI status
    ci = report.ci_summary
    gha_status = ci.github_actions_status if ci.github_actions_workflows else STATUS_UNKNOWN
    checks.append(
        ValidationCheckResult(
            name="GitHub Actions CI",
            category="ci",
            status=gha_status,
            details=ci.github_actions_details,
        )
    )
    if gha_status in {STATUS_UNKNOWN, STATUS_PENDING}:
        reservations.append(f"GitHub Actions status is {gha_status}: {ci.github_actions_details}")

    # 3. Jenkins CI status
    if ci.jenkins_file:
        checks.append(
            ValidationCheckResult(
                name="Jenkins Pipeline",
                category="ci",
                status=ci.jenkins_status,
                details=ci.jenkins_details,
            )
        )
        if ci.jenkins_status == STATUS_UNKNOWN:
            reservations.append("Jenkins pipeline status is UNKNOWN locally; verify build result on Jenkins.")

    # 4. Environment alignment check
    has_drift = any(r.drift_status == DRIFT_PROBLEMATIC for r in report.env_matrix)
    has_unknown_prod = any(r.production == STATUS_UNKNOWN for r in report.env_matrix)
    if has_drift:
        align_status = STATUS_FAIL
        align_details = "Problematic version drift detected across declared environments."
    elif has_unknown_prod:
        align_status = STATUS_UNKNOWN
        align_details = "Local/CI versions are aligned or accepted, but Production runtime version is UNKNOWN."
    else:
        align_status = STATUS_PASS
        align_details = "All declared environments are aligned or have accepted service differences."

    checks.append(
        ValidationCheckResult(
            name="Cross-Environment Version Alignment",
            category="alignment",
            status=align_status,
            details=align_details,
        )
    )

    # 5. Renovate Configuration check
    rc = report.renovate_config
    if not rc.exists or not rc.valid_json:
        ren_status = STATUS_FAIL
        ren_details = "; ".join(rc.issues) or "Renovate config is missing or invalid."
    elif not rc.extends_dev_platform:
        ren_status = STATUS_SKIPPED
        ren_details = "Custom renovate.json does not extend central Dev Platform preset."
    else:
        ren_status = STATUS_PASS
        ren_details = f"Valid {rc.path} inheriting Dev Platform preset."

    checks.append(
        ValidationCheckResult(
            name="Renovate Configuration & Preset",
            category="config",
            status=ren_status,
            details=ren_details,
        )
    )

    counts = {
        STATUS_PASS: 0,
        STATUS_FAIL: 0,
        STATUS_SKIPPED: 0,
        STATUS_PENDING: 0,
        STATUS_UNKNOWN: 0,
    }
    for chk in checks:
        counts[chk.status] = counts.get(chk.status, 0) + 1

    if counts[STATUS_FAIL] > 0:
        overall = STATUS_FAIL
    elif counts[STATUS_PENDING] > 0:
        overall = STATUS_PENDING
    elif counts[STATUS_SKIPPED] > 0:
        overall = f"{STATUS_SKIPPED} (REVIEW REQUIRED — NO FALSE PASS)"
    elif counts[STATUS_UNKNOWN] > 0:
        overall = f"{STATUS_UNKNOWN} (INCOMPLETE VERIFICATION)"
    else:
        overall = STATUS_PASS

    return ValidationReport(
        repo_name=report.repo_name,
        generated_at=timestamp,
        checks=checks,
        counts_by_status=counts,
        overall_status=overall,
        reservations=reservations,
    )
