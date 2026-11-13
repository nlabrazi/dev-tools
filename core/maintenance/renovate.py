import json
import re
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from rich.console import Console

from core.formatters import safe_parse_json
from core.maintenance.detector import _rel_path, inspect_renovate_config
from core.maintenance.models import (
    RISK_BLOCKED,
    RISK_BREAKING,
    RISK_LOW,
    RISK_REVIEW,
    RISK_UNKNOWN,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_PENDING,
    STATUS_UNKNOWN,
    VALID_RISK_LEVELS,
    CIStatusSummary,
    DependencyDashboardInfo,
    EnvironmentMatrixRow,
    RenovateConfigInfo,
    RenovatePRAnalysis,
    RenovatePRInfo,
    RenovateUpdateItem,
)
from core.ollama import (
    OllamaError,
    chat_json,
    debug_log_output,
    ensure_repo_context_allowed,
    is_ollama_enabled,
)
from core.prompts import RENOVATE_PR_REVIEW_SYSTEM, RENOVATE_PR_REVIEW_USER_TEMPLATE
from utils.common import describe_command_failure, env_int, run_command, trim_text_middle

console = Console()

CHANGE_ARROW_RE = re.compile(r"`?v?([0-9A-Za-z.\-_]+)`?\s*(?:→|->)\s*`?v?([0-9A-Za-z.\-_]+)`?")
MARKDOWN_LINK_TEXT_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
BREAKING_HEADING_RE = re.compile(
    r"(?:#+\s*.*breaking\s+change|breaking\s+changes?\s*⚠️|BREAKING\s+CHANGE\s*:)",
    re.IGNORECASE,
)
NOTABLE_HEADING_RE = re.compile(r"#+\s*.*notable\s+changes", re.IGNORECASE)
SEMVER_PARTS_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
DASHBOARD_MANAGER_RE = re.compile(r"<summary>([A-Za-z0-9_\-]+)\s*\(\d+\)</summary>")


def inspect_dependency_dashboard(repo_path: str) -> DependencyDashboardInfo:
    res = run_command(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "all",
            "--search",
            "Dependency Dashboard in:title",
            "--json",
            "number,title,state,body",
            "--limit",
            "10",
        ],
        cwd=repo_path,
        silent=True,
    )
    if res.returncode != 0:
        return DependencyDashboardInfo(
            status=STATUS_UNKNOWN,
            details=f"Could not query GitHub issues via gh: {describe_command_failure(res)}",
        )

    try:
        issues = json.loads(res.stdout or "[]")
    except Exception:
        return DependencyDashboardInfo(
            status=STATUS_UNKNOWN,
            details="Invalid JSON returned by gh issue list.",
        )

    if not isinstance(issues, list):
        return DependencyDashboardInfo(status=STATUS_UNKNOWN, details="Unexpected response from gh issue list.")

    dashboard_issue = None
    for issue in issues:
        if isinstance(issue, dict) and issue.get("title", "").strip() == "Dependency Dashboard":
            dashboard_issue = issue
            break
    if dashboard_issue is None and issues:
        for issue in issues:
            if isinstance(issue, dict) and "dependency dashboard" in issue.get("title", "").lower():
                dashboard_issue = issue
                break

    if dashboard_issue is None:
        return DependencyDashboardInfo(
            status=STATUS_UNKNOWN,
            state="MISSING",
            details="No Renovate Dependency Dashboard issue found on GitHub.",
        )

    number = int(dashboard_issue.get("number") or 0)
    state = str(dashboard_issue.get("state") or STATUS_UNKNOWN).upper()
    body = str(dashboard_issue.get("body") or "")

    no_open_or_pending = "This repository currently has no open or pending branches." in body
    detected_managers = DASHBOARD_MANAGER_RE.findall(body)

    open_items: list[str] = []
    pending_items: list[str] = []
    current_section = ""

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            current_section = line[3:].strip().lower()
            continue
        if not (line.startswith("- [ ]") or line.startswith("- [x]")):
            continue
        if "<!-- manual job -->" in line:
            continue
        cleaned_item = re.sub(r"<!--[^>]+-->", "", line[5:]).strip()
        cleaned_item = MARKDOWN_LINK_TEXT_RE.sub(r"\1", cleaned_item)
        if not cleaned_item:
            continue
        if any(k in current_section for k in ("rate-limited", "rate limited", "pending", "awaited", "deferred", "other")):
            pending_items.append(cleaned_item)
        else:
            open_items.append(cleaned_item)

    status = STATUS_PASS if state == "OPEN" else STATUS_FAIL
    details = (
        "No open or pending branches on Dependency Dashboard."
        if no_open_or_pending
        else f"Dashboard #{number} ({state}): {len(open_items)} open item(s), {len(pending_items)} pending/rate-limited."
    )

    return DependencyDashboardInfo(
        status=status,
        issue_number=number,
        state=state,
        no_open_or_pending=no_open_or_pending,
        open_items=open_items,
        pending_or_rate_limited_items=pending_items,
        detected_managers=detected_managers,
        details=details,
    )


