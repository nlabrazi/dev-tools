from datetime import datetime
import os
import re
from pathlib import Path

from core.maintenance.models import (
    RISK_BLOCKED,
    RISK_LOW,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_PENDING,
    STATUS_UNKNOWN,
    AuditReport,
    DeploymentChecklist,
    DeploymentChecklistItem,
    PostDeployVerificationReport,
    ProductionEnvironmentInfo,
)
from utils.common import run_command

PYVENV_VERSION_RE = re.compile(r"^\s*version(?:_info)?\s*=\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
VENV_NAME_VERSION_RE = re.compile(r"\.venv-([0-9])([0-9]{1,2})\b")
RAW_PIP_INSTALL_RE = re.compile(r"(?<!uv\s)\bpip3?\s+install\b")
UV_PIP_INSTALL_RE = re.compile(r"\buv(?:\"|'|\s)+pip\s+install\b")


def _extract_python_version_from_venv(venv_dir: Path, target_name: str | None) -> str | None:
    cfg = venv_dir / "pyvenv.cfg"
    if cfg.is_file():
        try:
            content = cfg.read_text(encoding="utf-8", errors="ignore")
            match = PYVENV_VERSION_RE.search(content)
            if match:
                return match.group(1)
        except OSError:
            pass

    if target_name:
        match = VENV_NAME_VERSION_RE.search(target_name)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return None


def _inspect_local_production_path(app_dir: Path) -> ProductionEnvironmentInfo:
    if not app_dir.exists() or not app_dir.is_dir():
        return ProductionEnvironmentInfo(
            configured=True,
            reachable=False,
            access_mode="local_path",
            app_path=str(app_dir),
            issues=[f"Configured production directory '{app_dir}' does not exist or is unreachable."],
            details="Production path not found.",
        )

    issues: list[str] = []
    runtime_versions: dict[str, str] = {}

    venv_entry = app_dir / ".venv"
    venv_path: str | None = None
    venv_is_symlink = False
    venv_target: str | None = None
    venv_has_pip: bool | None = None

    if venv_entry.exists() or venv_entry.is_symlink():
        venv_path = str(venv_entry)
        venv_is_symlink = venv_entry.is_symlink()
        if venv_is_symlink:
            try:
                venv_target = os.readlink(venv_entry)
            except OSError:
                venv_target = None
        resolved_venv = (app_dir / venv_target) if venv_target else venv_entry
        pip_bin = resolved_venv / "bin" / "pip"
        py_bin = resolved_venv / "bin" / "python"
        if py_bin.exists() or (resolved_venv / "bin").is_dir():
            venv_has_pip = pip_bin.exists()
        py_ver = _extract_python_version_from_venv(resolved_venv, venv_target)
        if py_ver:
            runtime_versions["Python"] = py_ver

    backup_venvs: list[str] = []
    active_target_name = Path(venv_target).name if venv_target else ""
    try:
        for child in sorted(app_dir.iterdir(), key=lambda p: p.name):
            if not child.name.startswith(".venv") or child.name == ".venv":
                continue
            if child.name == active_target_name:
                continue
            if child.is_dir() or child.is_symlink():
                backup_venvs.append(child.name)
    except OSError:
        pass

    if venv_path and not backup_venvs:
        issues.append(
            f"No backup virtualenv (.venv-*-backup) detected in {app_dir} for instant symlink rollback."
        )

    deploy_script_path: str | None = None
    deploy_uses_uv_pip: bool | None = None
    deploy_candidate = app_dir / "deploy.sh"
    if deploy_candidate.is_file():
        deploy_script_path = str(deploy_candidate)
        try:
            script_text = deploy_candidate.read_text(encoding="utf-8", errors="ignore")
            uses_uv = bool(UV_PIP_INSTALL_RE.search(script_text))
            uses_raw_pip = bool(RAW_PIP_INSTALL_RE.search(script_text)) and not uses_uv
            deploy_uses_uv_pip = uses_uv
            if venv_has_pip is False and uses_raw_pip:
                issues.append(
                    "Production deploy.sh uses raw 'pip install' while production .venv has no pip binary; use 'uv pip install --python .venv/bin/python'."
                )
        except OSError:
            pass

    return ProductionEnvironmentInfo(
        configured=True,
        reachable=True,
        access_mode="local_path",
        app_path=str(app_dir),
        runtime_versions=runtime_versions,
        venv_path=venv_path,
        venv_is_symlink=venv_is_symlink,
        venv_target=venv_target,
        venv_has_pip=venv_has_pip,
        backup_venvs=backup_venvs,
        deploy_script_path=deploy_script_path,
        deploy_uses_uv_pip=deploy_uses_uv_pip,
        issues=issues,
        details=f"Inspected configured production directory at {app_dir}.",
    )


def _inspect_ssh_production(host: str, app_path: str) -> ProductionEnvironmentInfo:
    probe_script = (
        f"if [ ! -d '{app_path}' ]; then echo 'REACHABLE=0'; exit 0; fi; "
        "echo 'REACHABLE=1'; "
        f"if [ -L '{app_path}/.venv' ]; then echo 'VENV_SYMLINK=1'; echo \"VENV_TARGET=$(readlink '{app_path}/.venv')\"; "
        f"elif [ -d '{app_path}/.venv' ]; then echo 'VENV_SYMLINK=0'; fi; "
        f"if [ -x '{app_path}/.venv/bin/python' ]; then "
        f"echo \"PYTHON_VERSION=$('{app_path}/.venv/bin/python' -c 'import platform; print(platform.python_version())' 2>/dev/null)\"; "
        f"if [ -x '{app_path}/.venv/bin/pip' ]; then echo 'VENV_HAS_PIP=1'; else echo 'VENV_HAS_PIP=0'; fi; "
        "fi; "
        f"echo \"BACKUP_VENVS=$(find '{app_path}' -maxdepth 1 -name '.venv*backup*' -printf '%f,' 2>/dev/null)\""
    )
    res = run_command(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, probe_script],
        silent=True,
    )
    if res.returncode != 0:
        err = ((res.stderr or "") + "\n" + (res.stdout or "")).strip() or f"exit code {res.returncode}"
        return ProductionEnvironmentInfo(
            configured=True,
            reachable=False,
            access_mode="ssh",
            app_path=app_path,
            issues=[f"Production SSH host '{host}' could not be reached ({err})."],
            details=f"SSH probe to {host}:{app_path} failed; production runtime versions remain UNKNOWN.",
        )

    kv: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    if kv.get("REACHABLE") != "1":
        return ProductionEnvironmentInfo(
            configured=True,
            reachable=False,
            access_mode="ssh",
            app_path=app_path,
            issues=[f"Application directory '{app_path}' not found on SSH host '{host}'."],
            details=f"Directory {app_path} missing on {host}.",
        )

    runtime_versions: dict[str, str] = {}
    if kv.get("PYTHON_VERSION"):
        runtime_versions["Python"] = kv["PYTHON_VERSION"]
    if kv.get("POSTGRES_VERSION"):
        runtime_versions["PostgreSQL"] = kv["POSTGRES_VERSION"]

    venv_symlink = kv.get("VENV_SYMLINK") == "1"
    venv_target = kv.get("VENV_TARGET") or None
    venv_has_pip = None
    if "VENV_HAS_PIP" in kv:
        venv_has_pip = kv["VENV_HAS_PIP"] == "1"

    backups = [b for b in (kv.get("BACKUP_VENVS") or "").split(",") if b]
    issues: list[str] = []
    if not backups:
        issues.append(f"No backup virtualenv (.venv*backup*) detected on {host}:{app_path}.")

    return ProductionEnvironmentInfo(
        configured=True,
        reachable=True,
        access_mode="ssh",
        app_path=app_path,
        runtime_versions=runtime_versions,
        venv_path=f"{app_path}/.venv" if "VENV_SYMLINK" in kv else None,
        venv_is_symlink=venv_symlink,
        venv_target=venv_target,
        venv_has_pip=venv_has_pip,
        backup_venvs=backups,
        issues=issues,
        details=f"Inspected production via SSH ({host}:{app_path}).",
    )


