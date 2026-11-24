from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt

from core.maintenance.analyzer import classify_repository_update_risks
from core.maintenance.detector import (
    detect_project_stacks,
    inspect_git_state,
    inspect_renovate_config,
)
from core.maintenance.environments import build_environment_matrix, collect_declared_versions
from core.maintenance.git_ops import (
    apply_direct_staging_updates,
    apply_ruby_gem_updates,
    collect_direct_staging_updates,
    collect_ruby_gem_updates,
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
    render_all_repos_maintenance_summary,
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
from utils.console import (
    ask_yes_no,
    clear_screen,
    resolve_console_command,
    ui_banner,
    ui_command_bar,
    ui_panel,
    ui_table,
)

console = Console()
MAINTENANCE_MENU_CHOICES = ("1", "2", "3", "4", "5", "t", "r", "?", "q")
MAINTENANCE_COMMAND_ITEMS = (
    ("1", "Renovate"),
    ("2", "Bump"),
    ("t", "Check/Test"),
    ("3", "Inspect"),
    ("4", "Prod"),
    ("5", "Gems"),
    ("r", "Refresh"),
    ("?", "Aide"),
    ("q", "Retour"),
)
MAINTENANCE_COMMAND_ALIASES = {
    "renovate": "1",
    "align": "1",
    "bump": "2",
    "update": "2",
    "deps": "2",
    "inspect": "3",
    "audit": "3",
    "export": "3",
    "prod": "4",
    "deploy": "4",
    "gems": "5",
    "ruby": "5",
    "check": "t",
    "test": "t",
    "tests": "t",
    "lint": "t",
    "refresh": "r",
    "clear": "r",
    "sync": "r",
    "help": "?",
    "aide": "?",
    "quit": "q",
    "exit": "q",
    "back": "q",
}


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
    progress_callback: Callable[[int, str], None] | None = None,
) -> AuditReport:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")

    if progress_callback is not None:
        progress_callback(1, f"[{repo_name}] 1/5 Inspection Git & détection de la stack...")
    git_state = inspect_git_state(repo_path)
    stacks, deployment_files, detector_reservations = detect_project_stacks(
        repo_path,
        repo_name,
        git_state=git_state,
    )

    if progress_callback is not None:
        progress_callback(2, f"[{repo_name}] 2/5 Matrice des versions (local / CI / prod)...")
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

    if progress_callback is not None:
        progress_callback(3, f"[{repo_name}] 3/5 Configuration Renovate & Dependency Dashboard...")
    renovate_config = inspect_renovate_config(repo_path)
    dashboard = inspect_dependency_dashboard(repo_path)

    if progress_callback is not None:
        progress_callback(4, f"[{repo_name}] 4/5 Récupération & analyse IA des PRs Renovate...")
    pr_progress_cb = (
        (lambda msg: progress_callback(4, f"[{repo_name}] 4/5 {msg}"))
        if progress_callback is not None
        else None
    )
    open_prs, pr_warnings = inspect_open_renovate_prs(
        repo_path,
        repo_name,
        env_matrix,
        renovate_config,
        include_ollama_analysis=include_ollama_analysis,
        progress_callback=pr_progress_cb,
    )

    if progress_callback is not None:
        progress_callback(5, f"[{repo_name}] 5/5 Vérification CI, calcul des risques & plan...")
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
    if dashboard.lookup_warnings:
        reservations.append(
            f"Renovate lookup warning(s) on Dependency Dashboard #{dashboard.issue_number}: "
            f"{', '.join(dashboard.lookup_warnings)} (check Mend Credentials / Host Rule for npm.pkg.github.com)."
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
    """Single-line retro command bar (use '?' or 'help' for the detailed command table)."""
    console.print()
    console.print(f"  {ui_command_bar(MAINTENANCE_COMMAND_ITEMS)}")


def render_maintenance_help(repo_name: str) -> None:
    table = ui_table(
        title=f"Aide Commandes · {repo_name}",
        caption="Tapez le chiffre ou la commande texte directement au prompt (ex: bump, check, prod, q).",
    )
    table.add_column("Touche", justify="center", style="bold bright_yellow", no_wrap=True)
    table.add_column("Commande", style="bold bright_green", no_wrap=True)
    table.add_column("Description", style="white")
    table.add_row("1", "renovate", "Aligner renovate.json (preset Dev Platform), CI/deploy.sh, commiter & promouvoir.")
    table.add_row("2", "bump", "Lister les MAJ (npm, workspaces, Renovate), appliquer sur staging, formater Biome, tester & promouvoir.")
    table.add_row("t", "check / test", "Lancer immédiatement les checks & tests locaux (Biome, Vitest/Node, Pytest, RSpec).")
    table.add_row("3", "inspect", "Analyser une PR avec Ollama, voir le plan complet ou exporter (.md/.json).")
    table.add_row("4", "prod", "Vérifier .venv symlink/backups, procédure rollback et statut cron Sentinel.")
    table.add_row("5", "gems", "Mettre à jour les gems Ruby/Rails sur staging via Bundler.")
    table.add_row("r", "refresh", "Nettoyer l'écran et relancer l'audit rapide du dépôt.")
    table.add_row("q", "quit", "Revenir au menu principal.")
    console.print(table)


def ask_maintenance_action(repo_name: str = "maintenance") -> str:
    """Retro interactive command prompt (e.g. `argandici> `) accepting keys or text commands."""
    prompt_label = f"\n[bold bright_yellow]{repo_name}[/][bold bright_green]>[/]"
    while True:
        raw = Prompt.ask(
            prompt_label,
            default="q",
            show_default=False,
        )
        resolved = resolve_console_command(
            raw,
            valid_choices=MAINTENANCE_MENU_CHOICES,
            aliases=MAINTENANCE_COMMAND_ALIASES,
            default="q",
        )
        if resolved is not None:
            return resolved
        console.print(
            f"  [bright_yellow]Commande inconnue '{raw.strip()}'. Tapez [bold]?[/bold] pour l'aide ou [bold]q[/bold] pour revenir.[/]"
        )


def _audit_with_progress(
    repo_path: str,
    repo_name: str,
    *,
    include_ollama_analysis: bool = True,
    prefix: str = "",
    target_console: Console | None = None,
) -> AuditReport:
    out = target_console or console
    out.print()
    with Progress(
        SpinnerColumn(style="bold cyan"),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=28),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=out,
        transient=True,
    ) as progress:
        task_id = progress.add_task(
            f"{prefix}[{repo_name}] Initialisation de l'analyse...",
            total=5,
        )

        def _on_progress(step: int, message: str) -> None:
            progress.update(task_id, completed=max(0, step - 1), description=f"{prefix}{message}")

        report = audit_repository(
            repo_path,
            repo_name,
            include_ollama_analysis=include_ollama_analysis,
            progress_callback=_on_progress,
        )
        progress.update(task_id, completed=5, description=f"{prefix}[{repo_name}] Analyse terminée.")
    return report


