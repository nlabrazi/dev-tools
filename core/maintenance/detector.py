import json
import os
import re
from pathlib import Path

from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE, resolve_repo_base_branch
from core.maintenance.models import (
    DEV_PLATFORM_PRESET,
    GitStateInfo,
    RenovateConfigInfo,
    StackComponent,
)
from utils.git import git_output

SERVICE_SUBDIRS = ("api", "ui", "backend", "frontend", "web", "desktop-shell")
PYTHON_REQ_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?\s*(==|>=|<=|~=|!=|>|<)?\s*(.*)$")

RENOVATE_CONFIG_CANDIDATES = (
    "renovate.json",
    ".github/renovate.json",
    "renovate.json5",
    ".renovaterc",
    ".renovaterc.json",
)

KNOWN_NODE_FRAMEWORKS = (
    ("nuxt", "Nuxt"),
    ("@angular/core", "Angular"),
    ("next", "Next.js"),
    ("react", "React"),
    ("vue", "Vue"),
    ("vite", "Vite"),
    ("@playwright/test", "Playwright"),
    ("playwright", "Playwright"),
    ("@nlabrazi/config-playwright", "Playwright"),
    ("@biomejs/biome", "Biome"),
    ("@nlabrazi/config-biome", "Biome"),
    ("prisma", "Prisma"),
    ("@prisma/client", "Prisma"),
    ("vitest", "Vitest"),
    ("@nlabrazi/config-vitest", "Vitest"),
    ("typescript", "TypeScript"),
    ("@nlabrazi/tsconfig", "TypeScript"),
)


