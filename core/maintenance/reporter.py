import json
from pathlib import Path

from rich.console import Console

from core.maintenance.models import (
    DRIFT_ACCEPTED,
    DRIFT_ALIGNED,
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
    RISK_LOW,
    RISK_REVIEW,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_PENDING,
    AuditReport,
    MigrationPlan,
    RenovatePRInfo,
    UpdateRiskAssessment,
)
from utils.common import is_dry_run
from utils.console import ui_panel, ui_table

console = Console()


def _status_badge(status: str) -> str:
    upper = (status or "UNKNOWN").upper()
    styles = {
        STATUS_PASS: "bold green",
        "OK": "bold green",
        STATUS_FAIL: "bold red",
        STATUS_PENDING: "bold yellow",
        "SKIPPED": "bold yellow",
        "UNKNOWN": "dim",
    }
    style = styles.get(upper, "white")
    return f"[{style}]{upper}[/]"


def _risk_badge(risk: str) -> str:
    upper = (risk or "UNKNOWN").upper()
    styles = {
        RISK_LOW: "bold green",
        RISK_REVIEW: "bold yellow",
        RISK_BREAKING: "bold red",
        RISK_BLOCKED: "bold red",
    }
    style = styles.get(upper, "cyan")
    return f"[{style}]{upper}[/]"


def _drift_badge(drift: str) -> str:
    styles = {
        DRIFT_ALIGNED: "green",
        DRIFT_ACCEPTED: "cyan",
        DRIFT_PROBLEMATIC: "bold red",
    }
    style = styles.get(drift, "yellow")
    return f"[{style}]{drift}[/]"


def render_renovate_pr_analysis(pr: RenovatePRInfo, *, target_console: Console | None = None) -> None:
    out = target_console or console
    analysis = pr.analysis
    if analysis is None:
        return

    tone_map = {
        RISK_LOW: "success",
        RISK_REVIEW: "warning",
        RISK_BREAKING: "danger",
        RISK_BLOCKED: "danger",
    }
    tone = tone_map.get(analysis.risk_level, "info")
    breaking_label = "[bold red]OUI ⚠️[/]" if analysis.breaking_change else "[bold green]NON ✅[/]"
    source_label = "🤖 Ollama IA + Contrôles déterministes" if analysis.source == "ollama" else "🔎 Analyse déterministe"
    updates_str = (
        ", ".join(f"{u.package} ({u.raw_change}, {u.update_type})" for u in pr.updates)
        if pr.updates
        else "(aucune table de dépendances)"
    )

    lines = [
        f"[bold white]PR #{pr.number}:[/] [cyan]{pr.title}[/]",
        f"[bold white]Branches:[/] {pr.head_branch} → {pr.base_branch}  [dim]|[/]  [bold white]CI:[/] {_status_badge(pr.ci_status)}  [dim]|[/]  [bold white]Source:[/] {source_label}",
        f"[bold white]Dépendances:[/] {updates_str}",
        f"[bold white]Niveau de risque:[/] {_risk_badge(analysis.risk_level)}  [dim]|[/]  [bold white]Breaking change:[/] {breaking_label}",
        "",
        f"[bold cyan]Synthèse[/]\n{analysis.summary}",
        "",
        f"[bold cyan]Analyse Breaking Change[/]\n{analysis.breaking_summary}",
    ]

    if analysis.key_changes:
        lines.append("")
        lines.append("[bold cyan]Changements clés[/]")
        lines.extend(f"- {item}" for item in analysis.key_changes)

    if analysis.project_impacts:
        lines.append("")
        lines.append("[bold yellow]Impacts sur le projet / CI / Environnements[/]")
        lines.extend(f"- {item}" for item in analysis.project_impacts)

    if analysis.checks_before_merge:
        lines.append("")
        lines.append("[bold magenta]À vérifier avant de merger[/]")
        lines.extend(f"- {item}" for item in analysis.checks_before_merge)

    if analysis.verdict:
        lines.append("")
        lines.append(f"[bold white]Verdict:[/] {analysis.verdict}")

    out.print(
        ui_panel(
            "\n".join(lines),
            title=f"Analyse PR Renovate #{pr.number}",
            tone=tone,
        )
    )


