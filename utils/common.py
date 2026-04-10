# utils/common.py

import os
from pathlib import Path
import subprocess
from typing import List, Optional, Union

DRY_RUN = False
_TIMEOUT_RESULT_RC = 124
_TIMEOUT_MARKER = "COMMAND_TIMEOUT:"

# Commands that are safe to execute even in dry-run (read-only)
_SAFE_PREFIXES: list[list[str]] = [
    ["git", "status"],
    ["git", "diff"],
    ["git", "fetch"],
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
    ["gh", "pr", "list"],
    ["gh", "pr", "view"],
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
    ["gh", "pr", "create"],
    ["gh", "pr", "merge"],
]

_DRY_RUN_BLOCKED_RC = 99


class DryRunBlockedError(RuntimeError):
    def __init__(self, action: str, command: Union[List[str], str]) -> None:
        if isinstance(command, str):
            command_label = command
        else:
            command_label = " ".join(command)
        self.action = action
        self.command = command_label
        super().__init__(f"{action} skipped in dry-run: would execute `{command_label}`")


class CommandTimedOutError(RuntimeError):
    def __init__(self, action: str, command: Union[List[str], str], details: str) -> None:
        if isinstance(command, str):
            command_label = command
        else:
            command_label = " ".join(command)
        self.action = action
        self.command = command_label
        self.details = details
        super().__init__(f"{action} timed out: {details}")


def set_dry_run(state: bool = True) -> None:
    global DRY_RUN
    DRY_RUN = state


def is_dry_run() -> bool:
    return DRY_RUN


def is_dry_run_result(result: subprocess.CompletedProcess) -> bool:
    return result.returncode == _DRY_RUN_BLOCKED_RC


def is_timeout_result(result: subprocess.CompletedProcess) -> bool:
    stderr = result.stderr or ""
    if result.returncode != _TIMEOUT_RESULT_RC:
        return False
    if isinstance(stderr, bytes):
        return stderr.startswith(bytes(_TIMEOUT_MARKER, encoding="utf-8"))
    return isinstance(stderr, str) and stderr.startswith(_TIMEOUT_MARKER)


def _is_prefix(command: list[str], prefix: list[str]) -> bool:
    if len(command) < len(prefix):
        return False
    return command[: len(prefix)] == prefix


def _is_safe_readonly(command: list[str]) -> bool:
    return any(_is_prefix(command, p) for p in _SAFE_PREFIXES)


def _is_blocked(command: list[str]) -> bool:
    return any(_is_prefix(command, p) for p in _BLOCK_PREFIXES)


def _normalize_output(value: object, text: bool) -> str | bytes:
    if value is None:
        return "" if text else b""
    if text:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    if isinstance(value, bytes):
        return value
    return bytes(str(value), encoding="utf-8", errors="replace")


def _timeout_result(
    command_list: list[str],
    error: subprocess.TimeoutExpired,
    text: bool,
    timeout: float | None,
) -> subprocess.CompletedProcess:
    timeout_label = timeout if timeout is not None else "unknown"
    stdout = _normalize_output(error.stdout, text)
    stderr = _normalize_output(error.stderr, text)
    details = f"{_TIMEOUT_MARKER} command timed out after {timeout_label}s"
    if text:
        stderr_text = str(stderr)
        stderr_value = details if not stderr_text else f"{details}\n{stderr_text}"
        return subprocess.CompletedProcess(
            args=command_list,
            returncode=_TIMEOUT_RESULT_RC,
            stdout=str(stdout),
            stderr=stderr_value,
        )

    stderr_bytes = stderr if isinstance(stderr, bytes) else bytes(str(stderr), encoding="utf-8", errors="replace")
    details_bytes = bytes(details, encoding="utf-8", errors="replace")
    stderr_value = details_bytes if not stderr_bytes else details_bytes + b"\n" + stderr_bytes
    return subprocess.CompletedProcess(
        args=command_list,
        returncode=_TIMEOUT_RESULT_RC,
        stdout=stdout if isinstance(stdout, bytes) else bytes(str(stdout), encoding="utf-8", errors="replace"),
        stderr=stderr_value,
    )


def run_command(
    command: Union[List[str], str],
    cwd: Optional[str] = None,
    silent: bool = False,
    text: bool = True,
    timeout: float | None = None,
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
            try:
                return subprocess.run(
                    command_list,
                    cwd=cwd,
                    capture_output=True,
                    text=text,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                return _timeout_result(command_list, error, text=text, timeout=timeout)

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
    try:
        result = subprocess.run(
            command_list,
            cwd=cwd,
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        return _timeout_result(command_list, error, text=text, timeout=timeout)

    # If silent, we simply do not print. Caller can decide.
    return result


def run_command_checked(
    command: Union[List[str], str],
    cwd: Optional[str] = None,
    silent: bool = False,
    text: bool = True,
    context: Optional[str] = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    result = run_command(command, cwd=cwd, silent=silent, text=text, timeout=timeout)
    if result.returncode == 0:
        return result

    if is_dry_run_result(result):
        if isinstance(command, str):
            command_label = command
        else:
            command_label = " ".join(command)
        action = context or command_label
        raise DryRunBlockedError(action, command)

    if is_timeout_result(result):
        if isinstance(command, str):
            command_label = command
        else:
            command_label = " ".join(command)
        action = context or command_label
        raw_stderr = result.stderr or ""
        if isinstance(raw_stderr, bytes):
            stderr_text = raw_stderr.decode("utf-8", errors="replace")
        else:
            stderr_text = raw_stderr
        details = stderr_text.replace(_TIMEOUT_MARKER, "", 1).strip() or "command timed out"
        raise CommandTimedOutError(action, command, details)

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
