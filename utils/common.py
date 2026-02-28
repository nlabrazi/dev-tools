# utils/common.py

import subprocess
from typing import List, Optional, Union

DRY_RUN = False

# Commands that are safe to execute even in dry-run (read-only)
_SAFE_PREFIXES: list[list[str]] = [
    ["git", "status"],
    ["git", "diff"],
    ["git", "log"],
    ["git", "rev-parse"],
    ["git", "merge-base"],
    ["git", "branch"],
    ["git", "remote"],
    ["git", "show"],
    ["git", "ls-files"],
    ["git", "config"],
]

# Commands that mutate state (must be blocked in dry-run)
_BLOCK_PREFIXES: list[list[str]] = [
    ["git", "add"],
    ["git", "commit"],
    ["git", "push"],
    ["git", "merge"],
    ["git", "rebase"],
    ["git", "reset"],
    ["git", "checkout"],
    ["git", "switch"],
    ["git", "restore"],
    ["git", "tag"],
    ["gh"],  # GitHub CLI actions should not run in dry-run
]


def set_dry_run(state: bool = True) -> None:
    global DRY_RUN
    DRY_RUN = state


def _is_prefix(command: list[str], prefix: list[str]) -> bool:
    if len(command) < len(prefix):
        return False
    return command[: len(prefix)] == prefix


def _is_safe_readonly(command: list[str]) -> bool:
    return any(_is_prefix(command, p) for p in _SAFE_PREFIXES)


def _is_blocked(command: list[str]) -> bool:
    return any(_is_prefix(command, p) for p in _BLOCK_PREFIXES)


def run_command(
    command: Union[List[str], str],
    cwd: Optional[str] = None,
    silent: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """
    Execute a command and return a CompletedProcess with stdout/stderr always available.

    DRY_RUN behavior:
    - Read-only commands (git status/diff/log/...) are executed for real so detection works.
    - Mutating commands (git add/commit/push, gh ...) are blocked and only printed.
    """
    # Normalize to list[str]
    if isinstance(command, str):
        command_list = command.split()
    else:
        command_list = command

    # DRY-RUN handling
    if DRY_RUN:
        cmd_str = " ".join(command_list)

        # If command is read-only, execute it for real (so status/diff detection works)
        if _is_safe_readonly(command_list) and not _is_blocked(command_list):
            if not silent:
                print(f"🧪 [DRY-RUN/READ] Executing (read-only): {cmd_str} in {cwd}")
            return subprocess.run(
                command_list,
                cwd=cwd,
                capture_output=True,
                text=text,
            )

        # Otherwise, block execution
        print(f"🌐 [DRY-RUN] Would execute: {cmd_str} in {cwd}")
        return subprocess.CompletedProcess(
            args=command_list,
            returncode=0,
            stdout="",
            stderr="",
        )

    # Normal execution
    if silent:
        result = subprocess.run(
            command_list,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=text,
        )
        # Ensure attrs exist
        if not hasattr(result, "stdout") or result.stdout is None:
            result.stdout = ""
        if not hasattr(result, "stderr") or result.stderr is None:
            result.stderr = ""
        return result

    return subprocess.run(
        command_list,
        cwd=cwd,
        capture_output=True,
        text=text,
    )