def _is_safe_direct_update(pkg: str, update_type: str) -> bool:
    if pkg.startswith("@nlabrazi/"):
        return True
    return update_type in {"patch", "minor", "pin", "digest"}


def _prompt_update_selection(
    updates: list[Any],
    *,
    item_label: str,
    title: str,
    caption: str,
    prompt_text: str,
    confirm_text_template: str,
    current_label: str = "Actuel",
    target_console: Console | None = None,
) -> list[Any] | None:
    out = target_console or console
    out.print()
    table = ui_table(title=title, caption=caption)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column(item_label, style="bold white")
    table.add_column(current_label, style="dim")
    table.add_column("Cible", style="bold green")
    table.add_column("Type", justify="center")
    table.add_column("Risque", style="white")

    for idx, upd in enumerate(updates, start=1):
        if upd.package.startswith("@nlabrazi/"):
            risk_label = "[bold green]🟢 Sûr (Outillage @nlabrazi)[/]"
        elif _is_safe_direct_update(upd.package, upd.update_type):
            risk_label = "[bold green]🟢 Sûr (Patch/Minor)[/]"
        else:
            risk_label = "[bold yellow]🟠 Majeur / À vérifier[/]"
        table.add_row(
            str(idx),
            upd.package,
            upd.from_version or "?",
            upd.to_version,
            upd.update_type,
            risk_label,
        )
    out.print(table)

    mode = Prompt.ask(
        prompt_text,
        choices=["1", "2", "3", "q"],
        default="1",
        show_choices=False,
    ).lower()
    if mode == "q":
        return None

    if mode == "1":
        return [u for u in updates if _is_safe_direct_update(u.package, u.update_type)]
    if mode == "2":
        selected = []
        for upd in updates:
            if ask_yes_no(
                confirm_text_template.format(
                    package=upd.package,
                    from_version=upd.from_version or "?",
                    to_version=upd.to_version,
                    update_type=upd.update_type,
                ),
                default="y" if _is_safe_direct_update(upd.package, upd.update_type) else "n",
            ):
                selected.append(upd)
        return selected
    return list(updates)


