from datetime import datetime

from rich.console import Console
from rich.prompt import Prompt

from core.maintenance.analyzer import classify_repository_update_risks
from core.maintenance.detector import (
    detect_project_stacks,
    inspect_git_state,
    inspect_renovate_config,
)
from core.maintenance.environments import build_environment_matrix, collect_declared_versions
from core.maintenance.git_ops import (
    merge_renovate_pr_to_staging,
    run_maintenance_auto_commit,
    run_maintenance_promote_to_base,
)
from core.maintenance.migrator import (
    apply_migration_changes,
    prepare_migration_changes,
    verify_migration_git_preconditions,
)
from core.maintenance.models import (
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
    STATUS_UNKNOWN,
    AuditReport,
)
from core.maintenance.planner import generate_migration_plan
from core.maintenance.production import (
    inspect_production_environment,
    prepare_deployment_and_rollback,
    verify_post_deployment,
)
from core.maintenance.renovate import (
    inspect_ci_status,
    inspect_dependency_dashboard,
    inspect_open_renovate_prs,
    inspect_single_renovate_pr,
)
from core.maintenance.reporter import (
    render_audit_report,
    render_deployment_checklist,
    render_migration_application_result,
    render_migration_plan,
    render_post_deploy_verification,
    render_production_environment_info,
    render_proposed_changes_preview,
    render_renovate_pr_analysis,
    render_risk_assessments,
    render_validation_report,
    save_audit_report,
)
from core.maintenance.validator import run_migration_validations
from utils.console import ask_yes_no, ui_panel, ui_table

console = Console()
MAINTENANCE_MENU_CHOICES = ("1", "2", "3", "4", "5", "6", "7", "8", "q")


def _compute_overall_status(
    *,
    renovate_exists: bool,
    has_problematic_drift: bool,
    has_blocked_or_breaking_pr: bool,
    open_prs_count: int,
    pending_updates_count: int,
    has_reservations: bool,
) -> str:
    if not renovate_exists:
        return "RENOVATE NOT CONFIGURED"
    if has_problematic_drift or has_blocked_or_breaking_pr:
        return "ACTION REQUIRED (DRIFT OR HIGH-RISK PR)"
    if open_prs_count > 0 or pending_updates_count > 0:
        return "MAINTENANCE IN PROGRESS (OPEN OR PENDING UPDATES)"
    if has_reservations:
        return "AWAITING FINAL RUNTIME / CI CHECK"
    return "UP TO DATE"


def audit_repository(
    repo_path: str,
    repo_name: str,
    *,
    include_ollama_analysis: bool = True,
    now: datetime | None = None,
) -> AuditReport:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    git_state = inspect_git_state(repo_path)
    stacks, deployment_files, detector_reservations = detect_project_stacks(
        repo_path,
        repo_name,
        git_state=git_state,
    )
    declared_versions = collect_declared_versions(repo_path)
    prod_info = inspect_production_environment(repo_name)
    prod_overrides = (
        prod_info.runtime_versions
        if (prod_info.configured and prod_info.reachable and prod_info.runtime_versions)
        else None
    )
    env_matrix = build_environment_matrix(
        repo_path,
        declared_versions,
        production_overrides=prod_overrides,
    )
    renovate_config = inspect_renovate_config(repo_path)
    dashboard = inspect_dependency_dashboard(repo_path)
    open_prs, pr_warnings = inspect_open_renovate_prs(
        repo_path,
        repo_name,
        env_matrix,
        renovate_config,
        include_ollama_analysis=include_ollama_analysis,
    )
    ci_summary = inspect_ci_status(repo_path)

    reservations: list[str] = []
    for item in detector_reservations + renovate_config.issues + pr_warnings + prod_info.issues:
        if item and item not in reservations:
            reservations.append(item)

    if dashboard.issue_number and dashboard.state != "OPEN":
        reservations.append(
            f"Renovate Dependency Dashboard issue #{dashboard.issue_number} is {dashboard.state} (should remain OPEN)."
        )
    if dashboard.pending_or_rate_limited_items:
        reservations.append(
            f"{len(dashboard.pending_or_rate_limited_items)} pending/rate-limited update(s) remain on the Dependency Dashboard."
        )

    has_problematic_drift = False
    for row in env_matrix:
        if row.drift_status == DRIFT_PROBLEMATIC:
            has_problematic_drift = True
            reservations.append(f"{row.component}: {row.drift_details}")
        if row.production == STATUS_UNKNOWN:
            prod_note = "Production runtime versions are UNKNOWN (no VPS probe configured in Phase 1)."
            if prod_note not in reservations:
                reservations.append(prod_note)

    if ci_summary.jenkins_file and ci_summary.jenkins_status == STATUS_UNKNOWN:
        reservations.append("Jenkins build status is UNKNOWN locally (verify Jenkins pipeline is green).")

    has_blocked_or_breaking_pr = any(
        pr.analysis is not None and pr.analysis.risk_level in {RISK_BLOCKED, RISK_BREAKING}
        for pr in open_prs
    )

    overall_status = _compute_overall_status(
        renovate_exists=renovate_config.exists and renovate_config.valid_json,
        has_problematic_drift=has_problematic_drift,
        has_blocked_or_breaking_pr=has_blocked_or_breaking_pr,
        open_prs_count=len(open_prs),
        pending_updates_count=len(dashboard.pending_or_rate_limited_items),
        has_reservations=bool(reservations),
    )

    base_report = AuditReport(
        repo_name=repo_name,
        repo_path=repo_path,
        generated_at=timestamp,
        git_state=git_state,
        stacks=stacks,
        renovate_config=renovate_config,
        dashboard=dashboard,
        open_prs=open_prs,
        ci_summary=ci_summary,
        env_matrix=env_matrix,
        deployment_files=deployment_files,
        reservations=reservations,
        overall_status=overall_status,
        production_info=prod_info,
    )
    risk_assessments = classify_repository_update_risks(base_report)
    migration_plan = generate_migration_plan(base_report, risk_assessments, now=now)

    return AuditReport(
        repo_name=base_report.repo_name,
        repo_path=base_report.repo_path,
        generated_at=base_report.generated_at,
        git_state=base_report.git_state,
        stacks=base_report.stacks,
        renovate_config=base_report.renovate_config,
        dashboard=base_report.dashboard,
        open_prs=base_report.open_prs,
        ci_summary=base_report.ci_summary,
        env_matrix=base_report.env_matrix,
        deployment_files=base_report.deployment_files,
        reservations=base_report.reservations,
        overall_status=base_report.overall_status,
        risk_assessments=risk_assessments,
        migration_plan=migration_plan,
        production_info=prod_info,
    )


