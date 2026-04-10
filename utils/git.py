import subprocess

from utils.common import env_int, run_command, run_command_checked

DEFAULT_GIT_TIMEOUT = 60


def get_git_timeout() -> int:
    return env_int("DEVTOOLS_GIT_TIMEOUT", DEFAULT_GIT_TIMEOUT, minimum=1)


def git_command(
    repo_path: str,
    args: list[str],
    *,
    silent: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return run_command(
        ["git"] + args,
        cwd=repo_path,
        silent=silent,
        timeout=get_git_timeout() if timeout is None else timeout,
    )


def git_command_checked(
    repo_path: str,
    args: list[str],
    *,
    silent: bool = True,
    context: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return run_command_checked(
        ["git"] + args,
        cwd=repo_path,
        silent=silent,
        context=context,
        timeout=get_git_timeout() if timeout is None else timeout,
    )


def git_output(
    repo_path: str,
    args: list[str],
    *,
    silent: bool = True,
    timeout: float | None = None,
) -> str:
    result = git_command(
        repo_path,
        args,
        silent=silent,
        timeout=timeout,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def git_output_checked(
    repo_path: str,
    args: list[str],
    *,
    silent: bool = True,
    context: str | None = None,
    timeout: float | None = None,
) -> str:
    result = git_command_checked(
        repo_path,
        args,
        silent=silent,
        context=context,
        timeout=timeout,
    )
    return (result.stdout or "").strip()