def render_audit_report(report: AuditReport, *, target_console: Console | None = None) -> None:
    out = target_console or console
    gs = report.git_state

    clean_label = "[green]CLEAN[/]" if gs.is_clean else f"[yellow]DIRTY ({gs.staged_count} staged, {gs.unstaged_count} unstaged, {gs.untracked_count} untracked)[/]"
    venv_desc = "not present"
    if gs.venv_exists:
        if gs.venv_is_symlink:
            venv_desc = f"symlink → {gs.venv_target or '?'}"
        else:
            venv_desc = "directory"
        if gs.venv_has_pip is False:
            venv_desc += " (no pip binary)"

    ahead_behind_str = "unknown"
    if gs.ahead_of_base is not None and gs.behind_base is not None:
        ahead_behind_str = f"ahead {gs.ahead_of_base}, behind {gs.behind_base}"

    out.print(
        ui_panel(
            (
                f"[bold white]Repository:[/] [cyan]{report.repo_name}[/] [dim]({report.repo_path})[/]\n"
                f"[bold white]Current branch:[/] [cyan]{gs.branch}[/]  [dim]|[/]  "
                f"[bold white]Base branch:[/] [cyan]{gs.base_branch or 'unresolved'}[/] [dim]({gs.base_branch_resolution})[/]\n"
                f"[bold white]Working tree:[/] {clean_label}  [dim]|[/]  [bold white]staging vs base:[/] {ahead_behind_str}\n"
                f"[bold white]Virtualenv (.venv):[/] {venv_desc}"
            ),
            title="1 · Repository & Git State",
        )
    )

    stack_table = ui_table(title="2 · Detected Technical Stack")
    stack_table.add_column("Ecosystem", style="bold cyan", no_wrap=True)
    stack_table.add_column("Manager", style="white", no_wrap=True)
    stack_table.add_column("Files", style="white")
    stack_table.add_column("Notes / Hygiene", style="yellow")

    if report.stacks:
        for st in report.stacks:
            notes = "; ".join(st.details) if st.details else "OK"
            files_preview = ", ".join(st.files[:5])
            if len(st.files) > 5:
                files_preview += f" (+{len(st.files) - 5} more)"
            stack_table.add_row(st.name, st.manager, files_preview, notes)
    else:
        stack_table.add_row("(none detected)", "-", "-", "No standard manifest detected")
    out.print(stack_table)

    rc = report.renovate_config
    db = report.dashboard
    if not rc.exists:
        renovate_label = "[bold red]MISSING[/] (no renovate.json)"
        renovate_tone = "warning"
    elif not rc.valid_json:
        renovate_label = "[bold red]INVALID JSON[/]"
        renovate_tone = "danger"
    elif rc.extends_dev_platform:
        renovate_label = "[bold green]OK[/] (inherits Dev Platform preset)"
        renovate_tone = "success"
    else:
        renovate_label = "[bold yellow]CUSTOM CONFIG[/] (does not extend Dev Platform preset)"
        renovate_tone = "warning"

    dashboard_line = db.details or db.status
    if db.issue_number:
        dashboard_line = f"Issue #{db.issue_number} ({db.state}) — {db.details}"

    renovate_lines = [
        f"[bold white]Configuration ({rc.path}):[/] {renovate_label}",
        f"[bold white]Extends:[/] {', '.join(rc.extends) if rc.extends else '(none)'}",
        f"[bold white]Local overrides:[/] {', '.join(rc.local_overrides) if rc.local_overrides else '(none)'}",
        f"[bold white]Private registries:[/] {', '.join(rc.private_registries) if rc.private_registries else '(none)'}",
        f"[bold white]Dependency Dashboard:[/] {dashboard_line}",
    ]
    if rc.issues:
        renovate_lines.append("[bold yellow]Config warnings:[/] " + " | ".join(rc.issues))

    out.print(
        ui_panel(
            "\n".join(renovate_lines),
            title="3 · Renovate & Dependency Dashboard",
            tone=renovate_tone,
        )
    )

    matrix_table = ui_table(
        title="4 · Environment Version Matrix",
        caption="Production stays UNKNOWN in Phase 1 unless explicitly configured. Version differences in DB are evaluated without forced upgrades.",
        show_lines=True,
    )
    matrix_table.add_column("Component", style="bold white", no_wrap=True)
    matrix_table.add_column("Local", justify="center")
    matrix_table.add_column("GitHub Actions", justify="center")
    matrix_table.add_column("Jenkins", justify="center")
    matrix_table.add_column("Docker", justify="center")
    matrix_table.add_column("Production", justify="center")
    matrix_table.add_column("Alignment & Sources", style="white")

    if report.env_matrix:
        for row in report.env_matrix:
            sources_str = "\n".join(f"• {s}" for s in row.sources)
            details_cell = f"{_drift_badge(row.drift_status)}\n{row.drift_details}"
            if sources_str:
                details_cell += f"\n[dim]{sources_str}[/]"
            matrix_table.add_row(
                row.component,
                row.local,
                row.github_actions,
                row.jenkins,
                row.docker,
                row.production,
                details_cell,
            )
    else:
        matrix_table.add_row("(none)", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "No runtime versions declared")
    out.print(matrix_table)

    if report.open_prs:
        pr_table = ui_table(title="5 · Open Dependency Pull Requests", show_lines=True)
        pr_table.add_column("PR", justify="center", style="bold cyan", no_wrap=True)
        pr_table.add_column("Title", style="bold white")
        pr_table.add_column("Target", justify="center", no_wrap=True)
        pr_table.add_column("CI", justify="center", no_wrap=True)
        pr_table.add_column("Risk", justify="center", no_wrap=True)
        pr_table.add_column("Breaking?", justify="center", no_wrap=True)

        for pr in report.open_prs:
            risk_val = pr.analysis.risk_level if pr.analysis else "UNKNOWN"
            brk_val = (
                "[bold red]YES[/]"
                if (pr.analysis and pr.analysis.breaking_change)
                else "[green]NO[/]"
            )
            pr_table.add_row(
                f"#{pr.number}",
                pr.title,
                f"{pr.head_branch} → {pr.base_branch}",
                _status_badge(pr.ci_status),
                _risk_badge(risk_val),
                brk_val,
            )
        out.print(pr_table)

        for pr in report.open_prs:
            render_renovate_pr_analysis(pr, target_console=out)
    else:
        out.print(
            ui_panel(
                "Aucune Pull Request Renovate/dépendance ouverte actuellement sur ce dépôt.",
                title="5 · Open Dependency Pull Requests",
                tone="success",
                compact=True,
            )
        )

    if report.risk_assessments:
        render_risk_assessments(report.risk_assessments, target_console=out)
    if report.migration_plan is not None:
        render_migration_plan(report.migration_plan, target_console=out)

    out.print(
        ui_panel(
            format_executive_summary_text(report),
            title="Final Maintenance Summary",
            tone="warning" if report.reservations else "success",
        )
    )


