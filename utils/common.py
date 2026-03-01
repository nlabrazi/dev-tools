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
    ["gh"], # GitHub CLI actions should not run in dry-run
]

_DRY_RUN_BLOCKED_RC = 99


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

    silent=True means: don't print anything unless you choose to in caller.
    It does NOT mean "discard outputs".
    """
    # Normalize to list[str]
    if isinstance(command, str):
        command_list = command.split()
    else:
        command_list = command

    cmd_str = " ".join(command_list)

    # DRY-RUN handling
    if DRY_RUN:
        # Allow read-only commands to execute for real
        if _is_safe_readonly(command_list) and not _is_blocked(command_list):
            if not silent:
                print(f"🧪 [DRY-RUN/READ] Executing: {cmd_str} in {cwd}")
            return subprocess.run(
                command_list,
                cwd=cwd,
                capture_output=True,
                text=text,
            )

        # Block mutating commands: DO NOT pretend success
        if not silent:
            print(f"🌐 [DRY-RUN] Blocked (would execute): {cmd_str} in {cwd}")
        return subprocess.CompletedProcess(
            args=command_list,
            returncode=_DRY_RUN_BLOCKED_RC,
            stdout="",
            stderr="DRY_RUN: blocked mutating command",
        )

    # Normal execution: ALWAYS capture output so callers can debug on failure
    result = subprocess.run(
        command_list,
        cwd=cwd,
        capture_output=True,
        text=text,
    )

    # If silent, we simply do not print. Caller can decide.
    return result
