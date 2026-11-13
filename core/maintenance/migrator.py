import difflib
import json
import re
from pathlib import Path

from core.config import DEFAULT_HEAD_BRANCH
from core.maintenance.detector import inspect_git_state
from core.maintenance.models import (
    DEV_PLATFORM_PRESET,
    DRIFT_PROBLEMATIC,
    AuditReport,
    MigrationApplicationResult,
    ProposedFileChange,
)
from utils.common import is_dry_run

JENKINS_PYTHON_TAG_RE = re.compile(r"(python:)([0-9]+(?:\.[0-9]+){0,2})(-[A-Za-z0-9.\-_]+)?")
JENKINS_NODE_TAG_RE = re.compile(r"(node:)([0-9]+(?:\.[0-9]+){0,2})(-[A-Za-z0-9.\-_]+)?")
JENKINS_PLAYWRIGHT_TAG_RE = re.compile(
    r"(mcr\.microsoft\.com/playwright:v?)([0-9]+(?:\.[0-9]+){0,2})(-[A-Za-z0-9.\-_]+)?"
)


def _unified_preview(path: str, old_text: str, new_text: str) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    return "\n".join(diff_lines) if diff_lines else f"(new file: {path})"


def verify_migration_git_preconditions(
    repo_path: str,
    *,
    expected_branch: str | None = None,
    require_clean_worktree: bool = False,
) -> tuple[bool, list[str]]:
    target_branch = (expected_branch or DEFAULT_HEAD_BRANCH).strip()
    git_state = inspect_git_state(repo_path)
    issues: list[str] = []

    if git_state.branch != target_branch:
        issues.append(
            f"Current branch is '{git_state.branch}', expected integration branch '{target_branch}'."
        )

    if require_clean_worktree and (git_state.staged_count > 0 or git_state.unstaged_count > 0):
        issues.append(
            f"Working tree has uncommitted tracked changes ({git_state.staged_count} staged, {git_state.unstaged_count} unstaged)."
        )

    return len(issues) == 0, issues


def _detect_private_npm_registry(repo_root: Path, report: AuditReport) -> bool:
    if report.renovate_config.has_private_registry:
        return True
    for candidate in (repo_root / ".npmrc", repo_root / "ui" / ".npmrc", repo_root / "frontend" / ".npmrc"):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8", errors="ignore")
                if "npm.pkg.github.com" in text or "@nlabrazi:registry" in text:
                    return True
            except OSError:
                pass
    for pkg_candidate in (repo_root / "package.json", repo_root / "ui" / "package.json", repo_root / "frontend" / "package.json"):
        if pkg_candidate.is_file():
            try:
                text = pkg_candidate.read_text(encoding="utf-8", errors="ignore")
                if "@nlabrazi/" in text:
                    return True
            except OSError:
                pass
    return False


def _propose_renovate_config_change(repo_root: Path, report: AuditReport) -> ProposedFileChange | None:
    rc = report.renovate_config
    if (repo_root / "configs" / "renovate" / "base.json").is_file():
        return None

    needs_private_npmrc = _detect_private_npm_registry(repo_root, report)
    target_path = repo_root / rc.path
    if not rc.exists:
        payload: dict[str, object] = {
            "$schema": "https://docs.renovatebot.com/renovate-schema.json",
            "extends": [DEV_PLATFORM_PRESET],
        }
        if needs_private_npmrc:
            payload["npmrc"] = "@nlabrazi:registry=https://npm.pkg.github.com/"
        new_text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        reason = "Initialize Renovate configuration inheriting central Dev Platform preset"
        if needs_private_npmrc:
            reason += " (with @nlabrazi GitHub Packages registry)"
        return ProposedFileChange(
            path=rc.path,
            action="create",
            reason=reason + ".",
            preview_diff=_unified_preview(rc.path, "", new_text),
            new_content=new_text,
        )

    if rc.valid_json and target_path.is_file():
        try:
            old_text = target_path.read_text(encoding="utf-8")
            data = json.loads(old_text)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None

        changed = False
        extends = list(data.get("extends")) if isinstance(data.get("extends"), list) else []
        if DEV_PLATFORM_PRESET not in extends:
            extends = [DEV_PLATFORM_PRESET] + [e for e in extends if e != "config:recommended"]
            data["extends"] = extends
            changed = True

        if needs_private_npmrc and "npmrc" not in data:
            data["npmrc"] = "@nlabrazi:registry=https://npm.pkg.github.com/"
            changed = True

        if changed:
            new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
            if new_text != old_text:
                return ProposedFileChange(
                    path=rc.path,
                    action="update",
                    reason="Inherit central Dev Platform Renovate preset while preserving local overrides.",
                    preview_diff=_unified_preview(rc.path, old_text, new_text),
                    new_content=new_text,
                )
    return None


