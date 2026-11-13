import re
from typing import Any

from core.maintenance.models import (
    DRIFT_ACCEPTED,
    DRIFT_ALIGNED,
    DRIFT_INFO_UNAVAILABLE,
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
    RISK_LOW,
    RISK_REVIEW,
    RISK_UNKNOWN,
    STATUS_FAIL,
    STATUS_PENDING,
    STATUS_UNKNOWN,
    AuditReport,
    EnvironmentMatrixRow,
    RenovatePRInfo,
    UpdateRiskAssessment,
)
from core.maintenance.renovate import BREAKING_HEADING_RE, infer_semver_update_type

DEPRECATION_RE = re.compile(r"\b(deprecat(?:ed|ion)|obsolete|removed\s+in)\b", re.IGNORECASE)
RUNTIME_CONSTRAINT_RE = re.compile(
    r"(?:requires?\s+|minimum\s+|drop(?:ped|s)?\s+support\s+for\s+|node(?:\.js)?\s*>=?\s*|python\s*>=?\s*)"
    r"(?:python|node(?:\.js)?|ruby|postgres(?:ql)?)?\s*([><=~^]*\s*v?\d+(?:\.\d+){0,2})",
    re.IGNORECASE,
)
DASHBOARD_BUMP_RE = re.compile(
    r"(?:update|bump)\s+(?:dependency\s+|docker\s+tag\s+)?([^\s]+)\s+"
    r"(?:from\s+v?([0-9A-Za-z.\-_]+)\s+)?to\s+v?([0-9A-Za-z.\-_]+)",
    re.IGNORECASE,
)
VERSION_TUPLE_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")

RISK_PRIORITY = {
    RISK_BLOCKED: 5,
    RISK_BREAKING: 4,
    RISK_REVIEW: 3,
    RISK_UNKNOWN: 2,
    RISK_LOW: 1,
}


