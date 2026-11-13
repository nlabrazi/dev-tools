import os
import re
from pathlib import Path

from core.maintenance.models import ProductionEnvironmentInfo
from utils.common import run_command

PYVENV_VERSION_RE = re.compile(r"^\s*version(?:_info)?\s*=\s*([0-9]+\.[0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
VENV_NAME_VERSION_RE = re.compile(r"\.venv-([0-9])([0-9]{1,2})\b")
RAW_PIP_INSTALL_RE = re.compile(r"(?<!uv\s)\bpip3?\s+install\b")
UV_PIP_INSTALL_RE = re.compile(r"\buv(?:\"|'|\s)+pip\s+install\b")


def _extract_python_version_from_venv(venv_dir: Path, target_name: str | None) -> str | None:
    cfg = venv_dir / "pyvenv.cfg"
    if cfg.is_file():
        try:
            content = cfg.read_text(encoding="utf-8", errors="ignore")
            match = PYVENV_VERSION_RE.search(content)
            if match:
                return match.group(1)
        except OSError:
            pass

    if target_name:
        match = VENV_NAME_VERSION_RE.search(target_name)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return None


def _inspect_local_production_path(app_dir: Path) -> ProductionEnvironmentInfo:
    if not app_dir.exists() or not app_dir.is_dir():
        return ProductionEnvironmentInfo(
            configured=True,
            reachable=False,
            access_mode="local_path",
            app_path=str(app_dir),
            issues=[f"Configured production directory '{app_dir}' does not exist or is unreachable."],
            details="Production path not found.",
        )

    issues: list[str] = []
    runtime_versions: dict[str, str] = {}

    venv_entry = app_dir / ".venv"
    venv_path: str | None = None
    venv_is_symlink = False
    venv_target: str | None = None
    venv_has_pip: bool | None = None

    if venv_entry.exists() or venv_entry.is_symlink():
        venv_path = str(venv_entry)
        venv_is_symlink = venv_entry.is_symlink()
        if venv_is_symlink:
            try:
                venv_target = os.readlink(venv_entry)
            except OSError:
                venv_target = None
        resolved_venv = (app_dir / venv_target) if venv_target else venv_entry
        pip_bin = resolved_venv / "bin" / "pip"
        py_bin = resolved_venv / "bin" / "python"
        if py_bin.exists() or (resolved_venv / "bin").is_dir():
            venv_has_pip = pip_bin.exists()
        py_ver = _extract_python_version_from_venv(resolved_venv, venv_target)
        if py_ver:
            runtime_versions["Python"] = py_ver

    backup_venvs: list[str] = []
    active_target_name = Path(venv_target).name if venv_target else ""
    try:
        for child in sorted(app_dir.iterdir(), key=lambda p: p.name):
            if not child.name.startswith(".venv") or child.name == ".venv":
                continue
            if child.name == active_target_name:
                continue
            if child.is_dir() or child.is_symlink():
                backup_venvs.append(child.name)
    except OSError:
        pass

    if venv_path and not backup_venvs:
        issues.append(
            f"No backup virtualenv (.venv-*-backup) detected in {app_dir} for instant symlink rollback."
        )

    deploy_script_path: str | None = None
    deploy_uses_uv_pip: bool | None = None
    deploy_candidate = app_dir / "deploy.sh"
    if deploy_candidate.is_file():
        deploy_script_path = str(deploy_candidate)
        try:
            script_text = deploy_candidate.read_text(encoding="utf-8", errors="ignore")
            uses_uv = bool(UV_PIP_INSTALL_RE.search(script_text))
            uses_raw_pip = bool(RAW_PIP_INSTALL_RE.search(script_text)) and not uses_uv
            deploy_uses_uv_pip = uses_uv
            if venv_has_pip is False and uses_raw_pip:
                issues.append(
                    "Production deploy.sh uses raw 'pip install' while production .venv has no pip binary; use 'uv pip install --python .venv/bin/python'."
                )
        except OSError:
            pass

    return ProductionEnvironmentInfo(
        configured=True,
        reachable=True,
        access_mode="local_path",
        app_path=str(app_dir),
        runtime_versions=runtime_versions,
        venv_path=venv_path,
        venv_is_symlink=venv_is_symlink,
        venv_target=venv_target,
        venv_has_pip=venv_has_pip,
        backup_venvs=backup_venvs,
        deploy_script_path=deploy_script_path,
        deploy_uses_uv_pip=deploy_uses_uv_pip,
        issues=issues,
        details=f"Inspected configured production directory at {app_dir}.",
    )


def _inspect_ssh_production(host: str, app_path: str) -> ProductionEnvironmentInfo:
    probe_script = (
        f"if [ ! -d '{app_path}' ]; then echo 'REACHABLE=0'; exit 0; fi; "
        "echo 'REACHABLE=1'; "
        f"if [ -L '{app_path}/.venv' ]; then echo 'VENV_SYMLINK=1'; echo \"VENV_TARGET=$(readlink '{app_path}/.venv')\"; "
        f"elif [ -d '{app_path}/.venv' ]; then echo 'VENV_SYMLINK=0'; fi; "
        f"if [ -x '{app_path}/.venv/bin/python' ]; then "
        f"echo \"PYTHON_VERSION=$('{app_path}/.venv/bin/python' -c 'import platform; print(platform.python_version())' 2>/dev/null)\"; "
        f"if [ -x '{app_path}/.venv/bin/pip' ]; then echo 'VENV_HAS_PIP=1'; else echo 'VENV_HAS_PIP=0'; fi; "
        "fi; "
        f"echo \"BACKUP_VENVS=$(find '{app_path}' -maxdepth 1 -name '.venv*backup*' -printf '%f,' 2>/dev/null)\""
    )
    res = run_command(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, probe_script],
        silent=True,
    )
    if res.returncode != 0:
        err = ((res.stderr or "") + "\n" + (res.stdout or "")).strip() or f"exit code {res.returncode}"
        return ProductionEnvironmentInfo(
            configured=True,
            reachable=False,
            access_mode="ssh",
            app_path=app_path,
            issues=[f"Production SSH host '{host}' could not be reached ({err})."],
            details=f"SSH probe to {host}:{app_path} failed; production runtime versions remain UNKNOWN.",
        )

    kv: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    if kv.get("REACHABLE") != "1":
        return ProductionEnvironmentInfo(
            configured=True,
            reachable=False,
            access_mode="ssh",
            app_path=app_path,
            issues=[f"Application directory '{app_path}' not found on SSH host '{host}'."],
            details=f"Directory {app_path} missing on {host}.",
        )

    runtime_versions: dict[str, str] = {}
    if kv.get("PYTHON_VERSION"):
        runtime_versions["Python"] = kv["PYTHON_VERSION"]
    if kv.get("POSTGRES_VERSION"):
        runtime_versions["PostgreSQL"] = kv["POSTGRES_VERSION"]

    venv_symlink = kv.get("VENV_SYMLINK") == "1"
    venv_target = kv.get("VENV_TARGET") or None
    venv_has_pip = None
    if "VENV_HAS_PIP" in kv:
        venv_has_pip = kv["VENV_HAS_PIP"] == "1"

    backups = [b for b in (kv.get("BACKUP_VENVS") or "").split(",") if b]
    issues: list[str] = []
    if not backups:
        issues.append(f"No backup virtualenv (.venv*backup*) detected on {host}:{app_path}.")

    return ProductionEnvironmentInfo(
        configured=True,
        reachable=True,
        access_mode="ssh",
        app_path=app_path,
        runtime_versions=runtime_versions,
        venv_path=f"{app_path}/.venv" if "VENV_SYMLINK" in kv else None,
        venv_is_symlink=venv_symlink,
        venv_target=venv_target,
        venv_has_pip=venv_has_pip,
        backup_venvs=backups,
        issues=issues,
        details=f"Inspected production via SSH ({host}:{app_path}).",
    )


def inspect_production_environment(
    repo_name: str,
    *,
    deploy_root: str | None = None,
    ssh_host: str | None = None,
) -> ProductionEnvironmentInfo:
    effective_deploy_root = (
        deploy_root
        if deploy_root is not None
        else os.environ.get("DEVTOOLS_PRODUCTION_DEPLOY_ROOT", "").strip()
    )
    effective_ssh_host = (
        ssh_host
        if ssh_host is not None
        else os.environ.get("DEVTOOLS_PRODUCTION_SSH_HOST", "").strip()
    )

    default_app_path = f"/srv/apps/{repo_name}"

    if effective_deploy_root:
        return _inspect_local_production_path(Path(effective_deploy_root) / repo_name)

    if effective_ssh_host:
        return _inspect_ssh_production(effective_ssh_host, default_app_path)

    return ProductionEnvironmentInfo(
        configured=False,
        reachable=False,
        access_mode="unconfigured",
        app_path=default_app_path,
        details="Production access not configured (set DEVTOOLS_PRODUCTION_SSH_HOST or DEVTOOLS_PRODUCTION_DEPLOY_ROOT).",
    )