def render_maintenance_menu(repo_name: str) -> None:
    table = ui_table(
        title=f"Dependency Maintenance · {repo_name}",
        caption="Read-only audit, Ollama PR analysis, migration planning, and Git workflows.",
    )
    table.add_column("Key", justify="center", style="bold cyan", no_wrap=True)
    table.add_column("Action", style="bold white", no_wrap=True)
    table.add_column("Description", style="white")
    table.add_row("1", "Full Audit & PR Analysis", "Audit stack, Renovate, version matrix, CI, open PRs, and migration plan.")
    table.add_row("2", "Inspect Specific PR", "Analyze any PR number (open or merged) with Ollama for breaking changes.")
    table.add_row("3", "Analyze Risks & Migration Plan", "Classify update risks, files to review, validation steps, and rollback plan.")
    table.add_row("4", "Prepare Migration Changes", "Preview and apply controlled local migration changes on staging.")
    table.add_row("5", "Run & Report Validations", "Run tests and report PASS, FAIL, SKIPPED, PENDING, and UNKNOWN statuses.")
    table.add_row("6", "Git Workflows (Commit / Merge / Promote)", "Commit on staging, merge a Renovate PR into staging, or promote staging → base.")
    table.add_row("7", "Production & Post-Deploy Checks", "Inspect production .venv/backups, deployment checklist, rollback, and Sentinel cron status.")
    table.add_row("8", "Export Audit Report", "Save the audit and migration report as JSON and Markdown.")
    table.add_row("q", "Back", "Return to the main menu.")
    console.print(table)


def ask_maintenance_action() -> str:
    return Prompt.ask(
        "\n[bold white]Maintenance action[/]",
        choices=list(MAINTENANCE_MENU_CHOICES),
        default="1",
        show_choices=False,
    ).lower()


