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
    run_renovate_onboarding_workflow,
)
from core.maintenance.journal import (
    load_maintenance_journal,
    record_maintenance_step,
)
from core.maintenance.models import (
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
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
    render_maintenance_journal,
    render_migration_plan,
    render_post_deploy_verification,
    render_production_environment_info,
    render_project_dashboard,
    render_renovate_pr_analysis,
    render_risk_assessments,
    render_validation_report,
    save_audit_report,
)
from core.maintenance.validator import run_migration_validations
from utils.console import ask_yes_no, ui_panel, ui_table

console = Console()
MAINTENANCE_MENU_CHOICES = ("1", "2", "3", "4", "r", "q")


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
        caption="Parcours guidé : Onboarding Renovate, Bump assisté par IA, Inspection détaillée & Vérification Prod.",
    )
    table.add_column("Key", justify="center", style="bold cyan", no_wrap=True)
    table.add_column("Action", style="bold white", no_wrap=True)
    table.add_column("Description", style="white")
    table.add_row(
        "1",
        "🚀 Déployer / Aligner Renovate",
        "Configurer renovate.json (preset Dev Platform), aligner CI/deploy.sh, commiter sur staging & promouvoir.",
    )
    table.add_row(
        "2",
        "⚡ Traiter les PRs Renovate (Bump guidé)",
        "Voir le verdict IA de chaque PR, merger les bumps sûrs sur staging, tester et promouvoir vers master.",
    )
    table.add_row(
        "3",
        "🔍 Inspecter en détail & Exporter",
        "Analyser un n° de PR précis avec Ollama, afficher le plan complet, la matrice détaillée ou exporter (.md/.json).",
    )
    table.add_row(
        "4",
        "🩺 Checklist Production & Post-déploiement",
        "Vérifier .venv symlink/backups, procédure de rollback et statut du cron via Sentinel.",
    )
    table.add_row("r", "🔄 Rafraîchir le Dashboard", "Relancer l'analyse rapide de l'état du projet et des PRs.")
    table.add_row("q", "Retour", "Revenir au menu principal.")
    console.print(table)


def ask_maintenance_action() -> str:
    return Prompt.ask(
        "\n[bold white]Action de maintenance[/]",
        choices=list(MAINTENANCE_MENU_CHOICES),
        default="q",
        show_choices=False,
    ).lower()


