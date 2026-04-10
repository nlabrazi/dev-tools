import os

from utils.git import git_command, git_output

LEGACY_BASE_BRANCH = "master"


def _env_name(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _optional_env_name(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _resolve_root_dirs() -> list[str]:
    raw = os.getenv("DEVTOOLS_ROOT_DIRS", "").strip()
    if raw:
        return [
            os.path.expanduser(part.strip())
            for part in raw.split(os.pathsep)
            if part.strip()
        ]

    return [
        os.path.expanduser("~/code/pers"),
        os.path.expanduser("~/code/bricolage"),
        os.path.expanduser("/mnt/d/Unity/Projects"),
    ]


def get_base_branch_override() -> str | None:
    return _optional_env_name("DEVTOOLS_BASE_BRANCH")


def is_base_branch_explicit() -> bool:
    return get_base_branch_override() is not None


def get_base_branch_fallback() -> str:
    return LEGACY_BASE_BRANCH


def describe_base_branch_strategy(remote: str | None = None) -> str:
    remote_name = remote or DEFAULT_REMOTE
    override = get_base_branch_override()
    if override:
        return f"configured branch '{override}' on {remote_name}"
    return f"{remote_name}/HEAD default branch with legacy fallback '{LEGACY_BASE_BRANCH}'"


def get_default_remote_branch(repo_path: str, remote: str | None = None) -> str | None:
    remote_name = remote or DEFAULT_REMOTE
    ref = git_output(repo_path, ["symbolic-ref", f"refs/remotes/{remote_name}/HEAD"])
    prefix = f"refs/remotes/{remote_name}/"
    if ref.startswith(prefix):
        branch = ref.split(prefix, 1)[1].strip()
        return branch or None
    return None


def remote_branch_exists(repo_path: str, branch: str, remote: str | None = None) -> bool:
    remote_name = remote or DEFAULT_REMOTE
    if not branch:
        return False
    result = git_command(
        repo_path,
        ["show-ref", "--verify", "--quiet", f"refs/remotes/{remote_name}/{branch}"],
        silent=True,
    )
    return result.returncode == 0


def resolve_repo_base_branch(repo_path: str, remote: str | None = None) -> tuple[str | None, str]:
    remote_name = remote or DEFAULT_REMOTE
    override = get_base_branch_override()
    if override:
        if remote_branch_exists(repo_path, override, remote_name):
            return override, f"configured via DEVTOOLS_BASE_BRANCH ({override})"
        return None, f"{remote_name}/{override} is missing while DEVTOOLS_BASE_BRANCH is set"

    remote_default = get_default_remote_branch(repo_path, remote_name)
    if remote_default and remote_branch_exists(repo_path, remote_default, remote_name):
        return remote_default, f"resolved from {remote_name}/HEAD ({remote_default})"

    fallback = get_base_branch_fallback()
    if remote_branch_exists(repo_path, fallback, remote_name):
        return fallback, f"legacy fallback ({fallback})"

    if remote_default:
        return None, f"{remote_name}/HEAD points to '{remote_default}' but {remote_name}/{remote_default} is unavailable"
    return None, f"could not resolve {remote_name}/HEAD and {remote_name}/{fallback} is unavailable"


ROOT_DIRS = _resolve_root_dirs()
DEFAULT_REMOTE = _env_name("DEVTOOLS_REMOTE", "origin")
DEFAULT_BASE_BRANCH = _env_name("DEVTOOLS_BASE_BRANCH", LEGACY_BASE_BRANCH)
DEFAULT_HEAD_BRANCH = _env_name("DEVTOOLS_HEAD_BRANCH", "staging")
CHANGELOG_FILENAME = "CHANGELOG.md"