def render_risk_assessments(
    assessments: list[UpdateRiskAssessment],
    *,
    target_console: Console | None = None,
) -> None:
    out = target_console or console
    table = ui_table(
        title="6 · Risk Classification & Target Versions",
        caption="Recommendations include their sources and known uncertainties.",
        show_lines=True,
    )
    table.add_column("Subject", style="bold white")
    table.add_column("Current → Proposed", justify="center")
    table.add_column("Target", justify="center", style="cyan")
    table.add_column("Risk", justify="center", no_wrap=True)
    table.add_column("Recommendation, Sources & Uncertainties", style="white")

    if not assessments:
        table.add_row("(none)", "-", "-", _risk_badge(RISK_LOW), "No update or alignment risk identified.")
        out.print(table)
        return

    for item in assessments:
        details_lines = [item.recommendation]
        if item.affected_files:
            details_lines.append(f"[bold yellow]Files:[/] {', '.join(item.affected_files)}")
        if item.sources:
            details_lines.append(f"[dim]Sources: {'; '.join(item.sources[:3])}[/]")
        if item.uncertainties:
            details_lines.append(f"[yellow]Uncertainties: {'; '.join(item.uncertainties[:2])}[/]")

        table.add_row(
            item.subject,
            f"{item.current_version} → {item.proposed_version}",
            item.target_version,
            _risk_badge(item.risk_level),
            "\n".join(details_lines),
        )
    out.print(table)


