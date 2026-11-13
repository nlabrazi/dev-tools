from datetime import datetime

from core.maintenance.analyzer import classify_repository_update_risks
from core.maintenance.models import (
    DEV_PLATFORM_PRESET,
    DRIFT_PROBLEMATIC,
    RISK_BLOCKED,
    RISK_BREAKING,
    RISK_REVIEW,
    AuditReport,
    MigrationFileTarget,
    MigrationPlan,
    RollbackPlan,
    UpdateRiskAssessment,
)


def _unique_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _categorize_file_path(path: str) -> str:
    lower = path.lower()
    if "jenkinsfile" in lower or ".github/workflows/" in lower:
        return "ci"
    if "dockerfile" in lower or "compose" in lower or lower.endswith((".python-version", ".nvmrc", ".node-version", ".ruby-version")):
        return "runtime"
    if "deploy" in lower or "start.sh" in lower or lower.startswith("/srv/apps/"):
        return "deployment"
    if ".git/info/exclude" in lower or ".gitignore" in lower:
        return "git"
    return "config"


def _build_rollback_plan(report: AuditReport, target_versions: dict[str, str]) -> RollbackPlan:
    ecosystems = {st.ecosystem for st in report.stacks}
    gs = report.git_state
    base_branch = gs.base_branch or "master"

    steps: list[str] = []
    preconditions: list[str] = [
        f"Branche d'intégration 'staging' et branche '{base_branch}' synchronisées avant déploiement.",
        "Aucune suppression d'environnement virtuel ou d'image de sauvegarde avant validation complète.",
    ]

    if "python" in ecosystems:
        strategy = "virtualenv-symlink-and-git-revert"
        backup_target = (
            f"{gs.venv_target}-backup (symlink .venv)"
            if gs.venv_is_symlink and gs.venv_target
            else ".venv-backup (conserver l'ancien environnement Python)"
        )
        steps.extend(
            [
                "Conserver l'environnement virtuel actuel sous '.venv-<old>-backup' avant de basculer le lien symbolique '.venv -> .venv-<new>'.",
                "En cas de régression en production : repasser le lien symbolique 'ln -sfn .venv-<old>-backup .venv' et relancer le service/cron.",
                f"En cas de régression sur le code ou la CI : 'git revert <merge-commit>' sur 'staging' puis sur '{base_branch}'.",
            ]
        )
        preconditions.append(
            "Vérifier que les crons (Sentinel) et scripts de déploiement pointent vers '.venv/bin/python' et utilisent 'uv pip' si '.venv' est sans 'pip'."
        )
    elif "docker" in ecosystems:
        strategy = "docker-image-and-git-revert"
        backup_target = f"Image Docker précédente + branche {base_branch}"
        steps.extend(
            [
                "Conserver le tag de l'image Docker actuelle avant reconstruction sur le VPS.",
                f"En cas d'échec après déploiement : redéployer l'image précédente et exécuter 'git revert' sur '{base_branch}'.",
            ]
        )
    else:
        strategy = "lockfile-and-git-revert"
        backup_target = f"Commit précédent sur staging / {base_branch}"
        steps.extend(
            [
                f"Revenir au commit précédent via 'git revert' sur 'staging' (ou '{base_branch}') et réinstaller les dépendances depuis le lockfile.",
            ]
        )

    return RollbackPlan(
        strategy=strategy,
        backup_target=backup_target,
        steps=steps,
        preconditions=preconditions,
    )