def infer_semver_update_type(from_ver: str, to_ver: str) -> str:
    m1 = SEMVER_PARTS_RE.match((from_ver or "").strip())
    m2 = SEMVER_PARTS_RE.match((to_ver or "").strip())
    if not m1 or not m2:
        return "unknown"
    maj1, min1, pat1 = int(m1.group(1)), int(m1.group(2) or 0), int(m1.group(3) or 0)
    maj2, min2, pat2 = int(m2.group(1)), int(m2.group(2) or 0), int(m2.group(3) or 0)
    if maj2 != maj1:
        return "major"
    if min2 != min1:
        return "minor"
    if pat2 != pat1:
        return "patch"
    return "pin"


def _clean_table_cell(cell: str) -> str:
    text = MARKDOWN_LINK_TEXT_RE.sub(r"\1", cell)
    text = re.sub(r"\s*\([^)]*\)", "", text)
    return text.strip().strip("`")


def parse_renovate_pr_body(title: str, body: str) -> tuple[list[RenovateUpdateItem], bool, bool, str]:
    text = body or ""
    updates: list[RenovateUpdateItem] = []

    lines = text.splitlines()
    header_cols: list[str] = []
    in_table = False

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            if in_table and updates:
                break
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]
        lower_cells = [MARKDOWN_LINK_TEXT_RE.sub(r"\1", c).strip().lower() for c in cells]

        if not in_table:
            if "package" in lower_cells and "change" in lower_cells:
                header_cols = lower_cells
                in_table = True
            continue

        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue

        col_map = {header_cols[i]: cells[i] for i in range(min(len(header_cols), len(cells)))}
        raw_pkg = col_map.get("package", "")
        raw_change = col_map.get("change", "")
        raw_update = col_map.get("update", "").strip().lower()

        pkg_name = _clean_table_cell(raw_pkg)
        change_text = MARKDOWN_LINK_TEXT_RE.sub(r"\1", raw_change).strip()
        arrow_m = CHANGE_ARROW_RE.search(change_text)
        from_ver = arrow_m.group(1) if arrow_m else ""
        to_ver = arrow_m.group(2) if arrow_m else change_text.strip("`")

        update_type = raw_update if raw_update in {"major", "minor", "patch", "pin", "digest"} else ""
        if not update_type and from_ver and to_ver:
            update_type = infer_semver_update_type(from_ver, to_ver)
        if not update_type:
            update_type = "unknown"

        if pkg_name:
            updates.append(
                RenovateUpdateItem(
                    package=pkg_name,
                    update_type=update_type,
                    from_version=from_ver,
                    to_version=to_ver,
                    raw_change=f"{from_ver} -> {to_ver}" if from_ver and to_ver else change_text,
                )
            )

    if not updates and title:
        m_bump = re.search(
            r"(?:update|bump)\s+(?:dependency\s+)?([^\s]+)\s+(?:from\s+v?([^\s]+)\s+)?to\s+v?([^\s]+)",
            title,
            re.IGNORECASE,
        )
        if m_bump:
            pkg = m_bump.group(1)
            from_v = m_bump.group(2) or ""
            to_v = m_bump.group(3)
            u_type = infer_semver_update_type(from_v, to_v) if from_v else "unknown"
            updates.append(
                RenovateUpdateItem(
                    package=pkg,
                    update_type=u_type,
                    from_version=from_v,
                    to_version=to_v,
                    raw_change=f"{from_v} -> {to_v}" if from_v else f"-> {to_v}",
                )
            )

    has_warning = "> [!WARNING]" in text or "> [!CAUTION]" in text
    has_release_notes = "### Release Notes" in text or "<details>" in text

    release_notes_excerpt = ""
    rn_index = text.find("### Release Notes")
    if rn_index != -1:
        end_index = text.find("### Configuration", rn_index)
        section = text[rn_index : end_index if end_index != -1 else len(text)]
        release_notes_excerpt = section.strip()
    elif text.strip():
        release_notes_excerpt = text.strip()

    return updates, has_release_notes, has_warning, release_notes_excerpt