def run_guided_pr_bump_workflow(
    repo_path: str,
    repo_name: str,
    report: AuditReport,
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    if not report.open_prs:
        out.print(
            ui_panel(
                "Aucune Pull Request Renovate ouverte sur ce dépôt actuellement.",
                title="Traitement des PRs Renovate",
                tone="success",
                compact=True,
            )
        )
        return "no-prs"

    merged_or_simulated = 0
    for pr in report.open_prs:
        render_renovate_pr_analysis(pr, target_console=out)
        merge_status = merge_renovate_pr_to_staging(
            repo_path,
            repo_name,
            pr,
            target_console=out,
        )
        if merge_status in {"merged", "dry-run"}:
            merged_or_simulated += 1

    if merged_or_simulated > 0:
        if ask_yes_no("Exécuter les validations et tests locaux sur staging maintenant ?", default="y"):
            val_report = run_migration_validations(report, execute_local_tests=True)
            render_validation_report(val_report, target_console=out)
        run_maintenance_promote_to_base(repo_path, repo_name, target_console=out)
        return "processed"

    return "reviewed"


def run_dependency_maintenance_for_repo(repo_name: str, repo_path: str) -> None:
    existing_journal = load_maintenance_journal(repo_path, repo_name)
    if existing_journal is not None and not existing_journal.is_complete:
        render_maintenance_journal(existing_journal, target_console=console)

    with console.status(f"[bold cyan]Analyse de l'état de {repo_name} et des PRs Renovate...[/]", spinner="dots"):
        last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=True)
    render_project_dashboard(last_report, target_console=console)
    record_maintenance_step(
        repo_path,
        repo_name,
        "audit",
        last_report.overall_status,
        details=f"{len(last_report.open_prs)} open PR(s)",
        pending_actions=last_report.reservations,
    )

    while True:
        render_maintenance_menu(repo_name)
        choice = ask_maintenance_action()
        if choice == "q":
            break

        if choice == "r":
            with console.status(f"[bold cyan]Rafraîchissement du dashboard pour {repo_name}...[/]", spinner="dots"):
                last_report = audit_repository(repo_path, repo_name, include_ollama_analysis=True)
            render_project_dashboard(last_report, target_console=console)
            continue

        if choice == "1":
            status = run_renovate_onboarding_workflow(
                repo_path,
                repo_name,
                last_report,
                target_console=console,
            )
            record_maintenance_step(
                repo_path,
                repo_name,
                "onboarding",
                status.upper(),
                details="Onboarding & alignement Renovate",
                pending_actions=last_report.reservations,
            )
            continue

        if choice == "2":
            bump_status = run_guided_pr_bump_workflow(
                repo_path,
                repo_name,
                last_report,
                target_console=console,
            )
            record_maintenance_step(
                repo_path,
                repo_name,
                "bump",
                bump_status.upper(),
                details=f"{len(last_report.open_prs)} PR(s) examinée(s)",
                pending_actions=last_report.reservations,
            )
            continue

        if choice == "3":
            sub = Prompt.ask(
                "[bold white]Inspection (1=Analyser un n° de PR précis, 2=Risques & Plan de migration, 3=Audit complet détaillé, 4=Exporter .md/.json, b=Retour)[/]",
                choices=["1", "2", "3", "4", "b"],
                default="2",
                show_choices=False,
            ).lower()
            if sub == "1":
                raw_pr = Prompt.ask("[bold white]Numéro de Pull Request à analyser[/]").strip().lstrip("#")
                if not raw_pr.isdigit():
                    console.print(
                        ui_panel(
                            f"Numéro de PR invalide '{raw_pr}'.",
                            title="Entrée Invalide",
                            tone="warning",
                            compact=True,
                        )
                    )
                    continue
                pr_number = int(raw_pr)
                pr_info = inspect_single_renovate_pr(
                    repo_path,
                    repo_name,
                    pr_number,
                    last_report.env_matrix,
                    last_report.renovate_config,
                )
                if pr_info is None:
                    console.print(
                        ui_panel(
                            f"Impossible de récupérer la PR #{pr_number} via GitHub CLI.",
                            title="PR Introuvable",
                            tone="danger",
                            compact=True,
                        )
                    )
                    continue
                render_renovate_pr_analysis(pr_info, target_console=console)
            elif sub == "2":
                render_risk_assessments(last_report.risk_assessments, target_console=console)
                if last_report.migration_plan is not None:
                    render_migration_plan(last_report.migration_plan, target_console=console)
            elif sub == "3":
                render_audit_report(last_report, target_console=console)
            elif sub == "4":
                exp_status, paths = save_audit_report(last_report)
                record_maintenance_step(
                    repo_path,
                    repo_name,
                    "export",
                    exp_status.upper(),
                    details=", ".join(paths),
                )
                tone = "success" if exp_status == "saved" else "info"
                label = "Fichiers exportés" if exp_status == "saved" else "Simulation dry-run (aucun fichier écrit)"
                console.print(
                    ui_panel(
                        f"{label} :\n" + "\n".join(f"- {p}" for p in paths),
                        title="Export du Rapport",
                        tone=tone,
                        compact=True,
                    )
                )
            continue

        if choice == "4":
            prod_info = last_report.production_info or inspect_production_environment(repo_name)
            render_production_environment_info(prod_info, target_console=console)
            checklist = prepare_deployment_and_rollback(last_report, prod_info)
            render_deployment_checklist(checklist, target_console=console)
            sentinel_ok = ask_yes_no(
                "Une exécution réelle du cron a-t-elle été confirmée au vert dans Sentinel après déploiement ?",
                default="n",
            )
            post_report = verify_post_deployment(
                repo_name,
                prod_info,
                sentinel_cron_verified=sentinel_ok,
            )
            render_post_deploy_verification(post_report, target_console=console)
            record_maintenance_step(
                repo_path,
                repo_name,
                "production_check",
                post_report.overall_status,
                details=f"Scheduled jobs: {post_report.scheduled_jobs_status}",
                pending_actions=post_report.notes,
                is_complete=sentinel_ok and checklist.ready_to_deploy,
            )