def run_guided_pr_bump_workflow(
    repo_path: str,
    repo_name: str,
    report: AuditReport,
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    if not report.open_prs:
        direct_updates = collect_direct_staging_updates(repo_path, report)
        if not direct_updates:
            out.print()
            out.print(
                ui_panel(
                    "Aucune Pull Request Renovate ouverte ni mise à jour en attente sur ce dépôt actuellement.",
                    title="Bump des Dépendances sur Staging",
                    tone="success",
                    compact=True,
                )
            )
            return "no-prs"

        selected = _prompt_update_selection(
            direct_updates,
            item_label="Paquet",
            title=f"Mises à jour disponibles sur staging · {repo_name}",
            caption="Détectées via le Dependency Dashboard Renovate et npm outdated (0 branche créée).",
            prompt_text="\n[bold white]Mode de bump sur staging (1=Toutes les sûres, 2=Choisir paquet par paquet, 3=Tout inclure, q=Annuler)[/]",
            confirm_text_template="Mettre à jour {package} ({from_version} → {to_version}, {update_type}) sur staging ?",
            current_label="Actuel",
            target_console=out,
        )
        if selected is None:
            return "skipped"

        if not selected:
            out.print(
                ui_panel(
                    "Aucune mise à jour sélectionnée.",
                    title="Bump Direct sur Staging",
                    tone="info",
                    compact=True,
                )
            )
            return "skipped"

        apply_status = apply_direct_staging_updates(
            repo_path,
            repo_name,
            selected,
            target_console=out,
        )
        if apply_status in {"applied", "dry-run"}:
            out.print()
            proceed_commit = True
            if ask_yes_no("Exécuter les validations et tests locaux (check & test) sur staging maintenant ?", default="y"):
                val_report = run_migration_validations(report, execute_local_tests=True)
                render_validation_report(val_report, target_console=out)
                failed_checks = getattr(val_report, "counts_by_status", {}).get("FAIL", 0)
                if failed_checks:
                    proceed_commit = ask_yes_no(
                        f"⚠️ {failed_checks} validation(s) locale(s) en ÉCHEC. Continuer quand même vers le commit ?",
                        default="n",
                    )
            if proceed_commit:
                run_maintenance_auto_commit(repo_path, repo_name, target_console=out)
                run_maintenance_promote_to_base(repo_path, repo_name, target_console=out)
            return "processed"
        return apply_status

    merged_or_simulated = 0
    for pr in report.open_prs:
        out.print()
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
        out.print()
        if ask_yes_no("Exécuter les validations et tests locaux sur staging maintenant ?", default="y"):
            val_report = run_migration_validations(report, execute_local_tests=True)
            render_validation_report(val_report, target_console=out)
        run_maintenance_promote_to_base(repo_path, repo_name, target_console=out)
        return "processed"

    return "reviewed"


def run_ruby_gem_bump_workflow(
    repo_path: str,
    repo_name: str,
    report: AuditReport,
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    gemfile = Path(repo_path) / "Gemfile"
    if not gemfile.is_file():
        out.print()
        out.print(
            ui_panel(
                "Aucun fichier Gemfile détecté dans ce dépôt : cette action est réservée aux projets Ruby / Ruby on Rails.",
                title="Bump Gems Ruby · Projet Non-Ruby",
                tone="warning",
                compact=True,
            )
        )
        return "not-ruby"

    gem_updates = collect_ruby_gem_updates(repo_path)
    if not gem_updates:
        out.print()
        out.print(
            ui_panel(
                "Toutes les gems Ruby déclarées dans Gemfile sont déjà à jour.",
                title="Bump Gems Ruby sur Staging",
                tone="success",
                compact=True,
            )
        )
        return "up-to-date"

    selected = _prompt_update_selection(
        gem_updates,
        item_label="Gem",
        title=f"Gems Ruby à mettre à jour sur staging · {repo_name}",
        caption="Détectées via bundle outdated (directes + transitives dans Gemfile.lock, 0 branche créée).",
        prompt_text="\n[bold white]Mode de bump Gems sur staging (1=Toutes les sûres, 2=Choisir gem par gem, 3=Tout inclure, q=Annuler)[/]",
        confirm_text_template="Mettre à jour la gem {package} ({from_version} → {to_version}, {update_type}) sur staging ?",
        current_label="Installée",
        target_console=out,
    )
    if selected is None:
        return "skipped"

    if not selected:
        out.print(
            ui_panel(
                "Aucune gem sélectionnée.",
                title="Bump Gems Ruby sur Staging",
                tone="info",
                compact=True,
            )
        )
        return "skipped"

    apply_status = apply_ruby_gem_updates(
        repo_path,
        repo_name,
        selected,
        target_console=out,
    )
    if apply_status in {"applied", "dry-run"}:
        out.print()
        if ask_yes_no("Exécuter les validations et tests locaux (RSpec / RuboCop) sur staging maintenant ?", default="y"):
            val_report = run_migration_validations(report, execute_local_tests=True)
            render_validation_report(val_report, target_console=out)
        run_maintenance_auto_commit(repo_path, repo_name, target_console=out)
        run_maintenance_promote_to_base(repo_path, repo_name, target_console=out)
        return "processed"
    return apply_status


def run_dependency_maintenance_for_all_repos(
    repositories: list[tuple[str, str]],
    *,
    include_ollama_analysis: bool = False,
    target_console: Console | None = None,
) -> list[AuditReport]:
    out = target_console or console
    if not repositories:
        out.print(
            ui_panel(
                "Aucun dépôt Git à scanner.",
                title="Dependency Maintenance · ALL",
                tone="warning",
                compact=True,
            )
        )
        return []

    total = len(repositories)
    clear_screen(out, label="Dependency Maintenance · ALL")
    out.print(
        ui_banner(
            "Dependency Maintenance · ALL",
            status_items=[f"{total} dépôts (scan rapide sans Ollama)"],
        )
    )

    reports: list[AuditReport] = []
    failed_repos: list[tuple[str, str]] = []

    for idx, (repo_name, repo_path) in enumerate(repositories, start=1):
        prefix = f"({idx}/{total}) "
        try:
            report = _audit_with_progress(
                repo_path,
                repo_name,
                include_ollama_analysis=include_ollama_analysis,
                prefix=prefix,
                target_console=out,
            )
        except Exception as err:
            failed_repos.append((repo_name, str(err)))
            continue

        reports.append(report)

    clear_screen(out, label="Bilan Global · ALL")
    render_all_repos_maintenance_summary(
        reports,
        failed_repos=failed_repos,
        target_console=out,
    )
    return reports


def run_dependency_maintenance_for_repo(repo_name: str, repo_path: str) -> None:
    existing_journal = load_maintenance_journal(repo_path, repo_name)
    last_report = _audit_with_progress(repo_path, repo_name, include_ollama_analysis=True)
    clear_screen(console, label=f"Maintenance · {repo_name}")
    if existing_journal is not None and not existing_journal.is_complete:
        render_maintenance_journal(existing_journal, target_console=console)
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
        choice = ask_maintenance_action(repo_name)
        if choice == "q":
            break

        if choice == "?":
            render_maintenance_help(repo_name)
            continue

        if choice == "t":
            val_report = run_migration_validations(last_report, execute_local_tests=True)
            render_validation_report(val_report, target_console=console)
            continue

        if choice == "r":
            last_report = _audit_with_progress(repo_path, repo_name, include_ollama_analysis=True)
            clear_screen(console, label=f"Maintenance · {repo_name}")
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
                clear_screen(console, label=f"PR #{pr_number} · {repo_name}")
                render_renovate_pr_analysis(pr_info, target_console=console)
            elif sub == "2":
                clear_screen(console, label=f"Plan de migration · {repo_name}")
                render_risk_assessments(last_report.risk_assessments, target_console=console)
                if last_report.migration_plan is not None:
                    render_migration_plan(last_report.migration_plan, target_console=console)
            elif sub == "3":
                clear_screen(console, label=f"Audit complet · {repo_name}")
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
            continue

        if choice == "5":
            gem_status = run_ruby_gem_bump_workflow(
                repo_path,
                repo_name,
                last_report,
                target_console=console,
            )
            record_maintenance_step(
                repo_path,
                repo_name,
                "bump_gems",
                gem_status.upper(),
                details="Bump des gems Ruby/Rails via Bundler",
                pending_actions=last_report.reservations,
            )
            continue