def compute_checks_status(rollup: object) -> str:
    if not isinstance(rollup, list) or not rollup:
        return STATUS_UNKNOWN

    states: list[str] = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        conclusion = str(item.get("conclusion") or "").upper()
        status = str(item.get("status") or "").upper()
        state = str(item.get("state") or "").upper()

        if conclusion in {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"} or state in {
            "FAILURE",
            "ERROR",
        }:
            states.append(STATUS_FAIL)
        elif status in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"} or state == "PENDING":
            states.append(STATUS_PENDING)
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} or state == "SUCCESS":
            states.append(STATUS_PASS)

    if not states:
        return STATUS_UNKNOWN
    if STATUS_FAIL in states:
        return STATUS_FAIL
    if STATUS_PENDING in states:
        return STATUS_PENDING
    return STATUS_PASS


def _match_package_to_component(pkg_name: str) -> str | None:
    lower = pkg_name.lower()
    if lower in {"python", "actions/setup-python"} or lower.endswith("/python"):
        return "Python"
    if lower in {"node", "actions/setup-node"} or lower.endswith("/node"):
        return "Node.js"
    if lower in {"ruby", "ruby/setup-ruby"} or lower.endswith("/ruby"):
        return "Ruby"
    if "postgres" in lower or lower in {"pg", "psycopg2", "psycopg2-binary"}:
        return "PostgreSQL"
    if "playwright" in lower:
        return "Playwright"
    return None


