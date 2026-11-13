from pathlib import Path

from rich.console import Console

from core.commit import auto_commit_all_repos
from core.config import DEFAULT_HEAD_BRANCH
from core.maintenance.detector import inspect_git_state
from core.maintenance.migrator import (
    apply_migration_changes,
    prepare_migration_changes,
)
from core.maintenance.models import (
    RISK_BLOCKED,
    STATUS_FAIL,
    AuditReport,
    RenovatePRInfo,
)
from core.maintenance.reporter import (
    render_migration_application_result,
    render_proposed_changes_preview,
)
from core.merge import main as merge_all_repos
from core.sync import checkout_branch, ensure_local_branch_exists, pull_ff_only
from utils.common import is_dry_run, run_command
from utils.console import ask_yes_no, ui_panel

console = Console()


def merge_renovate_pr_to_staging(
    repo_path: str,
    repo_name: str,
    pr: RenovatePRInfo,
    *,
    sync_after_merge: bool = True,
    target_console: Console | None = None,
) -> str:
    out = target_console or console

    if pr.ci_status == STATUS_FAIL:
        out.print(
            ui_panel(
                f"Refusing to merge PR #{pr.number} ({pr.title}): CI status is FAIL.",
                title="Merge Blocked · CI Failure",
                tone="danger",
                compact=True,
            )
        )
        return "blocked"

    if pr.analysis is not None and pr.analysis.risk_level == RISK_BLOCKED:
        out.print(
            ui_panel(
                f"Refusing to merge PR #{pr.number} ({pr.title}): Risk level is BLOCKED.",
                title="Merge Blocked · Risk Assessment",
                tone="danger",
                compact=True,
            )
        )
        return "blocked"

    if pr.base_branch != DEFAULT_HEAD_BRANCH and not pr.is_onboarding:
        out.print(
            ui_panel(
                (
                    f"Refusing to merge non-onboarding PR #{pr.number} targeting '{pr.base_branch}'. "
                    f"Renovate dependency PRs must target '{DEFAULT_HEAD_BRANCH}'."
                ),
                title="Merge Blocked · Target Branch Policy",
                tone="danger",
                compact=True,
            )
        )
        return "blocked"

    if not ask_yes_no(
        f"Merge Renovate PR #{pr.number} ({pr.title}) into {pr.base_branch}?",
        default="n",
    ):
        return "skipped"

    if is_dry_run():
        out.print(
            ui_panel(
                f"Dry-run: would merge PR #{pr.number} ({pr.head_branch} → {pr.base_branch}) in {repo_name}.",
                title="Renovate PR Merge (Dry-Run)",
                tone="info",
                compact=True,
            )
        )
        return "dry-run"

    res = run_command(
        ["gh", "pr", "merge", str(pr.number), "--merge"],
        cwd=repo_path,
        silent=True,
    )
    if res.returncode != 0:
        details = ((res.stderr or "") + "\n" + (res.stdout or "")).strip() or f"exit code {res.returncode}"
        out.print(
            ui_panel(
                f"Failed to merge PR #{pr.number}:\n{details}",
                title="Renovate PR Merge Failed",
                tone="danger",
                compact=True,
            )
        )
        return "failed"

    out.print(
        ui_panel(
            f"Merged PR #{pr.number} ({pr.title}) into {pr.base_branch}.",
            title="Renovate PR Merged",
            tone="success",
            compact=True,
        )
    )

    if sync_after_merge and pr.base_branch == DEFAULT_HEAD_BRANCH:
        if ask_yes_no(f"Pull fast-forward {pr.base_branch} locally after merging PR #{pr.number}?", default="y"):
            pull_ff_only(repo_path, repo_name, pr.base_branch)

    return "merged"


def run_maintenance_auto_commit(
    repo_path: str,
    repo_name: str,
    *,
    target_console: Console | None = None,
) -> dict[str, int] | None:
    out = target_console or console
    git_state = inspect_git_state(repo_path)

    if git_state.branch != DEFAULT_HEAD_BRANCH:
        out.print(
            ui_panel(
                (
                    f"Current branch is '{git_state.branch}', expected '{DEFAULT_HEAD_BRANCH}'. "
                    f"Direct commits to '{git_state.branch}' are blocked during maintenance."
                ),
                title="Auto-Commit Blocked",
                tone="danger",
                compact=True,
            )
        )
        return None

    if not ask_yes_no(
        f"Launch Auto-Commit & Push workflow for {repo_name} on {DEFAULT_HEAD_BRANCH}?",
        default="n",
    ):
        return None

    parent_dir = str(Path(repo_path).resolve().parent)
    return auto_commit_all_repos([parent_dir], selected_repo_path=str(Path(repo_path).resolve()))


