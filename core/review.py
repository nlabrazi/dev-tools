from dataclasses import dataclass

from core.formatters import safe_parse_json
from core.ollama import (
    OllamaError,
    chat_json,
    debug_log_output,
    ensure_repo_context_allowed,
    is_ollama_enabled,
)
from core.prompts import CODE_COMMENT_SYSTEM, CODE_COMMENT_USER_TEMPLATE
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
MAX_REVIEW_COMMENTS = 5
COMMENTABLE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".lua",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sass",
    ".scss",
    ".sh",
    ".svelte",
    ".ts",
    ".tsx",
    ".vue",
}
IGNORED_REVIEW_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}
IGNORED_REVIEW_SUFFIXES = (
    ".avif",
    ".bin",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".lock",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
)
VALID_COMMENT_PLACEMENTS = {"before", "after"}


class ReviewContextError(RuntimeError):
    pass


class ReviewCommentFormatError(ValueError):
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


@dataclass(frozen=True)
class ReviewCommentSuggestion:
    file: str
    anchor: str
    placement: str
    comment: str
    reason: str


@dataclass(frozen=True)
class ReviewCommentPlan:
    summary: str
    comments: list[ReviewCommentSuggestion]

    @property
    def has_comments(self) -> bool:
        return bool(self.comments)


def get_review_diff_max_chars() -> int:
    return env_int("OLLAMA_MAX_REVIEW_DIFF_CHARS", DEFAULT_REVIEW_DIFF_MAX_CHARS, minimum=1000)


def _as_clean_string(value: object, *, max_chars: int | None = None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if max_chars is not None and len(cleaned) > max_chars:
        return cleaned[:max_chars].rstrip()
    return cleaned


def is_commentable_source_file(file_path: str) -> bool:
    normalized = file_path.strip().replace("\\", "/")
    if not normalized:
        return False

    name = normalized.rsplit("/", 1)[-1].lower()
    if name in IGNORED_REVIEW_FILENAMES:
        return False
    if any(name.endswith(suffix) for suffix in IGNORED_REVIEW_SUFFIXES):
        return False

    dot_index = name.rfind(".")
    if dot_index == -1:
        return False
    return name[dot_index:] in COMMENTABLE_EXTENSIONS


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


def _format_files_for_prompt(files: list[str]) -> str:
    return "\n".join(f"- {file_name}" for file_name in files) or "- (none)"


def _allowed_review_files(files: list[str]) -> set[str]:
    return {file_name for file_name in files if is_commentable_source_file(file_name)}


def build_review_comment_plan(
    data: dict,
    *,
    allowed_files: list[str] | None = None,
) -> ReviewCommentPlan:
    if not isinstance(data, dict) or "review" not in data or not isinstance(data["review"], dict):
        raise ReviewCommentFormatError("Invalid JSON: missing 'review' object")

    review = data["review"]
    summary = _as_clean_string(review.get("summary"), max_chars=240)
    raw_comments = review.get("comments")
    if not isinstance(raw_comments, list):
        raise ReviewCommentFormatError("Invalid review JSON: 'comments' must be an array")

    allowed = None if allowed_files is None else _allowed_review_files(allowed_files)
    comments: list[ReviewCommentSuggestion] = []
    for raw_comment in raw_comments:
        if not isinstance(raw_comment, dict):
            continue

        file_name = _as_clean_string(raw_comment.get("file"))
        if not file_name or not is_commentable_source_file(file_name):
            continue
        if allowed is not None and file_name not in allowed:
            continue

        anchor = _as_clean_string(raw_comment.get("anchor"), max_chars=500)
        comment = _as_clean_string(raw_comment.get("comment"), max_chars=1200)
        reason = _as_clean_string(raw_comment.get("reason"), max_chars=500)
        placement = _as_clean_string(raw_comment.get("placement")).lower()
        if placement not in VALID_COMMENT_PLACEMENTS:
            placement = "before"

        if not anchor or not comment:
            continue

        comments.append(
            ReviewCommentSuggestion(
                file=file_name,
                anchor=anchor,
                placement=placement,
                comment=comment,
                reason=reason,
            )
        )
        if len(comments) >= MAX_REVIEW_COMMENTS:
            break

    return ReviewCommentPlan(summary=summary, comments=comments)


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


def generate_review_comments_with_ollama(
    repo_name: str,
    context: ReviewContext,
) -> ReviewCommentPlan | None:
    if not is_ollama_enabled():
        return None
    if not context.has_changes:
        return ReviewCommentPlan(
            summary=f"No changes found for {context.label}.",
            comments=[],
        )

    try:
        ensure_repo_context_allowed("git review context")

        user_prompt = CODE_COMMENT_USER_TEMPLATE.format(
            repo=repo_name,
            target_label=context.label,
            files=_format_files_for_prompt(context.files),
            diff=context.diff,
        )
        messages = [
            {"role": "system", "content": CODE_COMMENT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        raw = chat_json(messages, temperature=0.2, json_mode=True)
        debug_log_output("Code review comments output (attempt 1)", raw)

        data = safe_parse_json(raw)
        if data:
            try:
                return build_review_comment_plan(data, allowed_files=context.files)
            except ReviewCommentFormatError:
                pass

        print("⚠️ Ollama review output invalid, retrying once.")
        raw_retry = chat_json(messages, temperature=0.0, json_mode=True)
        debug_log_output("Code review comments output (attempt 2)", raw_retry)

        retry_data = safe_parse_json(raw_retry)
        if retry_data:
            return build_review_comment_plan(retry_data, allowed_files=context.files)

        print("⚠️ Ollama review output unusable, no comments generated.")
        return None
    except OllamaError as error:
        print(f"⚠️ Ollama unavailable for code review, no comments generated. Reason: {error}")
        return None
    except Exception as error:
        print(f"⚠️ Ollama review output invalid, no comments generated. Reason: {error}")
        return None


def generate_review_comments(repo_name: str, context: ReviewContext) -> ReviewCommentPlan:
    generated = generate_review_comments_with_ollama(repo_name, context)
    if generated:
        return generated
    return ReviewCommentPlan(
        summary="No AI code comments generated.",
        comments=[],
    )
