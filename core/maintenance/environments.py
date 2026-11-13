import json
import re
from pathlib import Path

from core.maintenance.detector import SERVICE_SUBDIRS, _rel_path
from core.maintenance.models import (
    DRIFT_ACCEPTED,
    DRIFT_ALIGNED,
    DRIFT_INFO_UNAVAILABLE,
    DRIFT_PROBLEMATIC,
    STATUS_UNKNOWN,
    DeclaredVersion,
    EnvironmentMatrixRow,
)

ARG_DEF_RE = re.compile(r"^\s*ARG\s+([A-Za-z0-9_]+)\s*=\s*([^\s#]+)", re.IGNORECASE)
FROM_IMAGE_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?([^\s#]+)", re.IGNORECASE)
IMAGE_TAG_RE = re.compile(
    r"(?:^|/)(python|node|ruby|postgres|playwright):v?([0-9]+(?:\.[0-9]+){0,2}[A-Za-z0-9.\-_]*)",
    re.IGNORECASE,
)
JENKINS_IMAGE_RE = re.compile(r"""image\s+['"]([^'"]+)['"]""")
JENKINS_DOCKER_RUN_POSTGRES_RE = re.compile(r"\bpostgres:([0-9]+(?:\.[0-9]+){0,2}[A-Za-z0-9.\-_]*)")
JENKINS_USES_DOCKERFILE_RE = re.compile(r"\bdockerfile\s*\{")
GHA_PYTHON_VER_RE = re.compile(r"""python-version\s*:\s*['"]?([0-9]+(?:\.[0-9]+){0,2}[^'"\s#]*)['"]?""")
GHA_NODE_VER_RE = re.compile(r"""node-version\s*:\s*['"]?([0-9]+(?:\.[0-9]+){0,2}[^'"\s#]*)['"]?""")
GHA_RUBY_VER_RE = re.compile(r"""ruby-version\s*:\s*['"]?([0-9]+(?:\.[0-9]+){0,2}[^'"\s#]*)['"]?""")
YAML_IMAGE_RE = re.compile(r"""image\s*:\s*['"]?([^\s'"#]+)['"]?""")

COMPONENT_MAP = {
    "python": "Python",
    "node": "Node.js",
    "ruby": "Ruby",
    "postgres": "PostgreSQL",
    "playwright": "Playwright",
}


def _clean_image_version(raw_tag: str) -> str:
    cleaned = raw_tag.strip().lstrip("v")
    for suffix in (
        "-bookworm-slim",
        "-bookworm",
        "-bullseye-slim",
        "-bullseye",
        "-slim",
        "-alpine",
        "-noble",
        "-jammy",
    ):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned


def _extract_image_component_version(image_ref: str) -> tuple[str, str] | None:
    match = IMAGE_TAG_RE.search(image_ref.strip())
    if not match:
        return None
    raw_name = match.group(1).lower()
    raw_tag = match.group(2)
    component = COMPONENT_MAP.get(raw_name)
    if not component:
        return None
    return component, _clean_image_version(raw_tag)


def _extract_local_versions(repo_root: Path) -> list[DeclaredVersion]:
    found: list[DeclaredVersion] = []
    dirs = [repo_root] + [repo_root / d for d in SERVICE_SUBDIRS if (repo_root / d).is_dir()]

    pyvenv_cfg = repo_root / ".venv" / "pyvenv.cfg"
    if pyvenv_cfg.is_file():
        try:
            for idx, line in enumerate(pyvenv_cfg.read_text(encoding="utf-8").splitlines(), start=1):
                if line.strip().startswith("version"):
                    _, val = line.split("=", 1)
                    ver = val.strip()
                    if ver:
                        found.append(DeclaredVersion("Python", "Local", ver, f".venv/pyvenv.cfg:L{idx}"))
                        break
        except OSError:
            pass

    for directory in dirs:
        py_ver_file = directory / ".python-version"
        if py_ver_file.is_file():
            try:
                ver = py_ver_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
                if ver:
                    found.append(DeclaredVersion("Python", "Local", ver, _rel_path(repo_root, py_ver_file)))
            except (OSError, IndexError):
                pass

        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            try:
                for idx, line in enumerate(pyproject.read_text(encoding="utf-8").splitlines(), start=1):
                    m = re.search(r"""requires-python\s*=\s*['"]([^'"]+)['"]""", line)
                    if m:
                        found.append(
                            DeclaredVersion(
                                "Python",
                                "Local",
                                m.group(1).strip(),
                                f"{_rel_path(repo_root, pyproject)}:L{idx}",
                            )
                        )
            except OSError:
                pass

        for node_rc in (".nvmrc", ".node-version"):
            rc_file = directory / node_rc
            if rc_file.is_file():
                try:
                    ver = rc_file.read_text(encoding="utf-8").strip().splitlines()[0].strip().lstrip("v")
                    if ver:
                        found.append(DeclaredVersion("Node.js", "Local", ver, _rel_path(repo_root, rc_file)))
                except (OSError, IndexError):
                    pass

        ruby_ver_file = directory / ".ruby-version"
        if ruby_ver_file.is_file():
            try:
                ver = ruby_ver_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
                if ver:
                    found.append(DeclaredVersion("Ruby", "Local", ver, _rel_path(repo_root, ruby_ver_file)))
            except (OSError, IndexError):
                pass

        pkg_json = directory / "package.json"
        if pkg_json.is_file():
            try:
                data = json.loads(pkg_json.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if isinstance(data, dict):
                rel = _rel_path(repo_root, pkg_json)
                engines = data.get("engines")
                if isinstance(engines, dict) and isinstance(engines.get("node"), str):
                    found.append(DeclaredVersion("Node.js", "Local", engines["node"].strip(), f"{rel}#engines.node"))
                deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
                dev_deps = data.get("devDependencies") if isinstance(data.get("devDependencies"), dict) else {}
                combined = {**deps, **dev_deps}
                pw_ver = combined.get("@playwright/test") or combined.get("playwright")
                if isinstance(pw_ver, str) and pw_ver.strip():
                    found.append(
                        DeclaredVersion(
                            "Playwright",
                            "Local",
                            pw_ver.strip().lstrip("^~"),
                            f"{rel}#@playwright/test",
                        )
                    )

    return found


def _extract_dockerfile_versions(repo_root: Path) -> list[DeclaredVersion]:
    found: list[DeclaredVersion] = []
    dirs = [repo_root] + [repo_root / d for d in SERVICE_SUBDIRS if (repo_root / d).is_dir()]

    for directory in dirs:
        for dockerfile in sorted(directory.glob("Dockerfile*")):
            if not dockerfile.is_file():
                continue
            rel = _rel_path(repo_root, dockerfile)
            args_map: dict[str, str] = {}
            try:
                lines = dockerfile.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue

            for idx, line in enumerate(lines, start=1):
                arg_match = ARG_DEF_RE.match(line)
                if arg_match:
                    args_map[arg_match.group(1)] = arg_match.group(2).strip("'\"")
                    continue
                from_match = FROM_IMAGE_RE.match(line)
                if not from_match:
                    continue
                raw_image = from_match.group(1).strip()
                for arg_key, arg_val in args_map.items():
                    raw_image = raw_image.replace(f"${{{arg_key}}}", arg_val).replace(f"${arg_key}", arg_val)
                extracted = _extract_image_component_version(raw_image)
                if extracted:
                    component, version = extracted
                    found.append(DeclaredVersion(component, "Docker", version, f"{rel}:L{idx}"))

        for pattern in ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml"):
            for compose_file in sorted(directory.glob(pattern)):
                if not compose_file.is_file():
                    continue
                rel = _rel_path(repo_root, compose_file)
                try:
                    lines = compose_file.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                for idx, line in enumerate(lines, start=1):
                    img_match = YAML_IMAGE_RE.search(line)
                    if not img_match:
                        continue
                    extracted = _extract_image_component_version(img_match.group(1))
                    if extracted:
                        component, version = extracted
                        found.append(DeclaredVersion(component, "Docker", version, f"{rel}:L{idx}"))

    return found


def _extract_jenkins_versions(repo_root: Path, docker_versions: list[DeclaredVersion]) -> list[DeclaredVersion]:
    jenkinsfile = repo_root / "Jenkinsfile"
    if not jenkinsfile.is_file():
        return []

    found: list[DeclaredVersion] = []
    try:
        content = jenkinsfile.read_text(encoding="utf-8")
    except OSError:
        return []

    lines = content.splitlines()
    for idx, line in enumerate(lines, start=1):
        for img_match in JENKINS_IMAGE_RE.finditer(line):
            extracted = _extract_image_component_version(img_match.group(1))
            if extracted:
                component, version = extracted
                found.append(DeclaredVersion(component, "Jenkins", version, f"Jenkinsfile:L{idx}"))

        for pg_match in JENKINS_DOCKER_RUN_POSTGRES_RE.finditer(line):
            found.append(
                DeclaredVersion(
                    "PostgreSQL",
                    "Jenkins",
                    _clean_image_version(pg_match.group(1)),
                    f"Jenkinsfile:L{idx}",
                )
            )

    if JENKINS_USES_DOCKERFILE_RE.search(content):
        for dv in docker_versions:
            if dv.source_file.startswith("Dockerfile"):
                found.append(
                    DeclaredVersion(
                        dv.component,
                        "Jenkins",
                        dv.version,
                        f"Jenkinsfile->Dockerfile ({dv.source_file})",
                    )
                )

    return found


def _extract_github_actions_versions(repo_root: Path) -> list[DeclaredVersion]:
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []

    found: list[DeclaredVersion] = []
    for wf in sorted(workflows_dir.glob("*.y*ml")):
        if not wf.is_file():
            continue
        rel = _rel_path(repo_root, wf)
        try:
            lines = wf.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        for idx, line in enumerate(lines, start=1):
            py_m = GHA_PYTHON_VER_RE.search(line)
            if py_m:
                found.append(DeclaredVersion("Python", "GitHub Actions", py_m.group(1), f"{rel}:L{idx}"))
            node_m = GHA_NODE_VER_RE.search(line)
            if node_m:
                found.append(DeclaredVersion("Node.js", "GitHub Actions", node_m.group(1), f"{rel}:L{idx}"))
            ruby_m = GHA_RUBY_VER_RE.search(line)
            if ruby_m:
                found.append(DeclaredVersion("Ruby", "GitHub Actions", ruby_m.group(1), f"{rel}:L{idx}"))
            img_m = YAML_IMAGE_RE.search(line)
            if img_m:
                extracted = _extract_image_component_version(img_m.group(1))
                if extracted:
                    component, version = extracted
                    found.append(DeclaredVersion(component, "GitHub Actions", version, f"{rel}:L{idx}"))

    return found


def collect_declared_versions(repo_path: str) -> list[DeclaredVersion]:
    repo_root = Path(repo_path)
    local_versions = _extract_local_versions(repo_root)
    docker_versions = _extract_dockerfile_versions(repo_root)
    jenkins_versions = _extract_jenkins_versions(repo_root, docker_versions)
    gha_versions = _extract_github_actions_versions(repo_root)
    return local_versions + gha_versions + jenkins_versions + docker_versions


def _unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for val in values:
        if val and val not in seen:
            seen.add(val)
            result.append(val)
    return result


def _comparable_tokens(version_str: str) -> list[str]:
    tokens: list[str] = []
    for part in version_str.split(","):
        cleaned = part.strip()
        if not cleaned or cleaned in {STATUS_UNKNOWN, "N/A"}:
            continue
        cleaned = re.sub(r"^[~^>=<]+", "", cleaned).strip().lstrip("v")
        if cleaned:
            tokens.append(cleaned)
    return _unique_ordered(tokens)


def _versions_are_aligned(component: str, env_values: dict[str, str]) -> tuple[str, str]:
    known_envs = {
        env: val
        for env, val in env_values.items()
        if val not in {STATUS_UNKNOWN, "N/A", ""}
    }
    if not known_envs:
        return DRIFT_INFO_UNAVAILABLE, "No declared version detected across local/CI files."

    token_map = {env: _comparable_tokens(val) for env, val in known_envs.items()}
    all_tokens = _unique_ordered([tok for toks in token_map.values() for tok in toks])

    if len(known_envs) == 1:
        only_env = next(iter(known_envs))
        return (
            DRIFT_INFO_UNAVAILABLE,
            f"Declared only in {only_env} ({known_envs[only_env]}); other environments not configured or unknown.",
        )

    if len(all_tokens) == 1:
        return (
            DRIFT_ALIGNED,
            f"Aligned on {all_tokens[0]} across {', '.join(known_envs.keys())} (Production: {env_values.get('Production', STATUS_UNKNOWN)}).",
        )

    if component == "PostgreSQL":
        summary = ", ".join(f"{env}={val}" for env, val in known_envs.items())
        return (
            DRIFT_ACCEPTED,
            f"Service version difference ({summary}). Do not force DB upgrade/downgrade solely to match numbers; verify driver compatibility.",
        )

    summary = ", ".join(f"{env}={val}" for env, val in known_envs.items())
    return (
        DRIFT_PROBLEMATIC,
        f"Version mismatch detected across environments: {summary}.",
    )


def build_environment_matrix(
    repo_path: str,
    declared: list[DeclaredVersion] | None = None,
    production_overrides: dict[str, str] | None = None,
) -> list[EnvironmentMatrixRow]:
    repo_root = Path(repo_path)
    items = declared if declared is not None else collect_declared_versions(repo_path)
    prod_map = production_overrides or {}

    has_gha = (repo_root / ".github" / "workflows").is_dir() and any(
        (repo_root / ".github" / "workflows").glob("*.y*ml")
    )
    has_jenkins = (repo_root / "Jenkinsfile").is_file()
    has_docker = any(
        d.is_file()
        for directory in ([repo_root] + [repo_root / s for s in SERVICE_SUBDIRS if (repo_root / s).is_dir()])
        for pattern in ("Dockerfile*", "docker-compose*.y*ml", "compose*.y*ml")
        for d in directory.glob(pattern)
    )

    ordered_components: list[str] = []
    for item in items:
        if item.component not in ordered_components:
            ordered_components.append(item.component)
    for comp in prod_map:
        if comp not in ordered_components:
            ordered_components.append(comp)

    rows: list[EnvironmentMatrixRow] = []
    for component in ordered_components:
        comp_items = [i for i in items if i.component == component]

        def cell_for(env_name: str, env_exists: bool = True) -> str:
            vals = _unique_ordered([i.version for i in comp_items if i.environment == env_name])
            if vals:
                return ", ".join(vals)
            return STATUS_UNKNOWN if env_exists else "N/A"

        local_val = cell_for("Local", True)
        gha_val = cell_for("GitHub Actions", has_gha)
        jenkins_val = cell_for("Jenkins", has_jenkins)
        docker_val = cell_for("Docker", has_docker)
        prod_val = prod_map.get(component, STATUS_UNKNOWN)

        env_values = {
            "Local": local_val,
            "GitHub Actions": gha_val,
            "Jenkins": jenkins_val,
            "Docker": docker_val,
            "Production": prod_val,
        }
        drift_status, drift_details = _versions_are_aligned(component, env_values)
        sources = _unique_ordered([f"{i.environment}: {i.source_file} ({i.version})" for i in comp_items])

        rows.append(
            EnvironmentMatrixRow(
                component=component,
                local=local_val,
                github_actions=gha_val,
                jenkins=jenkins_val,
                docker=docker_val,
                production=prod_val,
                drift_status=drift_status,
                drift_details=drift_details,
                sources=sources,
            )
        )

    return rows