def _rel_path(repo_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _candidate_dirs(repo_root: Path) -> list[Path]:
    dirs = [repo_root]
    for name in SERVICE_SUBDIRS:
        sub = repo_root / name
        if sub.is_dir():
            dirs.append(sub)
    return dirs


def inspect_git_state(repo_path: str) -> GitStateInfo:
    repo_root = Path(repo_path)
    branch = git_output(repo_path, ["branch", "--show-current"]) or "(detached)"
    base_branch, base_resolution = resolve_repo_base_branch(repo_path, DEFAULT_REMOTE)

    porcelain = git_output(repo_path, ["status", "--porcelain"])
    lines = [line.rstrip("\n") for line in porcelain.splitlines() if line.strip()]

    staged_count = 0
    unstaged_count = 0
    untracked_count = 0
    untracked_venv_entries: list[str] = []

    for line in lines:
        if line.startswith("??"):
            untracked_count += 1
            entry = line[3:].strip()
            if ".venv" in entry:
                untracked_venv_entries.append(entry)
            continue
        if len(line) >= 2:
            if line[0] != " ":
                staged_count += 1
            if line[1] != " ":
                unstaged_count += 1

    ahead_of_base: int | None = None
    behind_base: int | None = None
    if base_branch:
        counts_raw = git_output(
            repo_path,
            [
                "rev-list",
                "--left-right",
                "--count",
                f"{DEFAULT_REMOTE}/{base_branch}...{DEFAULT_REMOTE}/{DEFAULT_HEAD_BRANCH}",
            ],
        )
        if counts_raw:
            parts = counts_raw.replace("\t", " ").split()
            if len(parts) == 2:
                try:
                    behind_base = int(parts[0])
                    ahead_of_base = int(parts[1])
                except ValueError:
                    pass

    venv_path = repo_root / ".venv"
    venv_exists = os.path.lexists(venv_path)
    venv_is_symlink = os.path.islink(venv_path)
    venv_target: str | None = None
    venv_has_pip: bool | None = None

    if venv_is_symlink:
        try:
            venv_target = os.readlink(venv_path)
        except OSError:
            venv_target = None

    if venv_exists and venv_path.is_dir():
        pip_unix = venv_path / "bin" / "pip"
        pip_win = venv_path / "Scripts" / "pip.exe"
        venv_has_pip = pip_unix.exists() or pip_win.exists()

    warnings: list[str] = []
    if lines:
        warnings.append(
            f"Working tree is not clean ({staged_count} staged, {unstaged_count} unstaged, {untracked_count} untracked)."
        )
    if untracked_venv_entries:
        warnings.append(
            f"Untracked virtualenv entries detected ({', '.join(untracked_venv_entries)}). "
            "Consider adding them to .git/info/exclude."
        )
    if venv_exists and venv_has_pip is False:
        warnings.append(
            ".venv does not contain a 'pip' binary (typical of 'uv venv'). "
            "Ensure deployment scripts use 'uv pip' instead of 'pip'."
        )

    return GitStateInfo(
        branch=branch,
        base_branch=base_branch,
        base_branch_resolution=base_resolution,
        is_clean=len(lines) == 0,
        staged_count=staged_count,
        unstaged_count=unstaged_count,
        untracked_count=untracked_count,
        ahead_of_base=ahead_of_base,
        behind_base=behind_base,
        venv_exists=venv_exists,
        venv_is_symlink=venv_is_symlink,
        venv_target=venv_target,
        venv_has_pip=venv_has_pip,
        untracked_venv_entries=untracked_venv_entries,
        warnings=warnings,
    )


def _parse_requirements_file(req_path: Path) -> tuple[int, list[str]]:
    pinned = 0
    unpinned: list[str] = []
    try:
        lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0, []

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement", "-c ", "--constraint", "-e ", "--", "git+", "http:", "https:")):
            continue
        match = PYTHON_REQ_RE.match(line)
        if not match:
            continue
        pkg_name = match.group(1)
        operator = match.group(3)
        if operator == "==":
            pinned += 1
        else:
            unpinned.append(pkg_name)
    return pinned, unpinned


def _detect_python_stack(repo_root: Path) -> StackComponent | None:
    files: list[str] = []
    pinned_total = 0
    unpinned_all: list[str] = []
    details: list[str] = []
    managers: list[str] = []

    for directory in _candidate_dirs(repo_root):
        for req_file in sorted(directory.glob("requirements*.txt")):
            if req_file.is_file():
                rel = _rel_path(repo_root, req_file)
                files.append(rel)
                pinned, unpinned = _parse_requirements_file(req_file)
                pinned_total += pinned
                for pkg in unpinned:
                    label = f"{pkg} ({rel})" if directory != repo_root else pkg
                    if label not in unpinned_all:
                        unpinned_all.append(label)
                if "pip" not in managers:
                    managers.append("pip")

        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            files.append(_rel_path(repo_root, pyproject))
            if "pyproject" not in managers:
                managers.append("pyproject")

        uv_lock = directory / "uv.lock"
        if uv_lock.is_file():
            files.append(_rel_path(repo_root, uv_lock))
            if "uv" not in managers:
                managers.append("uv")

        poetry_lock = directory / "poetry.lock"
        if poetry_lock.is_file():
            files.append(_rel_path(repo_root, poetry_lock))
            if "poetry" not in managers:
                managers.append("poetry")

        py_ver = directory / ".python-version"
        if py_ver.is_file():
            files.append(_rel_path(repo_root, py_ver))

    if not files:
        return None

    if unpinned_all:
        details.append(f"{len(unpinned_all)} unpinned Python requirement(s)")
    if pinned_total:
        details.append(f"{pinned_total} pinned Python requirement(s) (==)")

    return StackComponent(
        ecosystem="python",
        name="Python",
        manager=", ".join(managers) or "pip",
        files=files,
        frameworks=[],
        pinned_count=pinned_total,
        unpinned_dependencies=unpinned_all,
        details=details,
    )


def _detect_node_stack(repo_root: Path) -> StackComponent | None:
    files: list[str] = []
    frameworks: list[str] = []
    managers: list[str] = []
    details: list[str] = []
    dep_count = 0

    for directory in _candidate_dirs(repo_root):
        pkg_json = directory / "package.json"
        if not pkg_json.is_file():
            continue
        files.append(_rel_path(repo_root, pkg_json))

        for lock_name, mgr_label in (
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("package-lock.json", "npm"),
        ):
            lock_path = directory / lock_name
            if lock_path.is_file():
                files.append(_rel_path(repo_root, lock_path))
                if mgr_label not in managers:
                    managers.append(mgr_label)

        for rc_name in (".nvmrc", ".node-version", ".npmrc"):
            rc_path = directory / rc_name
            if rc_path.is_file():
                files.append(_rel_path(repo_root, rc_path))

        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except Exception:
            data = {}

        if isinstance(data, dict):
            deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
            dev_deps = data.get("devDependencies") if isinstance(data.get("devDependencies"), dict) else {}
            combined = {**deps, **dev_deps}
            dep_count += len(combined)
            for key, label in KNOWN_NODE_FRAMEWORKS:
                if key in combined and label not in frameworks:
                    frameworks.append(label)

    if not files:
        return None

    if not managers:
        managers.append("npm (no lockfile)")
        details.append("No Node.js lockfile detected")

    return StackComponent(
        ecosystem="nodejs",
        name="Node.js" + (f" ({', '.join(frameworks[:3])})" if frameworks else ""),
        manager=", ".join(managers),
        files=files,
        frameworks=frameworks,
        pinned_count=dep_count,
        unpinned_dependencies=[],
        details=details,
    )


def _detect_ruby_stack(repo_root: Path) -> StackComponent | None:
    files: list[str] = []
    frameworks: list[str] = []

    for directory in _candidate_dirs(repo_root):
        gemfile = directory / "Gemfile"
        if not gemfile.is_file():
            continue
        files.append(_rel_path(repo_root, gemfile))
        gemfile_lock = directory / "Gemfile.lock"
        if gemfile_lock.is_file():
            files.append(_rel_path(repo_root, gemfile_lock))
        ruby_ver = directory / ".ruby-version"
        if ruby_ver.is_file():
            files.append(_rel_path(repo_root, ruby_ver))
        try:
            content = gemfile.read_text(encoding="utf-8")
            if "rails" in content.lower():
                frameworks.append("Rails")
        except OSError:
            pass

    if not files:
        return None

    return StackComponent(
        ecosystem="ruby",
        name="Ruby on Rails" if "Rails" in frameworks else "Ruby",
        manager="bundler",
        files=files,
        frameworks=frameworks,
    )


def _detect_docker_and_ci(repo_root: Path) -> list[StackComponent]:
    components: list[StackComponent] = []

    docker_files: list[str] = []
    for directory in _candidate_dirs(repo_root):
        for candidate in sorted(directory.glob("Dockerfile*")):
            if candidate.is_file():
                docker_files.append(_rel_path(repo_root, candidate))
        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml"):
            for candidate in sorted(directory.glob(pattern)):
                if candidate.is_file():
                    rel = _rel_path(repo_root, candidate)
                    if rel not in docker_files:
                        docker_files.append(rel)

    if docker_files:
        components.append(
            StackComponent(
                ecosystem="docker",
                name="Docker / Compose",
                manager="docker",
                files=docker_files,
            )
        )

    ci_files: list[str] = []
    ci_managers: list[str] = []
    jenkinsfile = repo_root / "Jenkinsfile"
    if jenkinsfile.is_file():
        ci_files.append("Jenkinsfile")
        ci_managers.append("Jenkins")

    workflows_dir = repo_root / ".github" / "workflows"
    if workflows_dir.is_dir():
        for wf in sorted(workflows_dir.glob("*.y*ml")):
            if wf.is_file():
                ci_files.append(_rel_path(repo_root, wf))
                if "GitHub Actions" not in ci_managers:
                    ci_managers.append("GitHub Actions")

    if ci_files:
        components.append(
            StackComponent(
                ecosystem="ci",
                name="CI Pipelines",
                manager=", ".join(ci_managers),
                files=ci_files,
            )
        )

    return components


def detect_deployment_files(repo_root: Path, repo_name: str, git_state: GitStateInfo) -> tuple[list[str], list[str]]:
    found: list[str] = []
    warnings: list[str] = []

    local_candidates = [
        repo_root / "deploy.sh",
        repo_root / "start.sh",
        repo_root / "scripts" / "deploy.sh",
    ]
    for candidate in local_candidates:
        if candidate.is_file():
            rel = _rel_path(repo_root, candidate)
            found.append(rel)
            try:
                text = candidate.read_text(encoding="utf-8")
                if re.search(r"(^|\s)pip\s+install\b", text) and "uv pip" not in text:
                    if git_state.venv_exists and git_state.venv_has_pip is False:
                        warnings.append(
                            f"Deployment script '{rel}' calls 'pip install' while .venv has no pip binary."
                        )
            except OSError:
                pass

    deploy_root = os.getenv("DEVTOOLS_DEPLOY_ROOT", "/srv/apps").strip()
    if deploy_root:
        ext_deploy = Path(deploy_root) / repo_name / "deploy.sh"
        if ext_deploy.is_file():
            found.append(str(ext_deploy))
            try:
                text = ext_deploy.read_text(encoding="utf-8")
                if re.search(r"(^|\s)pip\s+install\b", text) and "uv pip" not in text:
                    warnings.append(
                        f"External deploy script '{ext_deploy}' uses direct 'pip install' (incompatible with pip-less uv venv)."
                    )
            except OSError:
                pass

    return found, warnings


def detect_project_stacks(
    repo_path: str,
    repo_name: str,
    git_state: GitStateInfo | None = None,
) -> tuple[list[StackComponent], list[str], list[str]]:
    repo_root = Path(repo_path)
    state = git_state or inspect_git_state(repo_path)

    stacks: list[StackComponent] = []
    reservations: list[str] = list(state.warnings)

    py_stack = _detect_python_stack(repo_root)
    if py_stack:
        stacks.append(py_stack)
        if py_stack.unpinned_dependencies:
            sample = ", ".join(py_stack.unpinned_dependencies[:6])
            more = (
                f" (+{len(py_stack.unpinned_dependencies) - 6} more)"
                if len(py_stack.unpinned_dependencies) > 6
                else ""
            )
            reservations.append(
                f"Unpinned Python dependencies detected: {sample}{more}. "
                "Different environments may install different versions over time."
            )

    node_stack = _detect_node_stack(repo_root)
    if node_stack:
        stacks.append(node_stack)
        for detail in node_stack.details:
            if "No Node.js lockfile" in detail:
                reservations.append("Node.js package.json has no lockfile.")

    ruby_stack = _detect_ruby_stack(repo_root)
    if ruby_stack:
        stacks.append(ruby_stack)

    stacks.extend(_detect_docker_and_ci(repo_root))

    deployment_files, deploy_warnings = detect_deployment_files(repo_root, repo_name, state)
    reservations.extend(deploy_warnings)

    return stacks, deployment_files, reservations


def inspect_renovate_config(repo_path: str) -> RenovateConfigInfo:
    repo_root = Path(repo_path)
    config_file: Path | None = None
    rel_config = "renovate.json"

    for candidate in RENOVATE_CONFIG_CANDIDATES:
        path = repo_root / candidate
        if path.is_file():
            config_file = path
            rel_config = candidate
            break

    private_registries: list[str] = []
    for directory in _candidate_dirs(repo_root):
        npmrc_path = directory / ".npmrc"
        if npmrc_path.is_file():
            try:
                for line in npmrc_path.read_text(encoding="utf-8").splitlines():
                    cleaned = line.strip()
                    if not cleaned or cleaned.startswith("#") or "_authToken" in cleaned:
                        continue
                    if "registry=" in cleaned:
                        reg_label = f"{_rel_path(repo_root, npmrc_path)}: {cleaned}"
                        if reg_label not in private_registries:
                            private_registries.append(reg_label)
            except OSError:
                pass

    if config_file is None:
        return RenovateConfigInfo(
            exists=False,
            path=rel_config,
            valid_json=False,
            has_private_registry=bool(private_registries),
            private_registries=private_registries,
            issues=["Missing renovate.json configuration file."],
        )

    try:
        raw_text = config_file.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception as error:
        return RenovateConfigInfo(
            exists=True,
            path=rel_config,
            valid_json=False,
            has_private_registry=bool(private_registries),
            private_registries=private_registries,
            issues=[f"Invalid JSON in {rel_config}: {error}"],
        )

    if not isinstance(data, dict):
        return RenovateConfigInfo(
            exists=True,
            path=rel_config,
            valid_json=False,
            has_private_registry=bool(private_registries),
            private_registries=private_registries,
            issues=[f"Root of {rel_config} must be a JSON object."],
        )

    raw_extends = data.get("extends")
    extends_list = [str(item) for item in raw_extends] if isinstance(raw_extends, list) else []
    is_dev_platform_repo = (repo_root / "configs" / "renovate" / "base.json").is_file()
    extends_dev_platform = DEV_PLATFORM_PRESET in extends_list or is_dev_platform_repo

    base_branches = (
        [str(b) for b in data.get("baseBranchPatterns")]
        if isinstance(data.get("baseBranchPatterns"), list)
        else []
    )
    pr_limit = data.get("prConcurrentLimit") if isinstance(data.get("prConcurrentLimit"), int) else None
    automerge = data.get("automerge") if isinstance(data.get("automerge"), bool) else None

    if isinstance(data.get("npmrc"), str) and data["npmrc"].strip():
        npmrc_inline = f"{rel_config}#npmrc: {data['npmrc'].strip()}"
        if npmrc_inline not in private_registries:
            private_registries.append(npmrc_inline)

    local_overrides: list[str] = []
    for key in sorted(data.keys()):
        if key in {"$schema", "extends"}:
            continue
        local_overrides.append(key)

    issues: list[str] = []
    if not extends_dev_platform:
        issues.append(
            f"{rel_config} does not extend central preset '{DEV_PLATFORM_PRESET}'."
        )
    if automerge is True:
        issues.append("automerge is enabled in renovate.json (human validation required by policy).")

    return RenovateConfigInfo(
        exists=True,
        path=rel_config,
        valid_json=True,
        extends_dev_platform=extends_dev_platform,
        extends=extends_list,
        base_branch_patterns=base_branches,
        pr_concurrent_limit=pr_limit,
        automerge=automerge,
        has_private_registry=bool(private_registries),
        private_registries=private_registries,
        local_overrides=local_overrides,
        issues=issues,
    )