def render_migration_plan(
    plan: MigrationPlan,
    *,
    target_console: Console | None = None,
) -> None:
    out = target_console or console

    if plan.files_to_review:
        file_table = ui_table(title="7 · Files to Review / Modify", show_lines=True)
        file_table.add_column("File", style="bold cyan", no_wrap=True)
        file_table.add_column("Category", justify="center", style="white", no_wrap=True)
        file_table.add_column("Reason", style="yellow")
        file_table.add_column("Suggested Action", style="white")
        for target in plan.files_to_review:
            file_table.add_row(target.path, target.category, target.reason, target.suggested_change)
        out.print(file_table)

    plan_lines = [f"[bold white]{plan.summary}[/]"]
    if plan.target_versions:
        plan_lines.append("")
        plan_lines.append("[bold cyan]Target Versions[/]")
        for comp, ver in plan.target_versions.items():
            plan_lines.append(f"- [bold]{comp}:[/] {ver}")

    if plan.ordered_steps:
        plan_lines.append("")
        plan_lines.append("[bold cyan]Migration Steps[/]")
        plan_lines.extend(plan.ordered_steps)

    if plan.validation_steps:
        plan_lines.append("")
        plan_lines.append("[bold cyan]Validation Checklist[/]")
        plan_lines.extend(f"- {step}" for step in plan.validation_steps)

    rb = plan.rollback_plan
    plan_lines.append("")
    plan_lines.append(f"[bold magenta]Rollback Plan ({rb.strategy} · backup: {rb.backup_target})[/]")
    for pre in rb.preconditions:
        plan_lines.append(f"- [dim]Precondition:[/] {pre}")
    for step in rb.steps:
        plan_lines.append(f"- {step}")

    if plan.uncertainties:
        plan_lines.append("")
        plan_lines.append("[bold yellow]Known Uncertainties[/]")
        for unc in plan.uncertainties:
            plan_lines.append(f"- {unc}")

    out.print(
        ui_panel(
            "\n".join(plan_lines),
            title="8 · Migration & Rollback Plan",
            tone="info",
        )
    )



def format_executive_summary_text(report: AuditReport) -> str:
    rc = report.renovate_config
    if not rc.exists:
        renovate_state = "MISSING"
    elif not rc.valid_json:
        renovate_state = "INVALID"
    elif rc.extends_dev_platform:
        renovate_state = "OK (Dev Platform preset)"
    else:
        renovate_state = "OK (custom local config)"

    pending_count = len(report.dashboard.pending_or_rate_limited_items)
    lines = [
        f"Repository: {report.repo_name}",
        "",
        f"Renovate: {renovate_state}",
        f"Open dependency PRs: {len(report.open_prs)}",
        f"Pending updates: {pending_count}",
        "",
        f"GitHub Actions: {report.ci_summary.github_actions_status}",
        f"Jenkins: {report.ci_summary.jenkins_status}",
    ]

    if report.env_matrix:
        lines.append("")
        for row in report.env_matrix:
            ci_vals = [v for v in (row.github_actions, row.jenkins, row.docker) if v not in {"UNKNOWN", "N/A"}]
            ci_summary = ", ".join(dict.fromkeys(ci_vals)) if ci_vals else "UNKNOWN"
            lines.append(f"{row.component} CI: {ci_summary}")
            lines.append(f"{row.component} production: {row.production}")
            lines.append(f"{row.component} compatibility: {row.drift_status}")

    if report.reservations:
        lines.append("")
        lines.append("Reservations & Points to verify:")
        for res in report.reservations:
            lines.append(f"- {res}")

    lines.extend(
        [
            "",
            "Maintenance status:",
            report.overall_status,
        ]
    )
    return "\n".join(lines)