def _build_deterministic_pr_observations(
    pr: RenovatePRInfo,
    env_matrix: list[EnvironmentMatrixRow],
    renovate_config: RenovateConfigInfo,
) -> tuple[str, bool, list[str], list[str], list[str]]:
    key_changes: list[str] = []
    impacts: list[str] = []
    checks: list[str] = []
    breaking = False
    risk = RISK_LOW

    if pr.is_onboarding:
        risk = RISK_REVIEW
        key_changes.append("PR d'initialisation Renovate ('Configure Renovate').")
        if renovate_config.exists and pr.base_branch in {"master", "main"}:
            impacts.append(
                f"Le fichier '{renovate_config.path}' existe déjà sur la branche courante, mais cette PR cible '{pr.base_branch}'."
            )
            checks.append(
                f"Préférez fusionner votre configuration de 'staging' vers '{pr.base_branch}' plutôt que d'écraser le preset partagé."
            )
        else:
            checks.append("Vérifier que la configuration hérite bien du preset Dev Platform avant fusion.")
        return risk, breaking, key_changes, impacts, checks

    for upd in pr.updates:
        change_desc = f"{upd.package}: {upd.from_version} -> {upd.to_version} ({upd.update_type})"
        key_changes.append(change_desc)
        if upd.update_type == "major":
            if risk == RISK_LOW:
                risk = RISK_REVIEW

    if BREAKING_HEADING_RE.search(pr.release_notes_excerpt):
        breaking = True
        risk = RISK_BREAKING
        key_changes.append("Section 'Breaking Changes' détectée dans les notes de version Renovate.")

    if NOTABLE_HEADING_RE.search(pr.release_notes_excerpt) and risk == RISK_LOW:
        risk = RISK_REVIEW
        key_changes.append("Section 'Notable Changes' présente dans les notes de version.")

    if pr.has_renovate_warning:
        if risk == RISK_LOW:
            risk = RISK_REVIEW
        impacts.append(
            "Renovate signale un avertissement dans la PR (ex: dépendance ou registre privé non résolu)."
        )
        checks.append("Consulter le Dependency Dashboard pour vérifier l'avertissement Renovate.")

    matrix_by_comp = {row.component: row for row in env_matrix}
    for upd in pr.updates:
        comp = _match_package_to_component(upd.package)
        if not comp or comp not in matrix_by_comp:
            continue
        row = matrix_by_comp[comp]
        target_ver = upd.to_version.lstrip("v^~")
        if comp == "Playwright" and target_ver:
            for env_label, env_val in (("Jenkins", row.jenkins), ("Docker", row.docker), ("GitHub Actions", row.github_actions)):
                if env_val not in {STATUS_UNKNOWN, "N/A"} and target_ver not in env_val:
                    if risk == RISK_LOW:
                        risk = RISK_REVIEW
                    impacts.append(
                        f"Désalignement Playwright potentiel : la PR cible v{target_ver}, mais {env_label} déclare l'image Playwright {env_val}."
                    )
                    checks.append(
                        f"Aligner l'image Docker Playwright dans {env_label} sur v{target_ver} pour éviter un échec des tests E2E."
                    )
        elif comp in {"Python", "Node.js", "Ruby"} and upd.package.lower() in {"python", "node", "ruby"}:
            for env_label, env_val in (("Jenkins", row.jenkins), ("Docker", row.docker), ("GitHub Actions", row.github_actions)):
                if env_val not in {STATUS_UNKNOWN, "N/A"} and target_ver and target_ver not in env_val:
                    if risk == RISK_LOW:
                        risk = RISK_REVIEW
                    impacts.append(
                        f"Cette PR met à jour {comp} vers {target_ver}, mais {env_label} est toujours configuré en {env_val}."
                    )
                    checks.append(
                        f"Vérifier l'alignement de {comp} entre GitHub Actions, Jenkins, Docker et le VPS avant déploiement."
                    )
        elif comp == "PostgreSQL" and upd.package.lower() == "postgres":
            if risk == RISK_LOW:
                risk = RISK_REVIEW
            impacts.append(
                f"Mise à jour de l'image PostgreSQL vers {target_ver} dans les fichiers touchés ({', '.join(pr.changed_files) or 'CI'})."
            )
            checks.append(
                "Vérifier la compatibilité avec la version PostgreSQL de production (sans forcer de migration DB en prod)."
            )

    if pr.ci_status == STATUS_FAIL:
        risk = RISK_BLOCKED
        checks.append("La CI de cette PR est en échec (FAIL) : corriger ou analyser les logs avant toute fusion.")
    elif pr.ci_status == STATUS_PENDING:
        checks.append("Attendre la fin des contrôles CI en cours (PENDING) avant de fusionner.")
    elif pr.ci_status == STATUS_UNKNOWN:
        checks.append("Aucun check CI rapporté sur la PR : vérifier Jenkins / les tests localement avant fusion.")
    else:
        checks.append("Confirmer que la couverture de tests couvre le composant mis à jour.")

    if not pr.has_release_notes and not pr.is_onboarding:
        if risk == RISK_LOW and any(u.update_type in {"major", "minor", "unknown"} for u in pr.updates):
            risk = RISK_REVIEW
        checks.append("Les notes de version ne sont pas intégrées à la PR : vérifier le changelog officiel du paquet.")

    return risk, breaking, key_changes, impacts, checks


def build_fallback_pr_analysis(
    pr: RenovatePRInfo,
    env_matrix: list[EnvironmentMatrixRow],
    renovate_config: RenovateConfigInfo,
) -> RenovatePRAnalysis:
    risk, breaking, key_changes, impacts, checks = _build_deterministic_pr_observations(
        pr, env_matrix, renovate_config
    )

    if pr.is_onboarding:
        summary = (
            f"PR d'onboarding Renovate (#{pr.number}) ciblant '{pr.base_branch}'. "
            "Elle sert à initialiser Renovate sur le dépôt."
        )
    elif pr.updates:
        pkgs = ", ".join(f"{u.package} ({u.raw_change})" for u in pr.updates)
        summary = f"Mise à jour de dépendance(s) proposée par Renovate vers '{pr.base_branch}' : {pkgs}."
    else:
        summary = f"PR Renovate #{pr.number} ({pr.title}) ciblant '{pr.base_branch}'."

    if breaking:
        breaking_summary = (
            "Des mentions explicites de 'Breaking Changes' figurent dans les notes de version de cette PR. "
            "Une vérification manuelle des impacts est requise."
        )
    elif any(u.update_type == "major" for u in pr.updates):
        breaking_summary = (
            "Hausse de version majeure (major) : aucun breaking change explicite détecté automatiquement, "
            "mais les notes de version doivent être relues avant fusion."
        )
    elif not pr.has_release_notes and not pr.is_onboarding:
        breaking_summary = (
            "Aucun breaking change détecté dans le diff, mais les notes de version ne sont pas incluses dans la PR."
        )
    else:
        breaking_summary = "Aucun breaking change signalé dans les notes de version analysées."

    if not impacts:
        impacts.append(
            f"Fichiers modifiés : {', '.join(pr.changed_files) if pr.changed_files else 'aucun fichier listé'}."
        )

    if risk == RISK_BLOCKED:
        verdict = "Fusion bloquée : la CI est en échec. Ne pas merger en l'état."
    elif risk == RISK_BREAKING:
        verdict = "Attention : breaking changes documentés dans les release notes. Vérifier les impacts listés avant de merger."
    elif risk == RISK_REVIEW:
        verdict = "Revue recommandée : vérifier les points d'alignement CI/environnement avant de valider le merge."
    else:
        verdict = (
            "Mise à jour à faible risque apparent. Vous pouvez merger sereinement sur staging "
            "une fois les contrôles CI au vert."
        )

    return RenovatePRAnalysis(
        risk_level=risk,
        breaking_change=breaking,
        breaking_summary=breaking_summary,
        summary=summary,
        key_changes=key_changes,
        project_impacts=impacts,
        checks_before_merge=checks,
        verdict=verdict,
        source="heuristic",
    )