def inspect_production_environment(
    repo_name: str,
    *,
    deploy_root: str | None = None,
    ssh_host: str | None = None,
) -> ProductionEnvironmentInfo:
    effective_deploy_root = (
        deploy_root
        if deploy_root is not None
        else os.environ.get("DEVTOOLS_PRODUCTION_DEPLOY_ROOT", "").strip()
    )
    effective_ssh_host = (
        ssh_host
        if ssh_host is not None
        else os.environ.get("DEVTOOLS_PRODUCTION_SSH_HOST", "").strip()
    )

    default_app_path = f"/srv/apps/{repo_name}"

    if effective_deploy_root:
        return _inspect_local_production_path(Path(effective_deploy_root) / repo_name)

    if effective_ssh_host:
        return _inspect_ssh_production(effective_ssh_host, default_app_path)

    return ProductionEnvironmentInfo(
        configured=False,
        reachable=False,
        access_mode="unconfigured",
        app_path=default_app_path,
        details="Production access not configured (set DEVTOOLS_PRODUCTION_SSH_HOST or DEVTOOLS_PRODUCTION_DEPLOY_ROOT).",
    )


def prepare_deployment_and_rollback(
    report: AuditReport,
    prod_info: ProductionEnvironmentInfo | None = None,
    *,
    now: datetime | None = None,
) -> DeploymentChecklist:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    effective_prod = prod_info or report.production_info or inspect_production_environment(report.repo_name)

    items: list[DeploymentChecklistItem] = []
    blockers: list[str] = []

    if report.git_state.is_clean:
        items.append(
            DeploymentChecklistItem(
                name="Git Working Tree",
                status=STATUS_PASS,
                details=f"Branch '{report.git_state.branch}' is clean.",
            )
        )
    else:
        msg = (
            f"Working tree on '{report.git_state.branch}' has uncommitted changes "
            f"({report.git_state.staged_count} staged, {report.git_state.unstaged_count} unstaged)."
        )
        items.append(DeploymentChecklistItem(name="Git Working Tree", status=STATUS_FAIL, details=msg))
        blockers.append(msg)

    gha = report.ci_summary.github_actions_status
    jks = report.ci_summary.jenkins_status
    if gha == STATUS_FAIL or jks == STATUS_FAIL:
        msg = f"CI failure detected (GitHub Actions: {gha}, Jenkins: {jks})."
        items.append(DeploymentChecklistItem(name="CI Pipeline Status", status=STATUS_FAIL, details=msg))
        blockers.append(msg)
    elif gha == STATUS_PASS and jks in {STATUS_PASS, "N/A"}:
        items.append(
            DeploymentChecklistItem(
                name="CI Pipeline Status",
                status=STATUS_PASS,
                details=f"GitHub Actions: {gha}, Jenkins: {jks}.",
            )
        )
    else:
        items.append(
            DeploymentChecklistItem(
                name="CI Pipeline Status",
                status=STATUS_PENDING,
                details=f"GitHub Actions: {gha}, Jenkins: {jks} (verify CI is green before production deploy).",
            )
        )

    blocked_prs = [
        pr
        for pr in report.open_prs
        if pr.ci_status == STATUS_FAIL or (pr.analysis and pr.analysis.risk_level == RISK_BLOCKED)
    ]
    if blocked_prs:
        nums = ", ".join(f"#{pr.number}" for pr in blocked_prs)
        msg = f"Blocked or failing Renovate PR(s) open: {nums}."
        items.append(DeploymentChecklistItem(name="Open Renovate PR Risk", status=STATUS_FAIL, details=msg))
        blockers.append(msg)
    elif not report.open_prs or all(pr.analysis and pr.analysis.risk_level == RISK_LOW for pr in report.open_prs):
        items.append(
            DeploymentChecklistItem(
                name="Open Renovate PR Risk",
                status=STATUS_PASS,
                details=f"{len(report.open_prs)} open Renovate PR(s), no high-risk blockers.",
            )
        )
    else:
        items.append(
            DeploymentChecklistItem(
                name="Open Renovate PR Risk",
                status=STATUS_PENDING,
                details=f"{len(report.open_prs)} open Renovate PR(s) require review before promotion.",
            )
        )

    if not effective_prod.configured:
        items.append(
            DeploymentChecklistItem(
                name="Production Environment & Backup Virtualenv",
                status=STATUS_UNKNOWN,
                details="Production access not configured; verify backup virtualenv manually on VPS before deploying.",
            )
        )
    elif not effective_prod.reachable:
        msg = f"Configured production environment ({effective_prod.app_path}) is unreachable."
        items.append(
            DeploymentChecklistItem(
                name="Production Environment & Backup Virtualenv",
                status=STATUS_FAIL,
                details=msg,
            )
        )
        blockers.append(msg)
    elif effective_prod.venv_path and not effective_prod.backup_venvs:
        items.append(
            DeploymentChecklistItem(
                name="Production Environment & Backup Virtualenv",
                status=STATUS_PENDING,
                details=f"Create a backup virtualenv in {effective_prod.app_path} before switching .venv symlink.",
            )
        )
    else:
        backups_label = ", ".join(effective_prod.backup_venvs) if effective_prod.backup_venvs else "N/A"
        items.append(
            DeploymentChecklistItem(
                name="Production Environment & Backup Virtualenv",
                status=STATUS_PASS,
                details=f"Reachable at {effective_prod.app_path}; backups: {backups_label}.",
            )
        )

    if effective_prod.venv_has_pip is False and effective_prod.deploy_uses_uv_pip is False:
        msg = "Production deploy.sh uses raw 'pip install' while .venv has no pip binary; use 'uv pip install --python .venv/bin/python'."
        items.append(DeploymentChecklistItem(name="Deploy Script Compatibility", status=STATUS_FAIL, details=msg))
        blockers.append(msg)
    else:
        items.append(
            DeploymentChecklistItem(
                name="Deploy Script Compatibility",
                status=STATUS_PASS,
                details="Deployment script is compatible with detected virtualenv setup.",
            )
        )

    safety_rules = [
        "Never deploy automatically without explicit human confirmation.",
        "Never modify system Debian Python (/usr/bin/python3).",
        "Never delete backup virtualenvs (.venv-*-backup) without explicit confirmation.",
        "Never run destructive database migrations or major DB upgrades automatically.",
    ]

    rollback_steps: list[str] = []
    if effective_prod.backup_venvs:
        backup_target = effective_prod.backup_venvs[0]
        rollback_steps.append(
            f"1. Restore previous virtualenv symlink: ln -sfn {backup_target} {effective_prod.app_path}/.venv"
        )
        rollback_steps.append("2. Restart application service / workers and verify health check.")
    elif report.migration_plan is not None:
        rollback_steps.extend(report.migration_plan.rollback_plan.steps)
    else:
        rollback_steps.append("1. Revert the merge commit on base branch and re-run deploy.sh.")

    return DeploymentChecklist(
        repo_name=report.repo_name,
        generated_at=timestamp,
        ready_to_deploy=len(blockers) == 0,
        items=items,
        safety_rules=safety_rules,
        rollback_steps=rollback_steps,
        blockers=blockers,
    )


