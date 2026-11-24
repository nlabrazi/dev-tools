import json
import re
from pathlib import Path

from rich.console import Console

from core.commit import auto_commit_all_repos
from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE
from core.maintenance.analyzer import parse_dashboard_pending_update
from core.maintenance.detector import inspect_git_state
from core.maintenance.migrator import (
    apply_migration_changes,
    prepare_migration_changes,
)
from core.maintenance.models import (
    RISK_BLOCKED,
    STATUS_FAIL,
    STATUS_UNKNOWN,
    AuditReport,
    RenovatePRInfo,
    RenovateUpdateItem,
)
from core.maintenance.renovate import infer_semver_update_type
from core.maintenance.reporter import (
    render_migration_application_result,
    render_proposed_changes_preview,
)
from core.merge import main as merge_all_repos
from core.sync import checkout_branch, ensure_local_branch_exists, pull_ff_only
from utils.common import is_dry_run, run_command
from utils.console import ask_yes_no, ui_panel
from utils.git import git_command, git_output

console = Console()


def sync_local_branch_ff_only(
    repo_path: str,
    repo_name: str,
    branch: str,
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    if is_dry_run():
        out.print()
        out.print(
            ui_panel(
                f"Dry-run: would fast-forward local branch '{branch}' from {DEFAULT_REMOTE}/{branch}.",
                title="Fast-Forward Sync (Dry-Run)",
                tone="info",
                compact=True,
            )
        )
        return "dry-run"

    current_branch = git_output(repo_path, ["branch", "--show-current"])
    switched_from: str | None = None

    if current_branch and current_branch != branch:
        if (
            ensure_local_branch_exists(repo_path, repo_name, branch) == "ready"
            and checkout_branch(repo_path, repo_name, branch) == "ready"
        ):
            switched_from = current_branch
        else:
            fetch_res = git_command(
                repo_path,
                ["fetch", DEFAULT_REMOTE, f"{branch}:{branch}"],
                silent=True,
            )
            out.print()
            if fetch_res.returncode == 0:
                out.print(
                    ui_panel(
                        (
                            f"Branche locale '{branch}' mise à jour en fast-forward depuis {DEFAULT_REMOTE}/{branch} "
                            f"(branche active conservée : '{current_branch}')."
                        ),
                        title="Fast-Forward Sync",
                        tone="success",
                        compact=True,
                    )
                )
                return "pulled"
            err = (fetch_res.stderr or fetch_res.stdout or "").strip() or f"exit code {fetch_res.returncode}"
            out.print(
                ui_panel(
                    f"Impossible de mettre à jour '{branch}' en fast-forward depuis '{current_branch}' :\n{err}",
                    title="Fast-Forward Sync Failed",
                    tone="warning",
                    compact=True,
                )
            )
            return "failed"

    pull_status = pull_ff_only(repo_path, repo_name, branch)
    out.print()
    if pull_status == "pulled":
        switch_note = f" (basculé depuis '{switched_from}')" if switched_from else ""
        out.print(
            ui_panel(
                f"Branche locale '{branch}' synchronisée en fast-forward avec {DEFAULT_REMOTE}/{branch}{switch_note}.",
                title="Fast-Forward Sync",
                tone="success",
                compact=True,
            )
        )
        return "pulled"

    if pull_status == "dry-run":
        return "dry-run"

    out.print(
        ui_panel(
            f"Le fast-forward de '{branch}' depuis {DEFAULT_REMOTE}/{branch} a échoué (vérifiez l'état local de la branche).",
            title="Fast-Forward Sync Failed",
            tone="warning",
            compact=True,
        )
    )
    return "failed"


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

    out.print()
    if not ask_yes_no(
        f"Merge Renovate PR #{pr.number} ({pr.title}) into {pr.base_branch}?",
        default="n",
    ):
        out.print()
        return "skipped"

    if is_dry_run():
        out.print()
        out.print(
            ui_panel(
                f"Dry-run: would merge PR #{pr.number} ({pr.head_branch} → {pr.base_branch}) in {repo_name}.",
                title="Renovate PR Merge (Dry-Run)",
                tone="info",
                compact=True,
            )
        )
        out.print()
        return "dry-run"

    res = run_command(
        ["gh", "pr", "merge", str(pr.number), "--merge"],
        cwd=repo_path,
        silent=True,
    )
    if res.returncode != 0:
        details = ((res.stderr or "") + "\n" + (res.stdout or "")).strip() or f"exit code {res.returncode}"
        out.print()
        out.print(
            ui_panel(
                f"Failed to merge PR #{pr.number}:\n{details}",
                title="Renovate PR Merge Failed",
                tone="danger",
                compact=True,
            )
        )
        out.print()
        return "failed"

    out.print()
    out.print(
        ui_panel(
            f"Merged PR #{pr.number} ({pr.title}) into {pr.base_branch}.",
            title="Renovate PR Merged",
            tone="success",
            compact=True,
        )
    )

    if sync_after_merge and pr.base_branch == DEFAULT_HEAD_BRANCH:
        out.print()
        if ask_yes_no(f"Pull fast-forward {pr.base_branch} locally after merging PR #{pr.number}?", default="y"):
            sync_local_branch_ff_only(repo_path, repo_name, pr.base_branch, target_console=out)
        out.print()

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


def _iter_node_package_json_files(repo_root: Path) -> tuple[list[Path], bool]:
    files: list[Path] = []
    has_root_workspaces = False

    root_pkg = repo_root / "package.json"
    if root_pkg.is_file():
        files.append(root_pkg)
        try:
            root_data = json.loads(root_pkg.read_text(encoding="utf-8"))
        except Exception:
            root_data = {}
        raw_ws = root_data.get("workspaces") if isinstance(root_data, dict) else None
        ws_patterns: list[str] = []
        if isinstance(raw_ws, list):
            ws_patterns = [str(p) for p in raw_ws if isinstance(p, str)]
        elif isinstance(raw_ws, dict) and isinstance(raw_ws.get("packages"), list):
            ws_patterns = [str(p) for p in raw_ws["packages"] if isinstance(p, str)]
        if ws_patterns:
            has_root_workspaces = True
            for pattern in ws_patterns:
                for ws_dir in sorted(repo_root.glob(pattern)):
                    ws_pkg = ws_dir / "package.json"
                    if ws_pkg.is_file() and ws_pkg not in files:
                        files.append(ws_pkg)

    for subdir in ("ui", "frontend", "web", "api", "backend", "desktop-shell"):
        sub_pkg = repo_root / subdir / "package.json"
        if sub_pkg.is_file() and sub_pkg not in files:
            files.append(sub_pkg)

    return files, has_root_workspaces


def _semver_tuple(raw: str) -> tuple[int, int, int] | None:
    cleaned = re.sub(r"^[~^>=<]+", "", (raw or "").strip()).lstrip("v")
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", cleaned)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _collect_declared_npm_packages(repo_root: Path) -> set[str]:
    declared: set[str] = set()
    pkg_files, _ = _iter_node_package_json_files(repo_root)
    for pkg_file in pkg_files:
        try:
            data = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            deps = data.get(section)
            if isinstance(deps, dict):
                declared.update(str(k) for k in deps.keys())
    return declared


def _collect_workspace_lockfile_drift(repo_root: Path) -> list[RenovateUpdateItem]:
    """Detect workspace package.json ranges lagging behind versions already resolved in package-lock.json."""
    lock_path = repo_root / "package-lock.json"
    if not lock_path.is_file():
        return []
    pkg_files, has_workspaces = _iter_node_package_json_files(repo_root)
    if not has_workspaces:
        return []

    try:
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    lock_packages = lock_data.get("packages") if isinstance(lock_data, dict) else None
    if not isinstance(lock_packages, dict):
        return []

    locked_versions: dict[str, str] = {}
    for key, info in lock_packages.items():
        if not isinstance(info, dict) or not isinstance(key, str):
            continue
        ver = info.get("version")
        if not isinstance(ver, str) or "-" in ver:
            continue
        if key.startswith("node_modules/"):
            dep_name = key[len("node_modules/") :]
            if "node_modules/" not in dep_name:
                locked_versions[dep_name] = ver

    drifted: dict[str, RenovateUpdateItem] = {}
    for pkg_file in pkg_files:
        if pkg_file == repo_root / "package.json":
            continue
        try:
            pkg_data = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(pkg_data, dict):
            continue
        for section in ("dependencies", "devDependencies"):
            deps = pkg_data.get(section)
            if not isinstance(deps, dict):
                continue
            for dep_name, raw_spec in deps.items():
                if not isinstance(raw_spec, str) or dep_name in drifted:
                    continue
                locked_ver = locked_versions.get(dep_name)
                if not locked_ver:
                    continue
                declared_parts = _semver_tuple(raw_spec)
                locked_parts = _semver_tuple(locked_ver)
                if not declared_parts or not locked_parts or locked_parts <= declared_parts:
                    continue
                declared_str = f"{declared_parts[0]}.{declared_parts[1]}.{declared_parts[2]}"
                upd_type = infer_semver_update_type(declared_str, locked_ver)
                drifted[dep_name] = RenovateUpdateItem(
                    package=dep_name,
                    update_type=upd_type,
                    from_version=declared_str,
                    to_version=locked_ver,
                    raw_change=f"{declared_str} -> {locked_ver}",
                )
    return list(drifted.values())


def collect_direct_staging_updates(
    repo_path: str,
    report: AuditReport,
    *,
    include_npm_outdated: bool = True,
) -> list[RenovateUpdateItem]:
    updates_by_pkg: dict[str, RenovateUpdateItem] = {}

    if include_npm_outdated:
        repo_root = Path(repo_path)
        for directory in (repo_root, repo_root / "ui", repo_root / "frontend", repo_root / "web", repo_root / "api"):
            pkg_json = directory / "package.json"
            if not pkg_json.is_file():
                continue
            npm_cmd = ["npm", "outdated", "--json"]
            try:
                pkg_meta = json.loads(pkg_json.read_text(encoding="utf-8"))
                if isinstance(pkg_meta, dict) and pkg_meta.get("workspaces"):
                    npm_cmd = ["npm", "outdated", "--workspaces", "--json"]
            except Exception:
                pass
            res = run_command(npm_cmd, cwd=str(directory), silent=True)
            raw_out = (res.stdout or "").strip()
            if not raw_out:
                continue
            try:
                parsed = json.loads(raw_out)
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            for pkg, raw_info in parsed.items():
                if pkg in updates_by_pkg:
                    continue
                if isinstance(raw_info, list):
                    info = next((entry for entry in raw_info if isinstance(entry, dict)), None)
                elif isinstance(raw_info, dict):
                    info = raw_info
                else:
                    info = None
                if not isinstance(info, dict):
                    continue

                current_ver = str(info.get("current") or "").strip()
                wanted_ver = str(info.get("wanted") or "").strip()
                latest_ver = str(info.get("latest") or "").strip()
                from_ver = current_ver or wanted_ver

                # Prefer safe semver-compatible 'wanted' version when available;
                # fall back to 'latest' only if not a pre-release (-rc, -alpha, -beta).
                if wanted_ver and from_ver and wanted_ver != from_ver:
                    to_ver = wanted_ver
                    upd_type = infer_semver_update_type(from_ver, to_ver)
                elif latest_ver and "-" not in latest_ver and latest_ver != from_ver:
                    to_ver = latest_ver
                    upd_type = infer_semver_update_type(from_ver, to_ver) if from_ver else "major"
                    # In semver, 0.x minor bumps where wanted == current are breaking
                    if from_ver.startswith("0.") and upd_type == "minor":
                        upd_type = "major"
                else:
                    continue

                updates_by_pkg[pkg] = RenovateUpdateItem(
                    package=pkg,
                    update_type=upd_type,
                    from_version=from_ver,
                    to_version=to_ver,
                    raw_change=f"{from_ver} -> {to_ver}" if from_ver else f"-> {to_ver}",
                )

    repo_root = Path(repo_path)

    # Catch npm workspace ranges (e.g. dev-platform packages/*/package.json) lagging behind package-lock.json
    for drift_item in _collect_workspace_lockfile_drift(repo_root):
        if drift_item.package not in updates_by_pkg:
            updates_by_pkg[drift_item.package] = drift_item

    # Include explicit 'pkg ^old -> [Updates: ^new]' entries from Renovate's Detected Dependencies section
    declared_npm_pkgs = _collect_declared_npm_packages(repo_root)
    for det_item in getattr(report.dashboard, "detected_updates", []) or []:
        if det_item.package in updates_by_pkg:
            continue
        if declared_npm_pkgs and det_item.package not in declared_npm_pkgs:
            continue
        updates_by_pkg[det_item.package] = det_item

    for raw_item in report.dashboard.pending_or_rate_limited_items:
        pkg, from_ver, to_ver, upd_type = parse_dashboard_pending_update(raw_item)
        if not pkg or to_ver == STATUS_UNKNOWN or from_ver == STATUS_UNKNOWN or pkg in updates_by_pkg:
            continue
        updates_by_pkg[pkg] = RenovateUpdateItem(
            package=pkg,
            update_type=upd_type,
            from_version=from_ver,
            to_version=to_ver,
            raw_change=f"{from_ver} -> {to_ver}",
        )

    items = list(updates_by_pkg.values())
    items.sort(
        key=lambda u: (
            0 if u.package.startswith("@nlabrazi/") else (1 if u.update_type in {"patch", "minor", "pin", "digest"} else 2),
            u.package,
        )
    )
    return items


def apply_direct_staging_updates(
    repo_path: str,
    repo_name: str,
    updates: list[RenovateUpdateItem],
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    if not updates:
        return "no-op"

    git_state = inspect_git_state(repo_path)
    if git_state.branch != DEFAULT_HEAD_BRANCH:
        if git_state.staged_count > 0 or git_state.unstaged_count > 0:
            out.print(
                ui_panel(
                    (
                        f"Vous êtes sur '{git_state.branch}' avec des modifications non commitées. "
                        f"Commitez ou stashez vos changements avant de basculer sur '{DEFAULT_HEAD_BRANCH}'."
                    ),
                    title="Bump Direct Bloqué · Modifications en cours",
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

    summary_lines = [f"• {u.package}: {u.from_version or '?'} → {u.to_version} ({u.update_type})" for u in updates]
    if is_dry_run():
        out.print()
        out.print(
            ui_panel(
                f"Dry-run : mettrait à jour directement sur '{DEFAULT_HEAD_BRANCH}' dans {repo_name} :\n"
                + "\n".join(summary_lines),
                title="Bump Direct sur Staging (Dry-Run)",
                tone="info",
                compact=True,
            )
        )
        return "dry-run"

    repo_root = Path(repo_path)
    applied_pkgs: list[str] = []

    # 1. Apply Node/npm updates across root, subdirectories, and npm workspaces
    pkg_json_files, has_root_workspaces = _iter_node_package_json_files(repo_root)
    install_dirs: list[Path] = []
    original_pkg_contents: dict[Path, str] = {}

    for pkg_json in pkg_json_files:
        try:
            raw_original = pkg_json.read_text(encoding="utf-8")
            pkg_data = json.loads(raw_original)
        except Exception:
            continue
        if not isinstance(pkg_data, dict):
            continue

        file_changed = False
        file_matched = False
        for section in ("dependencies", "devDependencies"):
            deps = pkg_data.get(section)
            if not isinstance(deps, dict):
                continue
            for upd in updates:
                if upd.package in deps:
                    file_matched = True
                    current_spec = str(deps[upd.package])
                    prefix_match = re.match(r"^([~^])", current_spec)
                    prefix = prefix_match.group(1) if prefix_match else ""
                    clean_target = upd.to_version.lstrip("v^~")
                    new_spec = f"{prefix}{clean_target}"
                    if deps[upd.package] != new_spec:
                        deps[upd.package] = new_spec
                        file_changed = True
                    if upd.package not in applied_pkgs:
                        applied_pkgs.append(upd.package)

        if file_changed:
            pkg_name_val = str(pkg_data.get("name") or "")
            pkg_ver_val = str(pkg_data.get("version") or "")
            if pkg_name_val.startswith("@nlabrazi/") and not pkg_data.get("private"):
                ver_m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", pkg_ver_val)
                if ver_m:
                    next_patch = int(ver_m.group(3)) + 1
                    bumped_pkg_ver = f"{ver_m.group(1)}.{ver_m.group(2)}.{next_patch}{ver_m.group(4)}"
                    pkg_data["version"] = bumped_pkg_ver
                    applied_pkgs.append(f"{pkg_name_val} ({pkg_ver_val} → {bumped_pkg_ver})")
            original_pkg_contents[pkg_json] = raw_original
            pkg_json.write_text(json.dumps(pkg_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if file_changed or file_matched:
            target_install_dir = repo_root if (has_root_workspaces and pkg_json.parent != repo_root and (repo_root / "package.json").is_file() and pkg_json.parent.parent in {repo_root, repo_root / "apps", repo_root / "packages"}) else pkg_json.parent
            if target_install_dir not in install_dirs:
                install_dirs.append(target_install_dir)

    # Keep Jenkinsfile Playwright Docker image aligned when @playwright/test is bumped
    jenkinsfile = repo_root / "Jenkinsfile"
    if jenkinsfile.is_file():
        for upd in updates:
            if upd.package == "@playwright/test" and upd.package in applied_pkgs:
                try:
                    jf_text = jenkinsfile.read_text(encoding="utf-8")
                    clean_pw = upd.to_version.lstrip("v^~")
                    updated_jf = re.sub(
                        r"(mcr\.microsoft\.com/playwright:v?)([0-9]+(?:\.[0-9]+){0,2})(-[A-Za-z0-9.\-_]+)?",
                        lambda m: f"{m.group(1)}{clean_pw}{m.group(3) or ''}",
                        jf_text,
                    )
                    if updated_jf != jf_text:
                        jenkinsfile.write_text(updated_jf, encoding="utf-8")
                except OSError:
                    pass

    for install_dir in install_dirs:
        res = run_command(["npm", "install"], cwd=str(install_dir), silent=True)
        if res.returncode != 0:
            for modified_path, old_text in original_pkg_contents.items():
                try:
                    modified_path.write_text(old_text, encoding="utf-8")
                except OSError:
                    pass
            details = ((res.stderr or "") + "\n" + (res.stdout or "")).strip() or f"exit code {res.returncode}"
            out.print()
            out.print(
                ui_panel(
                    f"Échec de 'npm install' dans {install_dir} (les fichiers package.json ont été restaurés) :\n{details}",
                    title="Bump Direct Échoué · Conflit de Dépendances",
                    tone="danger",
                    compact=True,
                )
            )
            return "failed"

    # 2. Apply Python requirements*.txt updates
    for directory in (repo_root, repo_root / "backend", repo_root / "api"):
        if not directory.is_dir():
            continue
        for req_file in sorted(directory.glob("requirements*.txt")):
            if not req_file.is_file():
                continue
            try:
                content = req_file.read_text(encoding="utf-8")
            except OSError:
                continue
            new_content = content
            for upd in updates:
                clean_target = upd.to_version.lstrip("v^~=")
                pattern = re.compile(
                    rf"^(\s*{re.escape(upd.package)}\s*(?:==|>=|~=)\s*)([^\s;#]+)",
                    re.IGNORECASE | re.MULTILINE,
                )
                updated_text, count = pattern.subn(rf"\g<1>{clean_target}", new_content)
                if count > 0:
                    new_content = updated_text
                    if upd.package not in applied_pkgs:
                        applied_pkgs.append(upd.package)
            if new_content != content:
                req_file.write_text(new_content, encoding="utf-8")

    if not applied_pkgs:
        out.print()
        out.print(
            ui_panel(
                "Aucun fichier manifeste local (package.json / requirements.txt) ne correspondait aux paquets sélectionnés.",
                title="Bump Direct sur Staging",
                tone="warning",
                compact=True,
            )
        )
        return "no-op"

    out.print()
    out.print(
        ui_panel(
            f"Mis à jour directement sur '{DEFAULT_HEAD_BRANCH}' ({len(applied_pkgs)} paquet(s)) :\n"
            + "\n".join(f"• {pkg}" for pkg in applied_pkgs),
            title="Bump Direct Appliqué sur Staging",
            tone="success",
            compact=True,
        )
    )
    return "applied"


def collect_ruby_gem_updates(repo_path: str) -> list[RenovateUpdateItem]:
    repo_root = Path(repo_path)
    gemfile = repo_root / "Gemfile"
    if not gemfile.is_file():
        return []

    updates_by_pkg: dict[str, RenovateUpdateItem] = {}
    res = run_command(["bundle", "outdated", "--parseable"], cwd=str(repo_root), silent=True)
    for raw_line in (res.stdout or "").splitlines():
        m = re.match(
            r"^([A-Za-z0-9_.\-]+)\s+\(newest\s+([^,\s)]+),\s+installed\s+([^,\s)]+)",
            raw_line.strip(),
        )
        if not m:
            continue
        pkg, to_ver, from_ver = m.group(1), m.group(2), m.group(3)
        if pkg in updates_by_pkg or "-" in to_ver or from_ver == to_ver:
            continue
        upd_type = infer_semver_update_type(from_ver, to_ver)
        updates_by_pkg[pkg] = RenovateUpdateItem(
            package=pkg,
            update_type=upd_type,
            from_version=from_ver,
            to_version=to_ver,
            raw_change=f"{from_ver} -> {to_ver}",
        )

    items = list(updates_by_pkg.values())
    items.sort(
        key=lambda u: (
            0 if u.update_type in {"patch", "minor", "pin", "digest"} else 1,
            u.package,
        )
    )
    return items


def apply_ruby_gem_updates(
    repo_path: str,
    repo_name: str,
    updates: list[RenovateUpdateItem],
    *,
    target_console: Console | None = None,
) -> str:
    out = target_console or console
    if not updates:
        return "no-op"

    repo_root = Path(repo_path)
    gemfile = repo_root / "Gemfile"
    gemfile_lock = repo_root / "Gemfile.lock"
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

    git_state = inspect_git_state(repo_path)
    if git_state.branch != DEFAULT_HEAD_BRANCH:
        if git_state.staged_count > 0 or git_state.unstaged_count > 0:
            out.print(
                ui_panel(
                    (
                        f"Vous êtes sur '{git_state.branch}' avec des modifications non commitées. "
                        f"Commitez ou stashez vos changements avant de basculer sur '{DEFAULT_HEAD_BRANCH}'."
                    ),
                    title="Bump Gems Bloqué · Modifications en cours",
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

    summary_lines = [f"• {u.package}: {u.from_version or '?'} → {u.to_version} ({u.update_type})" for u in updates]
    if is_dry_run():
        out.print()
        out.print(
            ui_panel(
                f"Dry-run : mettrait à jour les gems Ruby sur '{DEFAULT_HEAD_BRANCH}' dans {repo_name} :\n"
                + "\n".join(summary_lines),
                title="Bump Gems Ruby sur Staging (Dry-Run)",
                tone="info",
                compact=True,
            )
        )
        return "dry-run"

    try:
        gemfile_text = gemfile.read_text(encoding="utf-8")
        lock_text = gemfile_lock.read_text(encoding="utf-8") if gemfile_lock.is_file() else ""
    except OSError:
        gemfile_text = ""
        lock_text = ""

    bundler_gems: list[str] = []
    new_gemfile_text = gemfile_text
    for upd in updates:
        gem_decl_re = re.compile(
            rf"""^(\s*gem\s+['"]{re.escape(upd.package)}['"]\s*,\s*['"])([^'"]+)(['"])""",
            re.MULTILINE,
        )
        if gem_decl_re.search(new_gemfile_text) or f" {upd.package} (" in lock_text:
            bundler_gems.append(upd.package)
            if upd.update_type == "major":
                major_minor = ".".join(upd.to_version.lstrip("v^~").split(".")[:2])
                new_gemfile_text = gem_decl_re.sub(rf"\g<1>~> {major_minor}\g<3>", new_gemfile_text)

    if not bundler_gems:
        out.print()
        out.print(
            ui_panel(
                "Aucune gem sélectionnée ne correspondait au Gemfile / Gemfile.lock du projet.",
                title="Bump Gems Ruby sur Staging",
                tone="warning",
                compact=True,
            )
        )
        return "no-op"

    if new_gemfile_text != gemfile_text:
        gemfile.write_text(new_gemfile_text, encoding="utf-8")

    res = run_command(["bundle", "update", "--conservative", *bundler_gems], cwd=str(repo_root), silent=True)
    if res.returncode != 0:
        if new_gemfile_text != gemfile_text:
            gemfile.write_text(gemfile_text, encoding="utf-8")
        details = ((res.stderr or "") + "\n" + (res.stdout or "")).strip() or f"exit code {res.returncode}"
        out.print()
        out.print(
            ui_panel(
                f"Échec de 'bundle update' dans {repo_root} (Gemfile restauré) :\n{details}",
                title="Bump Bundler Échoué",
                tone="danger",
                compact=True,
            )
        )
        return "failed"

    out.print()
    out.print(
        ui_panel(
            f"Gems Ruby mises à jour directement sur '{DEFAULT_HEAD_BRANCH}' ({len(bundler_gems)} gem(s)) :\n"
            + "\n".join(f"• {gem}" for gem in bundler_gems),
            title="Bump Gems Ruby Appliqué sur Staging",
            tone="success",
            compact=True,
        )
    )
    return "applied"


