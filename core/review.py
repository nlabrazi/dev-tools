import textwrap
from dataclasses import dataclass
from pathlib import Path

from core.formatters import safe_parse_json
from core.ollama import (
    OllamaError,
    chat_json,
    debug_log_output,
    ensure_repo_context_allowed,
    is_ollama_enabled,
)
from core.prompts import (
    CODE_COMMENT_SYSTEM,
    CODE_COMMENT_USER_TEMPLATE,
    CODE_REVIEW_SYSTEM,
    CODE_REVIEW_USER_TEMPLATE,
)
from utils.common import describe_command_failure, env_int, is_dry_run, trim_text_middle
from utils.git import git_command

REVIEW_TARGET_WORKTREE = "worktree"
REVIEW_TARGET_STAGED = "staged"
REVIEW_TARGET_BRANCH = "branch"
REVIEW_TARGET_COMMIT = "commit"
REVIEW_TARGET_FILE = "file"
REVIEW_TARGETS = {
    REVIEW_TARGET_WORKTREE,
    REVIEW_TARGET_STAGED,
    REVIEW_TARGET_BRANCH,
    REVIEW_TARGET_COMMIT,
    REVIEW_TARGET_FILE,
}

DEFAULT_REVIEW_DIFF_MAX_CHARS = 12000
DEFAULT_REVIEW_FILE_MAX_CHARS = 14000
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
REVIEWABLE_TEXT_EXTENSIONS = COMMENTABLE_EXTENSIONS | {
    ".ini",
    ".json",
    ".md",
    ".mdx",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
VALID_COMMENT_PLACEMENTS = {"before", "after"}
COMMENT_APPLICATION_APPLIED = "applied"
COMMENT_APPLICATION_DRY_RUN = "dry-run"
COMMENT_APPLICATION_SKIPPED = "skipped"
COMMENT_APPLICATION_FAILED = "failed"


class ReviewContextError(RuntimeError):
    pass


class ReviewCommentFormatError(ValueError):
    pass


class CodeReviewFormatError(ValueError):
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


@dataclass(frozen=True)
class CodeReviewExplanation:
    title: str
    overview: str
    technical_context: str
    important_files: list[str]
    behavior: list[str]
    points_to_check: list[str]
    risks: list[str]

    @property
    def has_content(self) -> bool:
        return any(
            [
                self.title,
                self.overview,
                self.technical_context,
                self.important_files,
                self.behavior,
                self.points_to_check,
                self.risks,
            ]
        )


@dataclass(frozen=True)
class ReviewCommentApplication:
    suggestion: ReviewCommentSuggestion
    status: str
    message: str


@dataclass(frozen=True)
class ReviewCommentApplicationReport:
    results: list[ReviewCommentApplication]

    @property
    def modified_files(self) -> list[str]:
        return sorted(
            {
                result.suggestion.file
                for result in self.results
                if result.status == COMMENT_APPLICATION_APPLIED
            }
        )

    @property
    def simulated_files(self) -> list[str]:
        return sorted(
            {
                result.suggestion.file
                for result in self.results
                if result.status == COMMENT_APPLICATION_DRY_RUN
            }
        )


def get_review_diff_max_chars() -> int:
    return env_int("OLLAMA_MAX_REVIEW_DIFF_CHARS", DEFAULT_REVIEW_DIFF_MAX_CHARS, minimum=1000)


def get_review_file_max_chars() -> int:
    return env_int("OLLAMA_MAX_REVIEW_FILE_CHARS", DEFAULT_REVIEW_FILE_MAX_CHARS, minimum=1000)


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


def is_reviewable_text_file(file_path: str) -> bool:
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
    return name[dot_index:] in REVIEWABLE_TEXT_EXTENSIONS


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


def _as_string_list(value: object, *, max_items: int = 8, max_chars: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []

    items: list[str] = []
    for raw_item in value:
        item = _as_clean_string(raw_item, max_chars=max_chars)
        if not item:
            continue
        items.append(item)
        if len(items) >= max_items:
            break
    return items


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


def build_code_review_explanation(data: dict) -> CodeReviewExplanation:
    if not isinstance(data, dict) or "review" not in data or not isinstance(data["review"], dict):
        raise CodeReviewFormatError("Invalid JSON: missing 'review' object")

    review = data["review"]
    return CodeReviewExplanation(
        title=_as_clean_string(review.get("title"), max_chars=120),
        overview=_as_clean_string(review.get("overview"), max_chars=1600),
        technical_context=_as_clean_string(review.get("technical_context"), max_chars=1600),
        important_files=_as_string_list(review.get("important_files")),
        behavior=_as_string_list(review.get("behavior")),
        points_to_check=_as_string_list(review.get("points_to_check")),
        risks=_as_string_list(review.get("risks")),
    )


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


def _clean_relative_file_path(raw_path: str | None) -> str:
    cleaned = (raw_path or "").strip().replace("\\", "/")
    if not cleaned:
        raise ReviewContextError("A relative file path is required for review target 'file'.")
    if cleaned.startswith("/") or cleaned == ".":
        raise ReviewContextError("File review requires a relative path inside the repository.")
    parts = [part for part in cleaned.split("/") if part]
    if any(part == ".." for part in parts):
        raise ReviewContextError("File review path cannot escape the repository.")
    return "/".join(parts)


def _read_review_file(repo_path: str, relative_file_path: str) -> str:
    if not is_reviewable_text_file(relative_file_path):
        raise ReviewContextError(f"Refusing to review unsupported or sensitive file '{relative_file_path}'.")

    repo_root = Path(repo_path).resolve()
    file_path = (repo_root / relative_file_path).resolve()
    try:
        file_path.relative_to(repo_root)
    except ValueError as error:
        raise ReviewContextError("File review path cannot escape the repository.") from error

    if not file_path.is_file():
        raise ReviewContextError(f"File not found: {relative_file_path}")

    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ReviewContextError(f"File is not valid UTF-8 text: {relative_file_path}") from error


def _resolve_comment_file(repo_path: str, relative_file_path: str, allowed_files: set[str]) -> tuple[str, Path]:
    normalized = _clean_relative_file_path(relative_file_path)
    if normalized not in allowed_files:
        raise ReviewContextError(f"File is outside the selected review context: {normalized}")
    if not is_commentable_source_file(normalized):
        raise ReviewContextError(f"Refusing to modify unsupported or sensitive file '{normalized}'.")

    repo_root = Path(repo_path).resolve()
    file_path = (repo_root / normalized).resolve()
    try:
        file_path.relative_to(repo_root)
    except ValueError as error:
        raise ReviewContextError("Comment file path cannot escape the repository.") from error

    if not file_path.is_file():
        raise ReviewContextError(f"File not found: {normalized}")
    return normalized, file_path


def _read_source_file(file_path: Path) -> str:
    try:
        with file_path.open("r", encoding="utf-8", newline="") as source_file:
            return source_file.read()
    except UnicodeDecodeError as error:
        raise ReviewContextError(f"File is not valid UTF-8 text: {file_path.name}") from error


def _write_source_file(file_path: Path, content: str) -> None:
    with file_path.open("w", encoding="utf-8", newline="") as source_file:
        source_file.write(content)


def _line_indentation(content: str, position: int) -> str:
    line_start = content.rfind("\n", 0, position) + 1
    line_end = content.find("\n", line_start)
    if line_end == -1:
        line_end = len(content)
    line = content[line_start:line_end].lstrip("\r")
    return line[: len(line) - len(line.lstrip(" \t"))]


def _next_line_indentation(content: str, position: int, fallback: str) -> str:
    line_end = content.find("\n", position)
    if line_end == -1 or line_end + 1 >= len(content):
        return fallback

    next_start = line_end + 1
    next_end = content.find("\n", next_start)
    if next_end == -1:
        next_end = len(content)
    next_line = content[next_start:next_end].lstrip("\r")
    if not next_line.strip():
        return fallback

    indentation = next_line[: len(next_line) - len(next_line.lstrip(" \t"))]
    return indentation if len(indentation) > len(fallback) else fallback


def _format_comment(comment: str, indentation: str, newline: str) -> str:
    dedented = textwrap.dedent(comment).strip()
    lines = dedented.splitlines()
    return newline.join(f"{indentation}{line.rstrip()}" if line.strip() else "" for line in lines)


def _insert_comment(content: str, suggestion: ReviewCommentSuggestion) -> tuple[str | None, str]:
    anchor = suggestion.anchor.strip()
    comment = suggestion.comment.strip()
    if not anchor or not comment:
        return None, "Anchor or comment is empty."
    if comment in content:
        return None, "Comment already exists in the file."

    occurrence_count = content.count(anchor)
    if occurrence_count == 0:
        return None, "Anchor was not found."
    if occurrence_count > 1:
        return None, f"Anchor is ambiguous ({occurrence_count} matches)."

    anchor_start = content.find(anchor)
    anchor_end = anchor_start + len(anchor)
    newline = "\r\n" if "\r\n" in content else "\n"
    indentation = _line_indentation(content, anchor_start)

    if suggestion.placement == "after":
        indentation = _next_line_indentation(content, anchor_end, indentation)
        line_end = content.find("\n", anchor_end)
        if line_end == -1:
            formatted = _format_comment(comment, indentation, newline)
            separator = "" if content.endswith(("\n", "\r")) else newline
            return f"{content}{separator}{formatted}", "Comment inserted after the anchor."

        insert_at = line_end + 1
        formatted = _format_comment(comment, indentation, newline)
        return (
            f"{content[:insert_at]}{formatted}{newline}{content[insert_at:]}",
            "Comment inserted after the anchor.",
        )

    line_start = content.rfind("\n", 0, anchor_start) + 1
    formatted = _format_comment(comment, indentation, newline)
    return (
        f"{content[:line_start]}{formatted}{newline}{content[line_start:]}",
        "Comment inserted before the anchor.",
    )


def apply_review_comments(
    repo_path: str,
    context: ReviewContext,
    plan: ReviewCommentPlan,
) -> ReviewCommentApplicationReport:
    allowed_files = {
        _clean_relative_file_path(file_name)
        for file_name in context.files
        if is_commentable_source_file(file_name)
    }
    cached_contents: dict[Path, str] = {}
    results: list[ReviewCommentApplication] = []

    for suggestion in plan.comments:
        try:
            normalized, file_path = _resolve_comment_file(repo_path, suggestion.file, allowed_files)
            content = cached_contents.get(file_path)
            if content is None:
                content = _read_source_file(file_path)

            updated_content, message = _insert_comment(content, suggestion)
            if updated_content is None:
                results.append(
                    ReviewCommentApplication(
                        suggestion=suggestion,
                        status=COMMENT_APPLICATION_SKIPPED,
                        message=message,
                    )
                )
                continue

            cached_contents[file_path] = updated_content
            if is_dry_run():
                results.append(
                    ReviewCommentApplication(
                        suggestion=suggestion,
                        status=COMMENT_APPLICATION_DRY_RUN,
                        message=f"Would update {normalized}. {message}",
                    )
                )
                continue

            _write_source_file(file_path, updated_content)
            results.append(
                ReviewCommentApplication(
                    suggestion=suggestion,
                    status=COMMENT_APPLICATION_APPLIED,
                    message=message,
                )
            )
        except (OSError, ReviewContextError) as error:
            results.append(
                ReviewCommentApplication(
                    suggestion=suggestion,
                    status=COMMENT_APPLICATION_FAILED,
                    message=str(error),
                )
            )

    return ReviewCommentApplicationReport(results=results)


def _build_file_context(repo_path: str, relative_file_path: str) -> ReviewContext:
    file_content = trim_text_middle(_read_review_file(repo_path, relative_file_path), get_review_file_max_chars())
    file_diff = _git_output_or_raise(
        repo_path,
        ["diff", "--no-ext-diff", "--", relative_file_path],
        context=f"collect file diff for {relative_file_path}",
        max_output_chars=get_review_diff_max_chars(),
    )
    diff_block = file_diff or "(no current worktree diff for this file)"
    context_text = (
        f"File path: {relative_file_path}\n\n"
        f"Current file content:\n{file_content}\n\n"
        f"Current worktree diff for this file:\n{diff_block}"
    )
    return ReviewContext(
        repo_path=repo_path,
        target=REVIEW_TARGET_FILE,
        label=f"file {relative_file_path}",
        diff=context_text,
        files=[relative_file_path],
        ref=relative_file_path,
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

    if normalized_target == REVIEW_TARGET_FILE:
        return _build_file_context(repo_path, _clean_relative_file_path(ref))

    commit_ref = _clean_ref(ref, target=normalized_target)
    return _build_context(
        repo_path,
        target=normalized_target,
        label=f"commit {commit_ref}",
        diff_args=["show", "--stat", "--patch", "--no-ext-diff", commit_ref],
        files_args=["show", "--name-only", "--format=", commit_ref],
        ref=commit_ref,
    )


def generate_code_review_with_ollama(
    repo_name: str,
    context: ReviewContext,
) -> CodeReviewExplanation | None:
    if not is_ollama_enabled():
        return None
    if not context.has_changes:
        return CodeReviewExplanation(
            title="Aucun changement détecté",
            overview=f"Aucun changement exploitable n'a été trouvé pour {context.label}.",
            technical_context="",
            important_files=[],
            behavior=[],
            points_to_check=[],
            risks=[],
        )

    try:
        ensure_repo_context_allowed("git review context")

        user_prompt = CODE_REVIEW_USER_TEMPLATE.format(
            repo=repo_name,
            target_label=context.label,
            files=_format_files_for_prompt(context.files),
            diff=context.diff,
        )
        messages = [
            {"role": "system", "content": CODE_REVIEW_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        raw = chat_json(messages, temperature=0.2, json_mode=True)
        debug_log_output("Code review explanation output (attempt 1)", raw)

        data = safe_parse_json(raw)
        if data:
            try:
                return build_code_review_explanation(data)
            except CodeReviewFormatError:
                pass

        print("⚠️ Ollama review explanation invalid, retrying once.")
        raw_retry = chat_json(messages, temperature=0.0, json_mode=True)
        debug_log_output("Code review explanation output (attempt 2)", raw_retry)

        retry_data = safe_parse_json(raw_retry)
        if retry_data:
            return build_code_review_explanation(retry_data)

        print("⚠️ Ollama review explanation unusable, no review generated.")
        return None
    except OllamaError as error:
        print(f"⚠️ Ollama unavailable for code review, no review generated. Reason: {error}")
        return None
    except Exception as error:
        print(f"⚠️ Ollama review explanation invalid, no review generated. Reason: {error}")
        return None


def generate_code_review(repo_name: str, context: ReviewContext) -> CodeReviewExplanation:
    generated = generate_code_review_with_ollama(repo_name, context)
    if generated:
        return generated
    return CodeReviewExplanation(
        title="Review indisponible",
        overview="Aucune explication IA n'a pu être générée pour ce contexte.",
        technical_context="",
        important_files=[],
        behavior=[],
        points_to_check=[],
        risks=[],
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