def verify_post_deployment(
    repo_name: str,
    prod_info: ProductionEnvironmentInfo | None = None,
    *,
    service_status: str = STATUS_UNKNOWN,
    database_status: str = STATUS_UNKNOWN,
    sentinel_cron_verified: bool = False,
    now: datetime | None = None,
) -> PostDeployVerificationReport:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    notes: list[str] = []

    if prod_info is not None and not prod_info.configured:
        notes.append("Production access is not configured locally; runtime statuses require manual or Sentinel check.")

    if sentinel_cron_verified:
        scheduled_status = STATUS_PASS
        notes.append("Scheduled jobs / cron execution confirmed via Sentinel.")
    else:
        scheduled_status = "PENDING VERIFICATION"
        notes.append(
            "Scheduled jobs: PENDING VERIFICATION — wait for the next real scheduled cron run and confirm via Sentinel before closing maintenance."
        )

    if service_status == STATUS_FAIL or database_status == STATUS_FAIL:
        overall = STATUS_FAIL
    elif (
        service_status == STATUS_PASS
        and database_status in {STATUS_PASS, "N/A"}
        and sentinel_cron_verified
    ):
        overall = "VERIFIED IN PRODUCTION"
    else:
        overall = "AWAITING FINAL RUNTIME / CRON CHECK"

    return PostDeployVerificationReport(
        repo_name=repo_name,
        generated_at=timestamp,
        service_status=service_status,
        database_status=database_status,
        scheduled_jobs_status=scheduled_status,
        sentinel_verified=sentinel_cron_verified,
        overall_status=overall,
        notes=notes,
    )