def _parse_ollama_pr_analysis(
    raw: str,
    *,
    deterministic_risk: str,
    deterministic_breaking: bool,
    deterministic_impacts: list[str],
    deterministic_checks: list[str],
) -> RenovatePRAnalysis | None:
    data = safe_parse_json(raw)
    if not isinstance(data, dict):
        return None

    item = data.get("pr_analysis") or data.get("analysis") or data.get("review")
    if not isinstance(item, dict):
        if any(k in data for k in ("summary", "overview", "risk_level", "breaking_summary", "verdict")):
            item = data
        else:
            return None

    raw_risk = str(item.get("risk_level") or "").strip().upper()
    if raw_risk not in VALID_RISK_LEVELS:
        raw_risk = deterministic_risk

    risk_priority = {
        RISK_LOW: 1,
        RISK_UNKNOWN: 2,
        RISK_REVIEW: 3,
        RISK_BREAKING: 4,
        RISK_BLOCKED: 5,
    }
    final_risk = (
        deterministic_risk
        if risk_priority.get(deterministic_risk, 0) > risk_priority.get(raw_risk, 0)
        else raw_risk
    )

    model_breaking = bool(item.get("breaking_change")) if isinstance(item.get("breaking_change"), bool) else False
    final_breaking = model_breaking or deterministic_breaking

    summary = str(item.get("summary") or item.get("overview") or item.get("description") or "").strip()
    breaking_summary = str(item.get("breaking_summary") or item.get("breaking_details") or "").strip()
    verdict = str(item.get("verdict") or item.get("recommendation") or item.get("conclusion") or "").strip()
    if not summary and not breaking_summary:
        return None
    if not summary:
        summary = breaking_summary
    if not verdict:
        verdict = (
            "Attention : vérifier les impacts avant fusion."
            if final_risk in {RISK_BREAKING, RISK_BLOCKED, RISK_REVIEW}
            else "Mise à jour prête à être fusionnée après vérification CI."
        )

    def _clean_list(val: object) -> list[str]:
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        if not isinstance(val, list):
            return []
        return [str(x).strip() for x in val if isinstance(x, str) and str(x).strip()][:8]

    key_changes = _clean_list(item.get("key_changes"))
    impacts = _clean_list(item.get("project_impacts"))
    checks = _clean_list(item.get("checks_before_merge"))

    combined_impacts_lower = " ".join(impacts).lower()
    for det_impact in deterministic_impacts:
        if det_impact not in impacts:
            if "jenkins" in det_impact.lower() and "jenkins" in combined_impacts_lower:
                continue
            if "avertissement" in det_impact.lower() and (
                "avertissement" in combined_impacts_lower or "warning" in combined_impacts_lower
            ):
                continue
            impacts.insert(0, det_impact)

    if not checks:
        checks = list(deterministic_checks)

    return RenovatePRAnalysis(
        risk_level=final_risk,
        breaking_change=final_breaking,
        breaking_summary=breaking_summary or "Aucun breaking change signalé dans l'analyse.",
        summary=summary,
        key_changes=key_changes,
        project_impacts=impacts,
        checks_before_merge=checks,
        verdict=verdict,
        source="ollama",
    )


