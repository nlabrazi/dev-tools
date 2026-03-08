# utils/common.py

import os
from pathlib import Path
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
    ["git", "show-ref"],
    ["git", "symbolic-ref"],
    ["git", "rev-list"],
    ["git", "ls-files"],
    ["git", "config"],
    ["git", "tag"],
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
    ["git", "tag", "-a"],
    ["git", "tag", "--annotate"],
    ["gh"], # GitHub CLI actions should not run in dry-run
]

_DRY_RUN_BLOCKED_RC = 99


def set_dry_run(state: bool = True) -> None:
    global DRY_RUN
    DRY_RUN = state


def is_dry_run() -> bool:
    return DRY_RUN


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


def run_command_checked(
    command: Union[List[str], str],
    cwd: Optional[str] = None,
    silent: bool = False,
    text: bool = True,
    context: Optional[str] = None,
) -> subprocess.CompletedProcess:
    result = run_command(command, cwd=cwd, silent=silent, text=text)
    if result.returncode == 0:
        return result

    details = ((result.stderr or "").strip() or (result.stdout or "").strip() or f"exit code {result.returncode}")
    if isinstance(command, str):
        command_label = command
    else:
        command_label = " ".join(command)
    action = context or command_label
    raise RuntimeError(f"{action} failed: {details}")


def env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def trim_text_middle(text: str, max_chars: int) -> str:
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    marker = "\n\n... [truncated for model context] ...\n\n"
    if max_chars <= len(marker) + 50:
        return text[:max_chars]

    head = int(max_chars * 0.7)
    tail = max_chars - head - len(marker)
    if tail < 0:
        tail = 0
    return text[:head] + marker + text[-tail:]


def prepend_text_file(path: str, prefix: str, encoding: str = "utf-8") -> bool:
    if DRY_RUN:
        print(f"🌐 [DRY-RUN] Blocked file write: prepend content to {path}")
        return False

    file_path = Path(path)
    existing = file_path.read_text(encoding=encoding) if file_path.exists() else ""
    if existing:
        separator = "" if prefix.endswith("\n") else "\n"
        new_content = f"{prefix}{separator}{existing}"
    else:
        new_content = prefix
    file_path.write_text(new_content, encoding=encoding)
    return True
