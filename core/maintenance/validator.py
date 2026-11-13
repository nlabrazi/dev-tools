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


def _detect_test_command(repo_root: Path) -> list[str] | None:
    venv_py = repo_root / ".venv" / "bin" / "python"
    py_bin = str(venv_py) if venv_py.is_file() else "python3"

    if (repo_root / "tests").is_dir() or any(repo_root.glob("test_*.py")):
        return [py_bin, "-m", "unittest", "discover", "-v"]

    pkg_json = repo_root / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else {}
        if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
            return ["npm", "test"]
    return None


def _run_local_test_check(repo_path: str, execute_local_tests: bool) -> ValidationCheckResult:
    repo_root = Path(repo_path)
    cmd = _detect_test_command(repo_root)
    if cmd is None:
        return ValidationCheckResult(
            name="Local Unit / Integration Tests",
            category="tests",
            status=STATUS_UNKNOWN,
            details="No standard local test runner detected in repository.",
        )

    cmd_str = " ".join(cmd)
    if not execute_local_tests or is_dry_run():
        reason = (
            "[DRY-RUN] Local test execution skipped in dry-run mode."
            if is_dry_run()
            else "Local test execution skipped by user."
        )
        return ValidationCheckResult(
            name="Local Unit / Integration Tests",
            category="tests",
            status=STATUS_SKIPPED,
            command=cmd_str,
            details=reason,
        )

    res = run_command(cmd, cwd=repo_path, silent=True, timeout=180)
    combined = f"{res.stdout or ''}\n{res.stderr or ''}".strip()
    if res.returncode == 124:
        return ValidationCheckResult(
            name="Local Unit / Integration Tests",
            category="tests",
            status=STATUS_FAIL,
            command=cmd_str,
            failed_count=1,
            details=describe_command_failure(res),
        )

    status, passed, failed, skipped, summary = parse_python_test_output(combined, res.returncode)
    return ValidationCheckResult(
        name="Local Unit / Integration Tests",
        category="tests",
        status=status,
        passed_count=passed,
        failed_count=failed,
        skipped_count=skipped,
        command=cmd_str,
        details=summary,
    )


def run_migration_validations(
    report: AuditReport,
    *,
    execute_local_tests: bool = True,
    now: datetime | None = None,
) -> ValidationReport:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    checks: list[ValidationCheckResult] = []
    reservations: list[str] = []

    # 1. Local test execution
    test_check = _run_local_test_check(report.repo_path, execute_local_tests=execute_local_tests)
    checks.append(test_check)
    if test_check.status == STATUS_SKIPPED and test_check.skipped_count > 0:
        reservations.append(
            f"{test_check.skipped_count} test(s) were SKIPPED during local test execution (skipped tests are never treated as a full pass)."
        )
    elif test_check.status == STATUS_SKIPPED:
        reservations.append(test_check.details)

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