def analyze_renovate_pr(
    repo_name: str,
    repo_path: str,
    pr: RenovatePRInfo,
    env_matrix: list[EnvironmentMatrixRow],
    renovate_config: RenovateConfigInfo,
    *,
    pr_diff: str = "",
    show_spinner: bool = True,
) -> RenovatePRAnalysis:
    fallback = build_fallback_pr_analysis(pr, env_matrix, renovate_config)
    if not is_ollama_enabled():
        return fallback

    try:
        ensure_repo_context_allowed("Renovate PR analysis context")

        max_chars = env_int("OLLAMA_MAX_REVIEW_DIFF_CHARS", 12000, minimum=1000)
        project_context_lines = [
            f"- {row.component}: Local={row.local}, GHA={row.github_actions}, Jenkins={row.jenkins}, Docker={row.docker}, Prod={row.production} ({row.drift_status})"
            for row in env_matrix
        ] or ["- (no declared runtime matrix)"]

        files_block = "\n".join(f"- {f}" for f in pr.changed_files) or "- (none)"
        updates_block = (
            "\n".join(
                f"- {u.package}: {u.from_version or '?'} -> {u.to_version or '?'} (type: {u.update_type})"
                for u in pr.updates
            )
            or "- (none parsed)"
        )
        det_notes_lines = (
            [f"- Risk baseline: {fallback.risk_level} (breaking={fallback.breaking_change})"]
            + [f"- Impact: {imp}" for imp in fallback.project_impacts]
            + [f"- Check: {chk}" for chk in fallback.checks_before_merge]
        )

        user_prompt = RENOVATE_PR_REVIEW_USER_TEMPLATE.format(
            repo=repo_name,
            pr_number=pr.number,
            pr_title=pr.title,
            head_branch=pr.head_branch,
            base_branch=pr.base_branch,
            ci_status=pr.ci_status,
            merge_state=pr.merge_state,
            project_context="\n".join(project_context_lines),
            files=files_block,
            updates=updates_block,
            deterministic_notes="\n".join(det_notes_lines),
            release_notes=trim_text_middle(pr.release_notes_excerpt or "(no release notes in PR body)", max_chars // 2),
            diff=trim_text_middle(pr_diff or "(diff not fetched)", max_chars // 2),
        )
        messages = [
            {"role": "system", "content": RENOVATE_PR_REVIEW_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        status_ctx_1 = (
            console.status(f"[bold cyan]🤖 Analyzing Renovate PR #{pr.number} with Ollama...[/]", spinner="dots")
            if show_spinner
            else nullcontext()
        )
        with status_ctx_1:
            raw = chat_json(messages, temperature=0.2, json_mode=True)
        debug_log_output(f"Renovate PR #{pr.number} analysis (attempt 1)", raw)

        parsed = _parse_ollama_pr_analysis(
            raw,
            deterministic_risk=fallback.risk_level,
            deterministic_breaking=fallback.breaking_change,
            deterministic_impacts=fallback.project_impacts,
            deterministic_checks=fallback.checks_before_merge,
        )
        if parsed:
            return parsed

        status_ctx_2 = (
            console.status(f"[bold cyan]🤖 Retrying Renovate PR #{pr.number} analysis...[/]", spinner="dots")
            if show_spinner
            else nullcontext()
        )
        with status_ctx_2:
            raw_retry = chat_json(messages, temperature=0.0, json_mode=True)
        debug_log_output(f"Renovate PR #{pr.number} analysis (attempt 2)", raw_retry)

        parsed_retry = _parse_ollama_pr_analysis(
            raw_retry,
            deterministic_risk=fallback.risk_level,
            deterministic_breaking=fallback.breaking_change,
            deterministic_impacts=fallback.project_impacts,
            deterministic_checks=fallback.checks_before_merge,
        )
        if parsed_retry:
            return parsed_retry

        return fallback
    except OllamaError as error:
        print(f"⚠️ Ollama unavailable for PR #{pr.number} analysis, using deterministic analysis. Reason: {error}")
        return fallback
    except Exception as error:
        print(f"⚠️ Ollama analysis failed for PR #{pr.number}, using deterministic analysis. Reason: {error}")
        return fallback


def fetch_pr_diff(repo_path: str, pr_number: int) -> str:
    res = run_command(
        ["gh", "pr", "diff", str(pr_number)],
        cwd=repo_path,
        silent=True,
        max_output_chars=env_int("OLLAMA_MAX_REVIEW_DIFF_CHARS", 12000, minimum=1000),
    )
    if res.returncode != 0:
        return ""
    return (res.stdout or "").strip()


def _is_dependency_pr(item: dict) -> bool:
    author_obj = item.get("author")
    login = str(author_obj.get("login") or "").lower() if isinstance(author_obj, dict) else ""
    head_ref = str(item.get("headRefName") or "").lower()
    title = str(item.get("title") or "").lower()

    if "renovate" in login or "dependabot" in login:
        return True
    if head_ref.startswith(("renovate/", "dependabot/")):
        return True
    if title.startswith(("chore(deps)", "fix(deps)", "build(deps)")) or "configure renovate" in title:
        return True
    return False


def _build_pr_info_from_gh_item(
    item: dict,
    *,
    repo_name: str,
    repo_path: str,
    env_matrix: list[EnvironmentMatrixRow],
    renovate_config: RenovateConfigInfo,
    include_ollama_analysis: bool = True,
    fetch_diff_for_analysis: bool = True,
    show_spinner: bool = True,
) -> RenovatePRInfo:
    number = int(item.get("number") or 0)
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    author_obj = item.get("author")
    author = str(author_obj.get("login") or "") if isinstance(author_obj, dict) else ""
    head_branch = str(item.get("headRefName") or "").strip()
    base_branch = str(item.get("baseRefName") or "").strip()
    state = str(item.get("state") or "OPEN").upper()
    merge_state = str(item.get("mergeStateStatus") or STATUS_UNKNOWN).upper()
    body = str(item.get("body") or "")

    raw_files = item.get("files")
    changed_files: list[str] = []
    if isinstance(raw_files, list):
        for f in raw_files:
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                changed_files.append(f["path"])

    is_onboarding = head_branch == "renovate/configure" or "configure renovate" in title.lower()
    ci_status = compute_checks_status(item.get("statusCheckRollup"))
    updates, has_rn, has_warn, rn_excerpt = parse_renovate_pr_body(title, body)

    pr_info = RenovatePRInfo(
        number=number,
        title=title,
        url=url,
        author=author,
        head_branch=head_branch,
        base_branch=base_branch,
        state=state,
        is_onboarding=is_onboarding,
        ci_status=ci_status,
        merge_state=merge_state,
        changed_files=changed_files,
        updates=updates,
        has_release_notes=has_rn,
        has_renovate_warning=has_warn,
        release_notes_excerpt=rn_excerpt,
        analysis=None,
    )

    if include_ollama_analysis:
        pr_diff = fetch_pr_diff(repo_path, number) if (fetch_diff_for_analysis and is_ollama_enabled()) else ""
        analysis = analyze_renovate_pr(
            repo_name,
            repo_path,
            pr_info,
            env_matrix,
            renovate_config,
            pr_diff=pr_diff,
            show_spinner=show_spinner,
        )
    else:
        analysis = build_fallback_pr_analysis(pr_info, env_matrix, renovate_config)

    return RenovatePRInfo(
        number=pr_info.number,
        title=pr_info.title,
        url=pr_info.url,
        author=pr_info.author,
        head_branch=pr_info.head_branch,
        base_branch=pr_info.base_branch,
        state=pr_info.state,
        is_onboarding=pr_info.is_onboarding,
        ci_status=pr_info.ci_status,
        merge_state=pr_info.merge_state,
        changed_files=pr_info.changed_files,
        updates=pr_info.updates,
        has_release_notes=pr_info.has_release_notes,
        has_renovate_warning=pr_info.has_renovate_warning,
        release_notes_excerpt=pr_info.release_notes_excerpt,
        analysis=analysis,
    )


def inspect_open_renovate_prs(
    repo_path: str,
    repo_name: str,
    env_matrix: list[EnvironmentMatrixRow],
    renovate_config: RenovateConfigInfo,
    *,
    include_ollama_analysis: bool = True,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[list[RenovatePRInfo], list[str]]:
    res = run_command(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,url,author,baseRefName,headRefName,state,mergeStateStatus,statusCheckRollup,body,files",
            "--limit",
            "25",
        ],
        cwd=repo_path,
        silent=True,
    )
    if res.returncode != 0:
        return [], [f"Could not query open PRs via gh: {describe_command_failure(res)}"]

    try:
        items = json.loads(res.stdout or "[]")
    except Exception:
        return [], ["Invalid JSON returned by gh pr list."]

    if not isinstance(items, list):
        return [], ["Unexpected response shape from gh pr list."]

    prs: list[RenovatePRInfo] = []
    warnings: list[str] = []
    dep_items = [item for item in items if isinstance(item, dict) and _is_dependency_pr(item)]

    for idx, item in enumerate(dep_items, start=1):
        pr_num = item.get("number", "?")
        if progress_callback is not None:
            progress_callback(f"Analyse IA Ollama PR #{pr_num} ({idx}/{len(dep_items)})...")
        pr_info = _build_pr_info_from_gh_item(
            item,
            repo_name=repo_name,
            repo_path=repo_path,
            env_matrix=env_matrix,
            renovate_config=renovate_config,
            include_ollama_analysis=include_ollama_analysis,
            show_spinner=progress_callback is None,
        )
        prs.append(pr_info)
        if pr_info.is_onboarding and renovate_config.exists:
            warnings.append(
                f"Onboarding PR #{pr_info.number} ('{pr_info.title}') targets '{pr_info.base_branch}' "
                f"while '{renovate_config.path}' already exists locally. Merge staging -> {pr_info.base_branch} to activate Renovate on staging."
            )

    return prs, warnings


def inspect_single_renovate_pr(
    repo_path: str,
    repo_name: str,
    pr_number: int,
    env_matrix: list[EnvironmentMatrixRow],
    renovate_config: RenovateConfigInfo,
) -> RenovatePRInfo | None:
    res = run_command(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,title,url,author,baseRefName,headRefName,state,mergeStateStatus,statusCheckRollup,body,files",
        ],
        cwd=repo_path,
        silent=True,
    )
    if res.returncode != 0:
        return None
    try:
        item = json.loads(res.stdout or "{}")
    except Exception:
        return None
    if not isinstance(item, dict) or not item.get("number"):
        return None

    return _build_pr_info_from_gh_item(
        item,
        repo_name=repo_name,
        repo_path=repo_path,
        env_matrix=env_matrix,
        renovate_config=renovate_config,
        include_ollama_analysis=True,
        fetch_diff_for_analysis=True,
    )


def inspect_ci_status(repo_path: str) -> CIStatusSummary:
    repo_root = Path(repo_path)
    workflows_dir = repo_root / ".github" / "workflows"
    workflows: list[str] = []
    if workflows_dir.is_dir():
        workflows = [_rel_path(repo_root, w) for w in sorted(workflows_dir.glob("*.y*ml")) if w.is_file()]

    gha_status = STATUS_UNKNOWN
    gha_details = "No GitHub Actions workflows configured in repository."
    if workflows:
        res = run_command(
            [
                "gh",
                "run",
                "list",
                "--limit",
                "3",
                "--json",
                "databaseId,workflowName,status,conclusion,headBranch",
            ],
            cwd=repo_path,
            silent=True,
        )
        if res.returncode == 0:
            try:
                runs = json.loads(res.stdout or "[]")
            except Exception:
                runs = []
            if isinstance(runs, list) and runs:
                latest = runs[0] if isinstance(runs[0], dict) else {}
                conclusion = str(latest.get("conclusion") or "").upper()
                status = str(latest.get("status") or "").upper()
                branch = str(latest.get("headBranch") or "")
                wf_name = str(latest.get("workflowName") or "workflow")
                if conclusion == "SUCCESS":
                    gha_status = STATUS_PASS
                elif conclusion in {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE"}:
                    gha_status = STATUS_FAIL
                elif status in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"}:
                    gha_status = STATUS_PENDING
                gha_details = f"Latest run '{wf_name}' on '{branch}': {gha_status} ({conclusion or status})."
            else:
                gha_details = "Workflows present locally, but no GitHub Actions runs returned by gh."
        else:
            gha_details = f"GitHub Actions status unavailable via gh: {describe_command_failure(res)}"

    jenkinsfile = repo_root / "Jenkinsfile"
    if jenkinsfile.is_file():
        jenkins_status = STATUS_UNKNOWN
        jenkins_file: str | None = "Jenkinsfile"
        jenkins_details = "Jenkinsfile present; remote Jenkins API status not configured in Phase 1 (UNKNOWN)."
    else:
        jenkins_status = STATUS_UNKNOWN
        jenkins_file = None
        jenkins_details = "No Jenkinsfile in repository."

    return CIStatusSummary(
        github_actions_status=gha_status,
        github_actions_workflows=workflows,
        github_actions_details=gha_details,
        jenkins_status=jenkins_status,
        jenkins_file=jenkins_file,
        jenkins_details=jenkins_details,
    )