def run_dependency_maintenance_for_repo(repo_name: str, repo_path: str) -> None:
    last_report: AuditReport | None = None

    while True:
        render_maintenance_menu(repo_name)
        choice = ask_maintenance_action()
        if choice == "q":
            break

        if choice == "1":
            with console.status(f"[bold cyan]Running read-only maintenance audit on {repo_name}...[/]", spinner="dots"):
                last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=True)
            render_audit_report(last_report, target_console=console)
            if ask_yes_no("Export this audit report to JSON and Markdown?", default="n"):
                status, paths = save_audit_report(last_report)
                tone = "success" if status == "saved" else "info"
                label = "Saved report files" if status == "saved" else "Dry-run simulation (no files written)"
                console.print(
                    ui_panel(
                        f"{label}:\n" + "\n".join(f"- {p}" for p in paths),
                        title="Report Export",
                        tone=tone,
                        compact=True,
                    )
                )
            continue

        if choice == "2":
            raw_pr = Prompt.ask("[bold white]Pull Request number to analyze[/]").strip().lstrip("#")
            if not raw_pr.isdigit():
                console.print(
                    ui_panel(
                        f"Invalid PR number '{raw_pr}'. Please enter a numeric PR ID.",
                        title="Invalid Input",
                        tone="warning",
                        compact=True,
                    )
                )
                continue
            pr_number = int(raw_pr)
            declared = collect_declared_versions(repo_path)
            env_matrix = build_environment_matrix(repo_path, declared)
            renovate_cfg = inspect_renovate_config(repo_path)
            pr_info = inspect_single_renovate_pr(
                repo_path,
                repo_name,
                pr_number,
                env_matrix,
                renovate_cfg,
            )
            if pr_info is None:
                console.print(
                    ui_panel(
                        f"Could not fetch PR #{pr_number} via GitHub CLI.",
                        title="PR Not Found",
                        tone="danger",
                        compact=True,
                    )
                )
                continue
            render_renovate_pr_analysis(pr_info, target_console=console)
            continue

        if choice == "3":
            if last_report is None:
                with console.status(f"[bold cyan]Analyzing risks and preparing migration plan for {repo_name}...[/]", spinner="dots"):
                    last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=True)
            render_risk_assessments(last_report.risk_assessments, target_console=console)
            if last_report.migration_plan is not None:
                render_migration_plan(last_report.migration_plan, target_console=console)
            continue

        if choice == "4":
            if last_report is None:
                with console.status(f"[bold cyan]Preparing migration changes for {repo_name}...[/]", spinner="dots"):
                    last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=False)
            ok, issues = verify_migration_git_preconditions(repo_path)
            if not ok:
                console.print(
                    ui_panel(
                        "\n".join(f"- {msg}" for msg in issues),
                        title="Git Precondition Check Failed",
                        tone="danger",
                        compact=True,
                    )
                )
                continue
            proposals = prepare_migration_changes(last_report)
            render_proposed_changes_preview(proposals, target_console=console)
            if proposals and ask_yes_no("Apply these controlled migration changes locally?", default="n"):
                app_result = apply_migration_changes(repo_path, proposals)
                render_migration_application_result(app_result, target_console=console)
            continue

        if choice == "5":
            if last_report is None:
                with console.status(f"[bold cyan]Running audit on {repo_name} before validations...[/]", spinner="dots"):
                    last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=False)
            run_tests = ask_yes_no("Execute local unit/integration tests now?", default="y")
            with console.status(f"[bold cyan]Running migration validations on {repo_name}...[/]", spinner="dots"):
                val_report = run_migration_validations(last_report, execute_local_tests=run_tests)
            render_validation_report(val_report, target_console=console)
            continue

        if choice == "6":
            if last_report is None:
                with console.status(f"[bold cyan]Inspecting repository state for {repo_name}...[/]", spinner="dots"):
                    last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=False)
            git_action = Prompt.ask(
                "[bold white]Git workflow action (1=Auto-Commit on staging, 2=Merge Renovate PR to staging, 3=Promote staging → base, b=Back)[/]",
                choices=["1", "2", "3", "b"],
                default="b",
                show_choices=False,
            ).lower()
            if git_action == "1":
                run_maintenance_auto_commit(repo_path, repo_name, target_console=console)
            elif git_action == "2":
                if not last_report.open_prs:
                    console.print(
                        ui_panel(
                            "No open Renovate pull requests found for this repository.",
                            title="Merge Renovate PR",
                            tone="info",
                            compact=True,
                        )
                    )
                else:
                    for pr in last_report.open_prs:
                        merge_renovate_pr_to_staging(
                            repo_path,
                            repo_name,
                            pr,
                            target_console=console,
                        )
            elif git_action == "3":
                run_maintenance_promote_to_base(repo_path, repo_name, target_console=console)
            continue

        if choice == "7":
            if last_report is None:
                with console.status(f"[bold cyan]Inspecting repository and production readiness for {repo_name}...[/]", spinner="dots"):
                    last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=False)
            prod_info = last_report.production_info or inspect_production_environment(repo_name)
            render_production_environment_info(prod_info, target_console=console)
            checklist = prepare_deployment_and_rollback(last_report, prod_info)
            render_deployment_checklist(checklist, target_console=console)
            sentinel_ok = ask_yes_no(
                "Has a real scheduled cron execution been verified via Sentinel after deployment?",
                default="n",
            )
            post_report = verify_post_deployment(
                repo_name,
                prod_info,
                sentinel_cron_verified=sentinel_ok,
            )
            render_post_deploy_verification(post_report, target_console=console)
            continue

        if choice == "8":
            if last_report is None:
                with console.status(f"[bold cyan]Running audit on {repo_name} before export...[/]", spinner="dots"):
                    last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=True)
            status, paths = save_audit_report(last_report)
            tone = "success" if status == "saved" else "info"
            label = "Saved report files" if status == "saved" else "Dry-run simulation (no files written)"
            console.print(
                ui_panel(
                    f"{label}:\n" + "\n".join(f"- {p}" for p in paths),
                    title="Report Export",
                    tone=tone,
                    compact=True,
                )
            )