def _unique_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _semver_key(version: str) -> tuple[int, int, int]:
    cleaned = re.sub(r"^[~^>=<]+", "", (version or "").strip()).lstrip("v")
    match = VERSION_TUPLE_RE.match(cleaned)
    if not match:
        return (0, 0, 0)
    return (
        int(match.group(1) or 0),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def _source_files_from_matrix_row(row: EnvironmentMatrixRow) -> list[str]:
    files: list[str] = []
    for src in row.sources:
        # Format is "<Environment>: <path_or_ref> (<version>)"
        if ":" in src:
            _, rest = src.split(":", 1)
            file_part = rest.strip().split(" (", 1)[0].strip()
            if "->" in file_part:
                file_part = file_part.split("->", 1)[0].strip()
            file_part = file_part.split(":L", 1)[0].split("#", 1)[0].strip()
            if file_part:
                files.append(file_part)
    return _unique_list(files)


def extract_release_notes_signals(release_notes: str) -> dict[str, Any]:
    text = (release_notes or "").strip()
    if not text:
        return {
            "has_breaking_marker": False,
            "breaking_lines": [],
            "deprecation_lines": [],
            "runtime_constraints": [],
            "uncertainties": ["Release notes are missing or not embedded in the PR body."],
        }

    breaking_lines: list[str] = []
    deprecation_lines: list[str] = []
    runtime_constraints: list[str] = []

    has_breaking_heading = bool(BREAKING_HEADING_RE.search(text))
    in_breaking_section = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if BREAKING_HEADING_RE.search(line):
                in_breaking_section = True
                breaking_lines.append(line.lstrip("#").strip())
                continue
            in_breaking_section = False

        if in_breaking_section and line.startswith(("-", "*")):
            breaking_lines.append(line.lstrip("-* ").strip())
        elif BREAKING_HEADING_RE.search(line):
            breaking_lines.append(line)

        if DEPRECATION_RE.search(line):
            deprecation_lines.append(line.lstrip("-*# ").strip())

        if RUNTIME_CONSTRAINT_RE.search(line) and any(
            kw in line.lower() for kw in ("python", "node", "ruby", "postgres", "engine")
        ):
            runtime_constraints.append(line.lstrip("-*# ").strip())

    return {
        "has_breaking_marker": has_breaking_heading or bool(breaking_lines),
        "breaking_lines": _unique_list(breaking_lines)[:5],
        "deprecation_lines": _unique_list(deprecation_lines)[:5],
        "runtime_constraints": _unique_list(runtime_constraints)[:5],
        "uncertainties": [],
    }


def parse_dashboard_pending_update(raw_item: str) -> tuple[str, str, str, str]:
    text = (raw_item or "").strip()
    match = DASHBOARD_BUMP_RE.search(text)
    if not match:
        return text, STATUS_UNKNOWN, STATUS_UNKNOWN, "unknown"
    pkg = match.group(1).strip("`")
    from_ver = (match.group(2) or "").strip("`") or STATUS_UNKNOWN
    to_ver = match.group(3).strip("`")
    update_type = infer_semver_update_type(from_ver, to_ver) if from_ver != STATUS_UNKNOWN else "unknown"
    return pkg, from_ver, to_ver, update_type


def _classify_open_pr(pr: RenovatePRInfo, report: AuditReport) -> UpdateRiskAssessment:
    signals = extract_release_notes_signals(pr.release_notes_excerpt)
    analysis = pr.analysis

    if pr.is_onboarding:
        sources = [f"GitHub PR #{pr.number} ({pr.head_branch} -> {pr.base_branch})"]
        affected = _unique_list(list(pr.changed_files) + ([report.renovate_config.path] if report.renovate_config.exists else []))
        uncertainties: list[str] = []
        if report.renovate_config.exists and pr.base_branch in {"master", "main"}:
            recommendation = (
                f"Ne pas fusionner la PR d'onboarding #{pr.number} telle quelle si elle écrase votre preset : "
                f"fusionnez d'abord 'staging' vers '{pr.base_branch}' pour activer '{report.renovate_config.path}'."
            )
            sources.append(f"Local config: {report.renovate_config.path}")
        else:
            recommendation = (
                f"Vérifier que la PR #{pr.number} hérite du preset central Dev Platform avant fusion."
            )
        return UpdateRiskAssessment(
            subject=f"PR #{pr.number} · Renovate Onboarding",
            category="renovate_config",
            current_version="unconfigured" if not report.renovate_config.exists else "configured on staging",
            proposed_version="renovate.json",
            target_version="Dev Platform preset",
            update_type="config",
            risk_level=analysis.risk_level if analysis else RISK_REVIEW,
            breaking_change=False,
            recommendation=recommendation,
            affected_files=affected,
            sources=sources,
            uncertainties=uncertainties,
            pr_number=pr.number,
        )

    if pr.updates:
        first = pr.updates[0]
        subject = first.package if len(pr.updates) == 1 else f"{first.package} (+{len(pr.updates) - 1} more)"
        current_ver = first.from_version or STATUS_UNKNOWN
        proposed_ver = first.to_version or STATUS_UNKNOWN
        update_type = first.update_type
    else:
        subject = pr.title
        current_ver = STATUS_UNKNOWN
        proposed_ver = STATUS_UNKNOWN
        update_type = "unknown"

    risk_level = analysis.risk_level if analysis else RISK_REVIEW
    breaking = bool(analysis.breaking_change) if analysis else bool(signals["has_breaking_marker"])
    if signals["has_breaking_marker"]:
        breaking = True
        if risk_level not in {RISK_BLOCKED, RISK_BREAKING}:
            risk_level = RISK_BREAKING

    affected_files = list(pr.changed_files)
    for row in report.env_matrix:
        if row.component.lower() in subject.lower() or (
            "playwright" in subject.lower() and row.component == "Playwright"
        ):
            affected_files.extend(_source_files_from_matrix_row(row))
    affected_files = _unique_list(affected_files)

    sources = [f"GitHub PR #{pr.number} ({pr.head_branch} -> {pr.base_branch}, CI={pr.ci_status})"]
    if pr.has_release_notes:
        sources.append(f"Release notes in PR #{pr.number}")
    if analysis:
        sources.append(f"Analysis source: {analysis.source}")

    uncertainties = list(signals["uncertainties"])
    if pr.ci_status == STATUS_UNKNOWN:
        uncertainties.append("CI status on PR is UNKNOWN (no statusCheckRollup reported).")
    elif pr.ci_status == STATUS_PENDING:
        uncertainties.append("CI checks are still PENDING on GitHub.")
    if pr.has_renovate_warning:
        uncertainties.append("Renovate reported a warning block in the PR description (e.g. private registry lookup).")

    recommendation = (
        analysis.verdict
        if (analysis and analysis.verdict)
        else f"Examiner les changements de la PR #{pr.number} et valider la CI avant fusion vers {pr.base_branch}."
    )
    target_version = proposed_ver if risk_level != RISK_BLOCKED else f"Hold {proposed_ver} until CI passes"

    return UpdateRiskAssessment(
        subject=subject,
        category="dependency_pr",
        current_version=current_ver,
        proposed_version=proposed_ver,
        target_version=target_version,
        update_type=update_type,
        risk_level=risk_level,
        breaking_change=breaking,
        recommendation=recommendation,
        affected_files=affected_files,
        sources=_unique_list(sources),
        uncertainties=_unique_list(uncertainties),
        pr_number=pr.number,
    )


def _recommend_environment_target(row: EnvironmentMatrixRow) -> tuple[str, str]:
    known_candidates: list[str] = []
    for val in (row.local, row.github_actions, row.jenkins, row.docker):
        if val not in {STATUS_UNKNOWN, "N/A", ""}:
            for part in val.split(","):
                cleaned = part.strip()
                if cleaned:
                    known_candidates.append(cleaned)

    if not known_candidates:
        return STATUS_UNKNOWN, STATUS_UNKNOWN

    current_summary = ", ".join(_unique_list(known_candidates))
    if row.component == "PostgreSQL" and row.drift_status == DRIFT_ACCEPTED:
        prod_label = row.production if row.production != STATUS_UNKNOWN else "existing prod DB"
        return current_summary, f"Keep {prod_label} in prod (CI={row.github_actions})"

    best = max(known_candidates, key=_semver_key)
    return current_summary, best


def _classify_environment_row(row: EnvironmentMatrixRow, report: AuditReport) -> UpdateRiskAssessment:
    current_ver, target_ver = _recommend_environment_target(row)
    affected_files = _source_files_from_matrix_row(row)
    if row.component == "Python" and report.deployment_files:
        affected_files = _unique_list(affected_files + list(report.deployment_files))

    sources = list(row.sources) or [f"Environment matrix ({row.component})"]
    uncertainties: list[str] = []
    if row.production == STATUS_UNKNOWN:
        uncertainties.append(
            f"Production {row.component} version is UNKNOWN (no VPS probe configured in Phase 1/2)."
        )

    if row.drift_status == DRIFT_PROBLEMATIC:
        return UpdateRiskAssessment(
            subject=f"Runtime Alignment · {row.component}",
            category="runtime_alignment",
            current_version=current_ver,
            proposed_version=target_ver,
            target_version=target_ver,
            update_type="drift",
            risk_level=RISK_REVIEW,
            breaking_change=False,
            recommendation=(
                f"Aligner les versions de {row.component} sur {target_ver} dans {', '.join(affected_files) or 'les fichiers CI/Docker'} "
                "et vérifier la compatibilité du script de déploiement avant mise en production."
            ),
            affected_files=affected_files,
            sources=sources,
            uncertainties=uncertainties,
        )

    if row.drift_status == DRIFT_ACCEPTED:
        uncertainties.append(
            "Database major versions differ between CI and Docker/Production; driver & schema compatibility must remain covered by tests."
        )
        return UpdateRiskAssessment(
            subject=f"Service Compatibility · {row.component}",
            category="runtime_alignment",
            current_version=current_ver,
            proposed_version=current_ver,
            target_version=target_ver,
            update_type="drift",
            risk_level=RISK_LOW,
            breaking_change=False,
            recommendation=(
                f"Écart de version {row.component} accepté ({row.drift_details}). "
                "Ne pas forcer de migration PostgreSQL en production uniquement pour égaliser les numéros de version."
            ),
            affected_files=affected_files,
            sources=sources,
            uncertainties=_unique_list(uncertainties),
        )

    if row.drift_status == DRIFT_ALIGNED:
        return UpdateRiskAssessment(
            subject=f"Runtime Alignment · {row.component}",
            category="runtime_alignment",
            current_version=current_ver,
            proposed_version=target_ver,
            target_version=target_ver,
            update_type="aligned",
            risk_level=RISK_LOW,
            breaking_change=False,
            recommendation=(
                f"{row.component} est aligné sur {target_ver} dans les fichiers déclarés. "
                "Confirmer l'alignement du runtime de production lors du déploiement."
            ),
            affected_files=affected_files,
            sources=sources,
            uncertainties=uncertainties,
        )

    uncertainties.append(f"{row.component} is only partially declared across environments.")
    return UpdateRiskAssessment(
        subject=f"Runtime Alignment · {row.component}",
        category="runtime_alignment",
        current_version=current_ver,
        proposed_version=target_ver,
        target_version=target_ver,
        update_type="unknown",
        risk_level=RISK_UNKNOWN,
        breaking_change=False,
        recommendation=row.drift_details or f"Compléter la déclaration de version pour {row.component} si pertinent.",
        affected_files=affected_files,
        sources=sources,
        uncertainties=_unique_list(uncertainties),
    )


def classify_repository_update_risks(report: AuditReport) -> list[UpdateRiskAssessment]:
    assessments: list[UpdateRiskAssessment] = []

    # 1. Open Renovate PRs
    for pr in report.open_prs:
        assessments.append(_classify_open_pr(pr, report))

    # 2. Pending / rate-limited updates on Dependency Dashboard
    for pending_raw in report.dashboard.pending_or_rate_limited_items:
        pkg, from_ver, to_ver, update_type = parse_dashboard_pending_update(pending_raw)
        dashboard_ref = (
            f"Dependency Dashboard issue #{report.dashboard.issue_number}"
            if report.dashboard.issue_number
            else "Dependency Dashboard"
        )
        assessments.append(
            UpdateRiskAssessment(
                subject=pkg,
                category="pending_update",
                current_version=from_ver,
                proposed_version=to_ver,
                target_version=to_ver,
                update_type=update_type,
                risk_level=RISK_REVIEW if update_type in {"major", "minor", "unknown"} else RISK_LOW,
                breaking_change=False,
                recommendation=(
                    f"Mise à jour en attente ou limitée (rate-limited) sur le {dashboard_ref}. "
                    "Traiter les PRs ouvertes en cours avant son ouverture par Renovate."
                ),
                affected_files=[],
                sources=[f"{dashboard_ref}: {pending_raw}"],
                uncertainties=[
                    "PR not yet generated by Renovate (release notes and CI results unavailable until PR opens)."
                ],
            )
        )

    # 3. Environment matrix rows
    for row in report.env_matrix:
        assessments.append(_classify_environment_row(row, report))

    # 4. Deployment & virtualenv hygiene risks
    gs = report.git_state
    if gs.venv_exists and gs.venv_has_pip is False:
        has_pip_deploy = any("calls 'pip install'" in r or "uses direct 'pip install'" in r for r in report.reservations)
        assessments.append(
            UpdateRiskAssessment(
                subject="Virtualenv & Deployment Pip Compatibility",
                category="deployment_hygiene",
                current_version=".venv (no pip binary)",
                proposed_version="uv pip install",
                target_version="uv pip install --python $VENV_DIR/bin/python",
                update_type="hygiene",
                risk_level=RISK_BREAKING if has_pip_deploy else RISK_REVIEW,
                breaking_change=has_pip_deploy,
                recommendation=(
                    "L'environnement '.venv' ne contient pas de binaire 'pip' (créé avec 'uv venv'). "
                    "Remplacer 'pip install' par 'uv pip install --python \"$VENV_DIR/bin/python\"' dans le script de déploiement."
                ),
                affected_files=list(report.deployment_files) or ["deploy.sh"],
                sources=[".venv"] + list(report.deployment_files),
                uncertainties=(
                    []
                    if report.deployment_files
                    else ["External deployment script under /srv/apps may exist on the VPS outside this repository."]
                ),
            )
        )

    if gs.untracked_venv_entries:
        assessments.append(
            UpdateRiskAssessment(
                subject="Untracked Virtualenv Entries (.venv*)",
                category="deployment_hygiene",
                current_version=", ".join(gs.untracked_venv_entries),
                proposed_version=".git/info/exclude",
                target_version="Excluded locally via .git/info/exclude",
                update_type="hygiene",
                risk_level=RISK_REVIEW,
                breaking_change=False,
                recommendation=(
                    f"Ajouter {', '.join(gs.untracked_venv_entries)} dans '.git/info/exclude' pour garder le working tree propre "
                    "lors d'une bascule symlink '.venv -> .venv-xxx' sans polluer '.gitignore'."
                ),
                affected_files=[".git/info/exclude"],
                sources=["git status --porcelain"],
                uncertainties=[],
            )
        )

    for st in report.stacks:
        if st.ecosystem == "python" and st.unpinned_dependencies:
            req_files = [f for f in st.files if "requirements" in f or f.endswith("pyproject.toml")]
            sample = ", ".join(st.unpinned_dependencies[:5])
            assessments.append(
                UpdateRiskAssessment(
                    subject="Unpinned Python Dependencies",
                    category="deployment_hygiene",
                    current_version=f"{len(st.unpinned_dependencies)} unpinned ({sample})",
                    proposed_version="Pinned requirements (==) or lockfile",
                    target_version="Pinned versions in requirements.txt",
                    update_type="hygiene",
                    risk_level=RISK_REVIEW,
                    breaking_change=False,
                    recommendation=(
                        "Épingler les versions dans requirements.txt (ou utiliser un fichier de lock) pour que Renovate "
                        "suive chaque mise à jour et que CI et production installent exactement les mêmes versions."
                    ),
                    affected_files=req_files,
                    sources=req_files,
                    uncertainties=[
                        "Exact installed versions in production cannot be guaranteed while requirements remain unpinned."
                    ],
                )
            )

    if not report.renovate_config.exists:
        assessments.append(
            UpdateRiskAssessment(
                subject="Renovate Configuration Missing",
                category="renovate_config",
                current_version="missing",
                proposed_version="github>nlabrazi/dev-platform//configs/renovate/base",
                target_version="github>nlabrazi/dev-platform//configs/renovate/base",
                update_type="config",
                risk_level=RISK_REVIEW,
                breaking_change=False,
                recommendation="Ajouter un fichier 'renovate.json' héritant du preset central Dev Platform.",
                affected_files=["renovate.json"],
                sources=[report.renovate_config.path],
                uncertainties=[],
            )
        )

    assessments.sort(key=lambda item: RISK_PRIORITY.get(item.risk_level, 0), reverse=True)
    return assessments


def summarize_risk_assessments(assessments: list[UpdateRiskAssessment]) -> dict[str, int]:
    counts = {
        RISK_BLOCKED: 0,
        RISK_BREAKING: 0,
        RISK_REVIEW: 0,
        RISK_LOW: 0,
        RISK_UNKNOWN: 0,
    }
    for item in assessments:
        counts[item.risk_level] = counts.get(item.risk_level, 0) + 1
    return counts