def _propose_git_exclude_change(repo_root: Path, report: AuditReport) -> ProposedFileChange | None:
    untracked_venvs = report.git_state.untracked_venv_entries
    if not untracked_venvs:
        return None

    exclude_path = repo_root / ".git" / "info" / "exclude"
    old_text = ""
    if exclude_path.is_file():
        try:
            old_text = exclude_path.read_text(encoding="utf-8")
        except OSError:
            old_text = ""

    existing_lines = {line.strip() for line in old_text.splitlines() if line.strip() and not line.strip().startswith("#")}
    to_add: list[str] = []
    for entry in untracked_venvs:
        clean_entry = entry.rstrip("/")
        for candidate in (clean_entry, f"{clean_entry}/"):
            if candidate not in existing_lines and candidate not in to_add:
                to_add.append(candidate)

    if not to_add:
        return None

    base = old_text if old_text.endswith("\n") or not old_text else old_text + "\n"
    new_text = base + "\n".join(to_add) + "\n"
    return ProposedFileChange(
        path=".git/info/exclude",
        action="exclude",
        reason="Ignore local virtualenv symlink/backup directories without modifying .gitignore.",
        preview_diff=_unified_preview(".git/info/exclude", old_text, new_text),
        new_content=new_text,
    )


def _propose_jenkinsfile_alignment(repo_root: Path, report: AuditReport) -> ProposedFileChange | None:
    jenkinsfile = repo_root / "Jenkinsfile"
    if not jenkinsfile.is_file():
        return None

    try:
        old_text = jenkinsfile.read_text(encoding="utf-8")
    except OSError:
        return None

    new_text = old_text
    reasons: list[str] = []
    target_map = report.migration_plan.target_versions if report.migration_plan else {}

    for row in report.env_matrix:
        if row.component == "Python" and row.drift_status == DRIFT_PROBLEMATIC:
            target_py = target_map.get("Python")
            if target_py and target_py not in {"UNKNOWN", "N/A"}:
                updated = JENKINS_PYTHON_TAG_RE.sub(
                    lambda m: f"{m.group(1)}{target_py}{m.group(3) or ''}",
                    new_text,
                )
                if updated != new_text:
                    new_text = updated
                    reasons.append(f"Align Jenkins Python image to {target_py}")

        if row.component == "Node.js" and row.drift_status == DRIFT_PROBLEMATIC:
            target_node = target_map.get("Node.js")
            if target_node and target_node not in {"UNKNOWN", "N/A"}:
                updated = JENKINS_NODE_TAG_RE.sub(
                    lambda m: f"{m.group(1)}{target_node}{m.group(3) or ''}",
                    new_text,
                )
                if updated != new_text:
                    new_text = updated
                    reasons.append(f"Align Jenkins Node.js image to {target_node}")

    # Also check Playwright drift (from EnvironmentMatrix or open Renovate PR)
    playwright_target: str | None = None
    for pr in report.open_prs:
        for upd in pr.updates:
            if "playwright" in upd.package.lower() and upd.to_version:
                playwright_target = upd.to_version.lstrip("v^~")
                break
    if not playwright_target:
        for row in report.env_matrix:
            if row.component == "Playwright" and row.drift_status == DRIFT_PROBLEMATIC:
                playwright_target = target_map.get("Playwright")

    if playwright_target and playwright_target not in {"UNKNOWN", "N/A"}:
        updated = JENKINS_PLAYWRIGHT_TAG_RE.sub(
            lambda m: f"{m.group(1)}{playwright_target}{m.group(3) or ''}",
            new_text,
        )
        if updated != new_text:
            new_text = updated
            reasons.append(f"Align Jenkins Playwright image to v{playwright_target}")

    if new_text == old_text:
        return None

    return ProposedFileChange(
        path="Jenkinsfile",
        action="update",
        reason="; ".join(reasons),
        preview_diff=_unified_preview("Jenkinsfile", old_text, new_text),
        new_content=new_text,
    )