def generate_migration_plan(
    report: AuditReport,
    assessments: list[UpdateRiskAssessment] | None = None,
    *,
    now: datetime | None = None,
) -> MigrationPlan:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    risk_items = assessments if assessments is not None else (
        report.risk_assessments or classify_repository_update_risks(report)
    )

    target_versions: dict[str, str] = {}
    for item in risk_items:
        if item.category == "runtime_alignment":
            comp_name = item.subject.split("·", 1)[-1].strip()
            target_versions[comp_name] = item.target_version
        elif item.category == "dependency_pr" and item.target_version:
            target_versions[item.subject] = item.target_version

    files_map: dict[str, MigrationFileTarget] = {}

    def _add_file_target(path: str, reason: str, suggested_change: str) -> None:
        cleaned = path.strip()
        if not cleaned:
            return
        if cleaned in files_map:
            existing = files_map[cleaned]
            if reason not in existing.reason:
                files_map[cleaned] = MigrationFileTarget(
                    path=cleaned,
                    category=existing.category,
                    reason=f"{existing.reason} ; {reason}",
                    suggested_change=f"{existing.suggested_change} ; {suggested_change}",
                )
            return
        files_map[cleaned] = MigrationFileTarget(
            path=cleaned,
            category=_categorize_file_path(cleaned),
            reason=reason,
            suggested_change=suggested_change,
        )

    for item in risk_items:
        if item.risk_level in {RISK_BLOCKED, RISK_BREAKING, RISK_REVIEW}:
            for file_path in item.affected_files:
                _add_file_target(
                    file_path,
                    reason=f"[{item.risk_level}] {item.subject} ({item.current_version} -> {item.proposed_version})",
                    suggested_change=item.recommendation,
                )

    for row in report.env_matrix:
        if row.drift_status == DRIFT_PROBLEMATIC:
            target_v = target_versions.get(row.component, row.github_actions)
            for src in row.sources:
                if ":" in src:
                    _, rest = src.split(":", 1)
                    file_part = rest.strip().split(" (", 1)[0].split("->", 1)[0].split(":L", 1)[0].split("#", 1)[0].strip()
                    if file_part:
                        _add_file_target(
                            file_part,
                            reason=f"Désalignement {row.component} ({row.drift_details})",
                            suggested_change=f"Aligner {row.component} sur la version cible {target_v}.",
                        )

    if not report.renovate_config.exists:
        _add_file_target(
            "renovate.json",
            reason="Configuration Renovate absente du dépôt.",
            suggested_change=f"Créer 'renovate.json' héritant de '{DEV_PLATFORM_PRESET}'.",
        )
    elif not report.renovate_config.extends_dev_platform:
        _add_file_target(
            report.renovate_config.path,
            reason="Configuration Renovate locale n'héritant pas du preset central Dev Platform.",
            suggested_change=(
                f"Ajouter '{DEV_PLATFORM_PRESET}' dans 'extends' tout en conservant les exceptions locales (ex: registres privés)."
            ),
        )

    ordered_steps: list[str] = []
    gs = report.git_state
    base_branch = gs.base_branch or "master"

    if not gs.is_clean:
        ordered_steps.append(
            f"1. Nettoyer ou isoler les modifications locales sur '{gs.branch}' ({gs.staged_count} staged, {gs.unstaged_count} unstaged, {gs.untracked_count} untracked)."
        )
    else:
        ordered_steps.append(
            f"1. Vérifier que le dépôt est sur la branche d'intégration 'staging' (actuellement '{gs.branch}', propre)."
        )

    if gs.untracked_venv_entries:
        ordered_steps.append(
            f"2. Ajouter {', '.join(gs.untracked_venv_entries)} dans '.git/info/exclude' pour éviter les fichiers non suivis liés aux environnements virtuels."
        )

    if not report.renovate_config.exists or any(pr.is_onboarding for pr in report.open_prs):
        ordered_steps.append(
            f"3. Aligner '{report.renovate_config.path}' avec le preset Dev Platform sur 'staging', puis fusionner 'staging -> {base_branch}' pour activer Renovate sur 'staging'."
        )

    drift_rows = [r for r in report.env_matrix if r.drift_status == DRIFT_PROBLEMATIC]
    if drift_rows:
        comps = ", ".join(f"{r.component} -> {target_versions.get(r.component, '?')}" for r in drift_rows)
        ordered_steps.append(
            f"4. Aligner les versions déclarées ({comps}) entre Local, GitHub Actions, Jenkins et Docker."
        )

    if gs.venv_exists and gs.venv_has_pip is False:
        ordered_steps.append(
            "5. Adapter le script de déploiement pour utiliser 'uv pip install --python \"$VENV_DIR/bin/python\"' (environnement .venv sans binaire pip)."
        )

    non_onboarding_prs = [pr for pr in report.open_prs if not pr.is_onboarding]
    if non_onboarding_prs:
        pr_list = ", ".join(f"#{pr.number} ({pr.title})" for pr in non_onboarding_prs)
        ordered_steps.append(
            f"6. Examiner et fusionner séquentiellement les PRs Renovate vers 'staging' après validation CI : {pr_list}."
        )

    ordered_steps.append(
        f"7. Une fois 'staging' validée par les tests et les pipelines CI, fusionner 'staging -> {base_branch}' puis préparer le déploiement en production avec sauvegarde de rollback."
    )

    validation_steps: list[str] = [
        "Exécuter la suite de tests unitaires et d'intégration localement (aucun test FAIL ; vérifier explicitement tout test SKIPPED).",
        "Contrôler le statut GitHub Actions sur 'staging' (attendu : PASS).",
    ]
    if report.ci_summary.jenkins_file:
        validation_steps.append(
            "Contrôler l'exécution du pipeline Jenkins (Jenkinsfile) avec l'image runtime alignée (attendu : PASS)."
        )
    if any(r.component == "PostgreSQL" for r in report.env_matrix):
        validation_steps.append(
            "Valider la connexion PostgreSQL et la compatibilité du driver sans forcer de mise à jour majeure du serveur DB de production."
        )
    if report.deployment_files or (gs.venv_exists and gs.venv_has_pip is False):
        validation_steps.append(
            "Après déploiement sur le VPS : vérifier le runtime actif (.venv), la santé du service et l'exécution des jobs planifiés (Sentinel)."
        )

    rollback_plan = _build_rollback_plan(report, target_versions)

    sources = _unique_list([src for item in risk_items for src in item.sources])
    uncertainties = _unique_list([unc for item in risk_items for unc in item.uncertainties])

    actionable_count = sum(
        1 for item in risk_items if item.risk_level in {RISK_BLOCKED, RISK_BREAKING, RISK_REVIEW}
    )
    if actionable_count == 0:
        summary = (
            f"Aucun risque bloquant ou décalage critique détecté sur {report.repo_name}. "
            "Le dépôt est aligné pour ses environnements connus ; seules les vérifications finales de runtime restent à confirmer."
        )
    else:
        summary = (
            f"Plan de migration pour {report.repo_name} : {actionable_count} point(s) d'attention identifié(s), "
            f"{len(files_map)} fichier(s) à examiner et procédure de rollback '{rollback_plan.strategy}' préparée."
        )

    return MigrationPlan(
        repo_name=report.repo_name,
        generated_at=timestamp,
        summary=summary,
        target_versions=target_versions,
        files_to_review=list(files_map.values()),
        ordered_steps=ordered_steps,
        validation_steps=validation_steps,
        rollback_plan=rollback_plan,
        sources=sources,
        uncertainties=uncertainties,
    )