def format_report_json(report: AuditReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"


def format_report_markdown(report: AuditReport) -> str:
    lines = [
        f"# Dependency Maintenance Audit — `{report.repo_name}`",
        "",
        f"- **Generated at**: `{report.generated_at}`",
        f"- **Repository path**: `{report.repo_path}`",
        f"- **Branch**: `{report.git_state.branch}` (base: `{report.git_state.base_branch or 'unresolved'}`)",
        f"- **Working tree clean**: `{'YES' if report.git_state.is_clean else 'NO'}`",
        f"- **Overall status**: `{report.overall_status}`",
        "",
        "## Executive Summary",
        "",
        "```text",
        format_executive_summary_text(report),
        "```",
        "",
        "## Environment Version Matrix",
        "",
        "| Component | Local | GitHub Actions | Jenkins | Docker | Production | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report.env_matrix:
        lines.append(
            f"| {row.component} | {row.local} | {row.github_actions} | {row.jenkins} | {row.docker} | {row.production} | {row.drift_status} |"
        )

    if report.open_prs:
        lines.extend(["", "## Open Dependency Pull Requests", ""])
        for pr in report.open_prs:
            lines.append(f"### PR #{pr.number} — {pr.title}")
            lines.append(f"- **Branches**: `{pr.head_branch}` → `{pr.base_branch}`")
            lines.append(f"- **CI Status**: `{pr.ci_status}`")
            if pr.analysis:
                lines.append(f"- **Risk Level**: `{pr.analysis.risk_level}`")
                lines.append(f"- **Breaking Change**: `{'YES' if pr.analysis.breaking_change else 'NO'}` ({pr.analysis.breaking_summary})")
                lines.append(f"- **Summary**: {pr.analysis.summary}")
                lines.append(f"- **Verdict**: {pr.analysis.verdict}")
                if pr.analysis.project_impacts:
                    lines.append("- **Project Impacts**:")
                    for imp in pr.analysis.project_impacts:
                        lines.append(f"  - {imp}")
                if pr.analysis.checks_before_merge:
                    lines.append("- **Checks Before Merge**:")
                    for chk in pr.analysis.checks_before_merge:
                        lines.append(f"  - {chk}")
            lines.append("")

    if report.reservations:
        lines.extend(["## Reservations & Open Items", ""])
        for item in report.reservations:
            lines.append(f"- {item}")
        lines.append("")

    if report.migration_plan is not None:
        mp = report.migration_plan
        lines.extend(["## Migration & Rollback Plan", "", mp.summary, ""])
        if mp.files_to_review:
            lines.extend(
                [
                    "### Files to Review",
                    "",
                    "| File | Category | Reason | Suggested Action |",
                    "|---|---|---|---|",
                ]
            )
            for f_target in mp.files_to_review:
                lines.append(
                    f"| `{f_target.path}` | {f_target.category} | {f_target.reason} | {f_target.suggested_change} |"
                )
            lines.append("")
        if mp.ordered_steps:
            lines.extend(["### Migration Steps", ""])
            for step in mp.ordered_steps:
                lines.append(f"- {step}")
            lines.append("")
        if mp.validation_steps:
            lines.extend(["### Validation Steps", ""])
            for v_step in mp.validation_steps:
                lines.append(f"- {v_step}")
            lines.append("")
        lines.extend(
            [
                f"### Rollback Plan (`{mp.rollback_plan.strategy}` — `{mp.rollback_plan.backup_target}`)",
                "",
            ]
        )
        for rb_step in mp.rollback_plan.steps:
            lines.append(f"- {rb_step}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def save_audit_report(
    report: AuditReport,
    output_dir: str | Path | None = None,
) -> tuple[str, list[str]]:
    target_dir = Path(output_dir) if output_dir is not None else Path(report.repo_path)
    json_path = target_dir / f"{report.repo_name}-maintenance-audit.json"
    md_path = target_dir / f"{report.repo_name}-maintenance-audit.md"
    paths = [str(json_path), str(md_path)]

    if is_dry_run():
        return "dry-run", paths

    target_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(format_report_json(report), encoding="utf-8")
    md_path.write_text(format_report_markdown(report), encoding="utf-8")
    return "saved", paths
