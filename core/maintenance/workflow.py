from datetime import datetime

from rich.console import Console
from rich.prompt import Prompt

from core.maintenance.detector import (
    detect_project_stacks,
    inspect_git_state,
    inspect_renovate_config,
)
from core.maintenance.environments import build_environment_matrix, collect_declared_versions
from core.maintenance.models import (
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
    STATUS_UNKNOWN,
    AuditReport,
)
from core.maintenance.renovate import (
    inspect_ci_status,
    inspect_dependency_dashboard,
    inspect_open_renovate_prs,
    inspect_single_renovate_pr,
)
from core.maintenance.reporter import (
    render_audit_report,
    render_renovate_pr_analysis,
    save_audit_report,
)
from utils.console import ask_yes_no, ui_panel, ui_table

console = Console()
MAINTENANCE_MENU_CHOICES = ("1", "2", "3", "q")


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
    env_matrix = build_environment_matrix(repo_path, declared_versions)
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
    for item in detector_reservations + renovate_config.issues + pr_warnings:
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

    return AuditReport(
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
    )


def render_maintenance_menu(repo_name: str) -> None:
    table = ui_table(
        title=f"Dependency Maintenance · {repo_name}",
        caption="Read-only audit and Ollama-assisted Renovate PR analysis (Phase 1).",
    )
    table.add_column("Key", justify="center", style="bold cyan", no_wrap=True)
    table.add_column("Action", style="bold white", no_wrap=True)
    table.add_column("Description", style="white")
    table.add_row("1", "Full Audit & PR Analysis", "Audit stack, Renovate, version matrix, CI and analyze open PRs with Ollama.")
    table.add_row("2", "Inspect Specific PR", "Analyze any PR number (open or merged) with Ollama for breaking changes.")
    table.add_row("3", "Export Audit Report", "Save the audit report as JSON and Markdown.")
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
