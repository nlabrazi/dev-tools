import os
import re
from collections import Counter
from datetime import datetime

from rich.console import Console

from core.formatters import build_conventional_commit, safe_parse_json
from core.ollama import OllamaError, chat_json
from core.prompts import COMMIT_SYSTEM, COMMIT_USER_TEMPLATE
from utils.common import env_int, trim_text_middle

console = Console()

COMMIT_HEADER_RE = re.compile(
    r"^(feat|fix|refactor|docs|test|chore|perf|ci|build|style|ui)(\([^)]+\))?(!)?: .+$",
    re.IGNORECASE,
)


def extract_plain_commit(raw: str) -> str | None:
    """
    Best-effort extraction when model output is not valid JSON.
    Keeps a proper Conventional Commit header and optional bullet body.
    """
    if not raw:
        return None

    cleaned: list[str] = []
    for line in raw.replace("\r\n", "\n").splitlines():
        s = line.rstrip()
        if s.strip().startswith("```"):
            continue
        cleaned.append(s)

    non_empty = [line.strip() for line in cleaned if line.strip()]
    if not non_empty:
        return None

    header_idx = -1
    header = ""
    for idx, line in enumerate(non_empty):
        candidate = line
        if line.lower().startswith("commit:"):
            candidate = line.split(":", 1)[1].strip()
        if COMMIT_HEADER_RE.match(candidate):
            header_idx = idx
            header = candidate
            break

    if not header:
        return None

    body_lines: list[str] = []
    for line in non_empty[header_idx + 1 :]:
        if COMMIT_HEADER_RE.match(line):
            break
        text = line
        if text.lower().startswith("body:"):
            text = text.split(":", 1)[1].strip()
        text = text.strip()
        if not text:
            continue
        if not text.startswith("- "):
            text = f"- {text}"
        body_lines.append(text)
        if len(body_lines) >= 6:
            break

    if body_lines:
        return f"{header}\n\n" + "\n".join(body_lines)
    return header


def is_comment_line(line: str) -> bool:
    stripped = line.lstrip()
    return (
        stripped.startswith("#")
        or stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
    )


def detect_commit_type_from_diff(diff_content: str) -> str:
    if not diff_content:
        return "chore"

    diff_lines = diff_content.lower().splitlines()
    type_counter = Counter()

    for line in diff_lines:
        if not (line.startswith("+") or line.startswith("-")):
            continue

        if is_comment_line(line):
            if "fix" in line or "bug" in line or "error" in line or "typo" in line:
                type_counter["fix"] += 0.5
            if "refactor" in line:
                type_counter["refactor"] += 0.5
            continue

        if "fix" in line or "bug" in line or "error" in line or "typo" in line:
            type_counter["fix"] += 1
        if "function" in line or "def " in line or "class " in line:
            type_counter["feat"] += 2
        if "refactor" in line or ("remove" in line and len(line) > 30):
            type_counter["refactor"] += 1
        if ".md" in line or "documentation" in line:
            type_counter["docs"] += 1
        if ".css" in line or ".scss" in line or ".html" in line:
            type_counter["style"] += 1
        if ".json" in line or ".yml" in line or "config" in line or "build" in line:
            type_counter["chore"] += 1

    if not type_counter:
        return "chore"

    selected_type, _ = type_counter.most_common(1)[0]
    return selected_type


def build_fallback_commit_message(
    files: list[str],
    diff_content: str,
    *,
    now: datetime | None = None,
) -> str:
    commit_type = detect_commit_type_from_diff(diff_content)
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")

    if files:
        title_keywords = " and ".join(files[:2])
        return f"{commit_type}: update {title_keywords} ({timestamp})"

    return f"{commit_type}: auto commit based on diff analysis ({timestamp})"


def generate_commit_message_with_ollama(repo: str, files: list[str], diff_content: str) -> str | None:
    """
    Returns full commit message (header + body) or None if Ollama fails / bad JSON.
    """

    def parse_and_build(raw: str) -> str | None:
        data = safe_parse_json(raw)
        if not data:
            return None
        try:
            return build_conventional_commit(data)
        except Exception:
            return None

    try:
        max_files = env_int("OLLAMA_MAX_FILES", 80, minimum=1)
        files_for_prompt = files[:max_files]
        files_block = "\n".join(f"- {file_name}" for file_name in files_for_prompt) or "- (unknown)"
        if len(files) > max_files:
            files_block += f"\n- ... (+{len(files) - max_files} more)"

        max_diff_chars = env_int("OLLAMA_MAX_DIFF_CHARS", 4500, minimum=800)
        trimmed_diff = trim_text_middle(diff_content, max_diff_chars)

        user_prompt = COMMIT_USER_TEMPLATE.format(
            repo=repo,
            files=files_block,
            diff=trimmed_diff,
        )
        messages = [
            {"role": "system", "content": COMMIT_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        with console.status("[bold cyan]🤖 Generating commit message...[/]", spinner="dots"):
            raw = chat_json(
                messages,
                temperature=0.2,
                json_mode=True,
            )
        if os.getenv("OLLAMA_DEBUG", "0") == "1":
            print("\n[DEBUG] Raw Ollama output (attempt 1):\n", raw, "\n")

        built = parse_and_build(raw)
        if built:
            return built
        plain = extract_plain_commit(raw)
        if plain:
            print("⚠️ Ollama JSON invalid, using plain-text commit from model output.")
            return plain

        print("⚠️ Ollama output is not valid commit JSON, retrying once.")
        with console.status("[bold cyan]🤖 Retrying commit message generation...[/]", spinner="dots"):
            raw_retry = chat_json(
                messages,
                temperature=0.0,
                json_mode=True,
            )
        if os.getenv("OLLAMA_DEBUG", "0") == "1":
            print("\n[DEBUG] Raw Ollama output (attempt 2):\n", raw_retry, "\n")

        built_retry = parse_and_build(raw_retry)
        if built_retry:
            return built_retry
        plain_retry = extract_plain_commit(raw_retry)
        if plain_retry:
            print("⚠️ Ollama JSON invalid on retry, using plain-text commit from model output.")
            return plain_retry

        plain_system = (
            "Return ONLY a Conventional Commit message in plain text.\n"
            "First line format: type(scope optional): subject\n"
            "Allowed types: feat, fix, refactor, docs, test, chore, perf, ci, build, style.\n"
            "Optional body lines must be bullets prefixed by '- '."
        )
        with console.status("[bold cyan]🤖 Recovering commit message (plain mode)...[/]", spinner="dots"):
            raw_plain = chat_json(
                [
                    {"role": "system", "content": plain_system},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                json_mode=False,
            )
        if os.getenv("OLLAMA_DEBUG", "0") == "1":
            print("\n[DEBUG] Raw Ollama output (plain recovery):\n", raw_plain, "\n")

        plain_recovery = extract_plain_commit(raw_plain)
        if plain_recovery:
            print("⚠️ Ollama JSON invalid, plain recovery mode used.")
            return plain_recovery

        print("⚠️ Ollama returned unusable output, fallback used.")
        return None
    except OllamaError as error:
        print(f"⚠️ Ollama unavailable, fallback used. Reason: {error}")
        return None
    except Exception as error:
        print(f"⚠️ Ollama output invalid, fallback used. Reason: {error}")
        return None


def generate_commit_message(
    repo: str,
    files: list[str],
    diff_content: str,
    *,
    now: datetime | None = None,
) -> str:
    generated = generate_commit_message_with_ollama(repo, files, diff_content)
    if generated:
        return generated
    return build_fallback_commit_message(files, diff_content, now=now)
