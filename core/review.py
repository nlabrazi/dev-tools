from dataclasses import dataclass

from utils.common import describe_command_failure, env_int
from utils.git import git_command

REVIEW_TARGET_WORKTREE = "worktree"
REVIEW_TARGET_STAGED = "staged"
REVIEW_TARGET_BRANCH = "branch"
REVIEW_TARGET_COMMIT = "commit"
REVIEW_TARGETS = {
    REVIEW_TARGET_WORKTREE,
    REVIEW_TARGET_STAGED,
    REVIEW_TARGET_BRANCH,
    REVIEW_TARGET_COMMIT,
}

DEFAULT_REVIEW_DIFF_MAX_CHARS = 12000


class ReviewContextError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewContext:
    repo_path: str
    target: str
    label: str
    diff: str
    files: list[str]
    ref: str = ""

    @property
    def has_changes(self) -> bool:
        return bool(self.diff.strip())


def get_review_diff_max_chars() -> int:
    return env_int("OLLAMA_MAX_REVIEW_DIFF_CHARS", DEFAULT_REVIEW_DIFF_MAX_CHARS, minimum=1000)


def _clean_ref(ref: str | None, *, target: str) -> str:
    cleaned = (ref or "").strip()
    if not cleaned:
        raise ReviewContextError(f"A git ref is required for review target '{target}'.")
    return cleaned


def _split_files(raw_output: str) -> list[str]:
    files: list[str] = []
    seen: set[str] = set()
    for line in raw_output.splitlines():
        file_name = line.strip()
        if not file_name or file_name in seen:
            continue
        files.append(file_name)
        seen.add(file_name)
    return files


def _git_output_or_raise(
    repo_path: str,
    args: list[str],
    *,
    context: str,
    max_output_chars: int | None = None,
) -> str:
    result = git_command(
        repo_path,
        args,
        silent=True,
        max_output_chars=max_output_chars,
    )
    if result.returncode != 0:
        details = describe_command_failure(result)
        raise ReviewContextError(f"{context} failed: {details}")
    return (result.stdout or "").rstrip("\n")


def _build_context(
    repo_path: str,
    *,
    target: str,
    label: str,
    diff_args: list[str],
    files_args: list[str],
    ref: str = "",
) -> ReviewContext:
    diff = _git_output_or_raise(
        repo_path,
        diff_args,
        context=f"collect {label} diff",
        max_output_chars=get_review_diff_max_chars(),
    )
    files_output = _git_output_or_raise(
        repo_path,
        files_args,
        context=f"collect {label} file list",
    )

    return ReviewContext(
        repo_path=repo_path,
        target=target,
        label=label,
        diff=diff,
        files=_split_files(files_output),
        ref=ref,
    )


def collect_review_context(repo_path: str, target: str = REVIEW_TARGET_WORKTREE, ref: str | None = None) -> ReviewContext:
    normalized_target = (target or "").strip().lower()
    if normalized_target not in REVIEW_TARGETS:
        supported = ", ".join(sorted(REVIEW_TARGETS))
        raise ReviewContextError(f"Unsupported review target '{target}'. Supported targets: {supported}.")

    if normalized_target == REVIEW_TARGET_WORKTREE:
        return _build_context(
            repo_path,
            target=normalized_target,
            label="worktree changes",
            diff_args=["diff", "--no-ext-diff"],
            files_args=["diff", "--name-only"],
        )

    if normalized_target == REVIEW_TARGET_STAGED:
        return _build_context(
            repo_path,
            target=normalized_target,
            label="staged changes",
            diff_args=["diff", "--cached", "--no-ext-diff"],
            files_args=["diff", "--cached", "--name-only"],
        )

    if normalized_target == REVIEW_TARGET_BRANCH:
        branch = _clean_ref(ref, target=normalized_target)
        range_ref = f"{branch}...HEAD"
        return _build_context(
            repo_path,
            target=normalized_target,
            label=f"diff against {range_ref}",
            diff_args=["diff", "--no-ext-diff", range_ref],
            files_args=["diff", "--name-only", range_ref],
            ref=branch,
        )

    commit_ref = _clean_ref(ref, target=normalized_target)
    return _build_context(
        repo_path,
        target=normalized_target,
        label=f"commit {commit_ref}",
        diff_args=["show", "--stat", "--patch", "--no-ext-diff", commit_ref],
        files_args=["show", "--name-only", "--format=", commit_ref],
        ref=commit_ref,
    )