def run_maintenance_promote_to_base(
    repo_path: str,
    repo_name: str,
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    git_state = inspect_git_state(repo_path)

    if not git_state.is_clean:
        out.print(
            ui_panel(
                (
                    f"Working tree is not clean ({git_state.staged_count} staged, "
                    f"{git_state.unstaged_count} unstaged, {git_state.untracked_count} untracked). "
                    "Commit or clean changes before promoting staging to the base branch."
                ),
                title="Promotion Blocked · Dirty Working Tree",
                tone="danger",
                compact=True,
            )
        )
        return "blocked"

    base_label = git_state.base_branch or "base"
    if not ask_yes_no(
        f"Launch Auto-Merge / PR workflow ({DEFAULT_HEAD_BRANCH} → {base_label}) for {repo_name}?",
        default="n",
    ):
        return "skipped"

    parent_dir = str(Path(repo_path).resolve().parent)
    merge_all_repos([parent_dir], selected_repo_path=str(Path(repo_path).resolve()))
    return "dry-run" if is_dry_run() else "executed"


def run_renovate_onboarding_workflow(
    repo_path: str,
    repo_name: str,
    report: AuditReport,
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    git_state = inspect_git_state(repo_path)

    if git_state.branch != DEFAULT_HEAD_BRANCH:
        if git_state.staged_count > 0 or git_state.unstaged_count > 0:
            out.print(
                ui_panel(
                    (
                        f"Vous êtes sur '{git_state.branch}' avec des modifications non commitées. "
                        f"Commitez ou stashez vos changements avant de basculer sur '{DEFAULT_HEAD_BRANCH}'."
                    ),
                    title="Onboarding Renovate Bloqué · Modifications en cours",
                    tone="danger",
                    compact=True,
                )
            )
            return "blocked"

        if not ask_yes_no(
            f"Le dépôt est sur '{git_state.branch}'. Basculer sur '{DEFAULT_HEAD_BRANCH}' et synchroniser ?",
            default="y",
        ):
            return "skipped"

        if ensure_local_branch_exists(repo_path, repo_name, DEFAULT_HEAD_BRANCH) == "failed":
            return "failed"
        if checkout_branch(repo_path, repo_name, DEFAULT_HEAD_BRANCH) == "failed":
            return "failed"
        pull_ff_only(repo_path, repo_name, DEFAULT_HEAD_BRANCH)

    onboarding_prs = [pr for pr in report.open_prs if pr.is_onboarding]
    if onboarding_prs:
        pr_nums = ", ".join(f"#{pr.number} ({pr.title} → {pr.base_branch})" for pr in onboarding_prs)
        out.print(
            ui_panel(
                (
                    f"PR d'onboarding Renovate détectée : {pr_nums}.\n"
                    f"En déployant notre 'renovate.json' (preset Dev Platform) via '{DEFAULT_HEAD_BRANCH} → {git_state.base_branch or 'master'}', "
                    "Renovate adoptera directement votre configuration centrale."
                ),
                title="PR d'Onboarding Renovate Détectée",
                tone="info",
                compact=True,
            )
        )

    proposals = prepare_migration_changes(report)
    render_proposed_changes_preview(proposals, target_console=out)

    if not proposals:
        out.print(
            ui_panel(
                "Renovate et les fichiers CI/déploiement sont déjà alignés sur ce dépôt.",
                title="Onboarding & Alignement Renovate",
                tone="success",
                compact=True,
            )
        )
        return "no-op"

    if not ask_yes_no(
        f"Appliquer ces {len(proposals)} adaptation(s) sur '{DEFAULT_HEAD_BRANCH}' ?",
        default="y",
    ):
        return "skipped"

    app_result = apply_migration_changes(
        repo_path,
        proposals,
        expected_branch=None if is_dry_run() else DEFAULT_HEAD_BRANCH,
    )
    render_migration_application_result(app_result, target_console=out)
    if app_result.status == "aborted":
        return "aborted"

    tracked_changes = [p for p in proposals if p.path != ".git/info/exclude"]
    if tracked_changes:
        run_maintenance_auto_commit(repo_path, repo_name, target_console=out)
        run_maintenance_promote_to_base(repo_path, repo_name, target_console=out)

    return "dry-run" if is_dry_run() else "completed"