def _propose_deploy_script_uv_fix(repo_root: Path, report: AuditReport) -> ProposedFileChange | None:
    gs = report.git_state
    if not (gs.venv_exists and gs.venv_has_pip is False):
        return None

    for rel_path in ("deploy.sh", "scripts/deploy.sh"):
        candidate = repo_root / rel_path
        if not candidate.is_file():
            continue
        try:
            old_text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        if "uv pip" in old_text:
            continue
        new_text = re.sub(
            r"(^|\s)(?:\"?\$?[A-Za-z0-9_/.]*pip\"?)\s+install\s+-r\s+([^\s]+)",
            r'\1"$HOME/.local/bin/uv" pip install --python "${VENV_DIR:-.venv}/bin/python" -r \2',
            old_text,
            flags=re.MULTILINE,
        )
        if new_text != old_text:
            return ProposedFileChange(
                path=rel_path,
                action="update",
                reason="Replace direct 'pip install' with 'uv pip install --python' for pip-less virtualenv.",
                preview_diff=_unified_preview(rel_path, old_text, new_text),
                new_content=new_text,
            )
    return None


def prepare_migration_changes(report: AuditReport) -> list[ProposedFileChange]:
    repo_root = Path(report.repo_path)
    proposals: list[ProposedFileChange] = []

    for builder in (
        _propose_renovate_config_change,
        _propose_git_exclude_change,
        _propose_jenkinsfile_alignment,
        _propose_deploy_script_uv_fix,
    ):
        proposal = builder(repo_root, report)
        if proposal is not None:
            proposals.append(proposal)

    return proposals


def apply_migration_changes(
    repo_path: str,
    changes: list[ProposedFileChange],
    *,
    expected_branch: str | None = None,
    require_clean_worktree: bool = False,
) -> MigrationApplicationResult:
    if not changes:
        return MigrationApplicationResult(
            status="no-op",
            messages=["No migration file changes to apply."],
        )

    ok, precondition_issues = verify_migration_git_preconditions(
        repo_path,
        expected_branch=expected_branch,
        require_clean_worktree=require_clean_worktree,
    )
    if not ok:
        return MigrationApplicationResult(
            status="aborted",
            skipped_files=[c.path for c in changes],
            messages=precondition_issues,
        )

    if is_dry_run():
        return MigrationApplicationResult(
            status="dry-run",
            applied_files=[c.path for c in changes],
            messages=[f"[DRY-RUN] Simulated {len(changes)} file change(s) without writing to disk."],
        )

    repo_root = Path(repo_path).resolve()
    applied: list[str] = []
    skipped: list[str] = []
    messages: list[str] = []

    for change in changes:
        target = (repo_root / change.path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            skipped.append(change.path)
            messages.append(f"Rejected path outside repository root: {change.path}")
            continue

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.new_content, encoding="utf-8")
            applied.append(change.path)
            messages.append(f"Applied ({change.action}): {change.path}")
        except OSError as error:
            skipped.append(change.path)
            messages.append(f"Failed to write {change.path}: {error}")

    status = "applied" if applied else "aborted"
    return MigrationApplicationResult(
        status=status,
        applied_files=applied,
        skipped_files=skipped,
        messages=messages,
    )
