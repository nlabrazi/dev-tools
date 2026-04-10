from datetime import datetime

from core.config import DEFAULT_HEAD_BRANCH
from core.formatters import build_pr, safe_parse_json
from core.ollama import (
    OllamaError,
    chat_json,
    debug_log_output,
    ensure_repo_context_allowed,
    is_ollama_enabled,
)
from core.prompts import PR_SYSTEM, PR_USER_TEMPLATE
from utils.common import env_int, trim_text_middle


def extract_plain_pr(raw: str) -> tuple[str | None, str | None]:
    """
    Best-effort extraction when model output is not valid JSON.
    Expects lines with "TITLE:" and markdown sections.
    """
    if not raw:
        return None, None

    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").splitlines():
        stripped = line.rstrip()
        if stripped.strip().startswith("```"):
            continue
        lines.append(stripped)

    title = ""
    title_idx = -1
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("title:"):
            title = stripped.split(":", 1)[1].strip()
            title_idx = idx
            break
        if stripped.startswith("#") and not stripped.startswith("## "):
            title = stripped.lstrip("#").strip()
            title_idx = idx
            break
        title = stripped
        title_idx = idx
        break

    if not title:
        return None, None
    title = title[:80].rstrip()
    if not title:
        return None, None

    body = "\n".join(lines[title_idx + 1 :]).strip()
    required_sections = ("## What", "## Why", "## Testing", "## Notes")
    if not body or any(section not in body for section in required_sections):
        base_body = body or "- Summary not provided."
        body = (
            f"## What\n{base_body}\n\n"
            "## Why\n- N/A\n\n"
            "## Testing\n- Not specified in commit summary.\n\n"
            "## Notes\n- N/A"
        )

    return title, body


def build_fallback_pr_text(
    commit_summary: str,
    base_branch: str,
    *,
    head_branch: str = DEFAULT_HEAD_BRANCH,
    now: datetime | None = None,
) -> tuple[str, str]:
    date_str = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    title = f"🔀 chore: merge {head_branch} into {base_branch} ({date_str})"
    body = f"""## 📦 Merge Summary

This pull request merges the latest validated commits from `{head_branch}` into `{base_branch}`.

---

**✨ Commits included:**

{commit_summary}

---

_Auto-generated on {date_str}_
"""
    return title, body


def generate_pr_text_with_ollama(
    repo_name: str,
    commit_summary: str,
    base_branch: str,
    *,
    head_branch: str = DEFAULT_HEAD_BRANCH,
) -> tuple[str | None, str | None]:
    """
    Returns (title, body) if success, else (None, None).
    """
    if not is_ollama_enabled():
        return None, None

    def parse_and_build(raw: str) -> tuple[str | None, str | None]:
        data = safe_parse_json(raw)
        if not data:
            return None, None
        try:
            return build_pr(data)
        except Exception:
            return None, None

    try:
        ensure_repo_context_allowed("git commit summary")

        max_summary_chars = env_int("OLLAMA_MAX_PR_SUMMARY_CHARS", 5000, minimum=1200)
        commit_summary_trimmed = trim_text_middle(commit_summary.strip(), max_summary_chars)

        pr_user = PR_USER_TEMPLATE.format(
            repo=repo_name,
            base=base_branch,
            head=head_branch,
            commit_summary=commit_summary_trimmed,
        )
        messages = [
            {"role": "system", "content": PR_SYSTEM},
            {"role": "user", "content": pr_user},
        ]

        raw = chat_json(messages, temperature=0.2, json_mode=True)
        debug_log_output("PR generation output (attempt 1)", raw)

        title, body = parse_and_build(raw)
        if title and body:
            return title, body

        plain_title, plain_body = extract_plain_pr(raw)
        if plain_title and plain_body:
            print("⚠️ Ollama PR JSON invalid, using plain-text PR from model output.")
            return plain_title, plain_body

        print("⚠️ Ollama PR output invalid, retrying once.")
        raw_retry = chat_json(messages, temperature=0.0, json_mode=True)
        debug_log_output("PR generation output (attempt 2)", raw_retry)

        retry_title, retry_body = parse_and_build(raw_retry)
        if retry_title and retry_body:
            return retry_title, retry_body

        plain_retry_title, plain_retry_body = extract_plain_pr(raw_retry)
        if plain_retry_title and plain_retry_body:
            print("⚠️ Ollama PR JSON invalid on retry, using plain-text PR from model output.")
            return plain_retry_title, plain_retry_body

        plain_system = (
            "Return plain text only with this exact structure:\n"
            "TITLE: <max 80 chars>\n"
            "## What\n"
            "...\n"
            "## Why\n"
            "...\n"
            "## Testing\n"
            "...\n"
            "## Notes\n"
            "...\n"
            "Do not invent tests; if unknown write that testing evidence is not provided."
        )
        raw_plain = chat_json(
            [
                {"role": "system", "content": plain_system},
                {"role": "user", "content": pr_user},
            ],
            temperature=0.1,
            json_mode=False,
        )
        debug_log_output("PR generation output (plain recovery)", raw_plain)

        plain_recovery_title, plain_recovery_body = extract_plain_pr(raw_plain)
        if plain_recovery_title and plain_recovery_body:
            print("⚠️ Ollama PR JSON invalid, plain recovery mode used.")
            return plain_recovery_title, plain_recovery_body

        print("⚠️ Ollama PR output unusable, fallback used.")
        return None, None
    except OllamaError as error:
        print(f"⚠️ Ollama unavailable for PR text, fallback used. Reason: {error}")
        return None, None
    except Exception as error:
        print(f"⚠️ Ollama PR output invalid, fallback used. Reason: {error}")
        return None, None


def generate_pr_text(
    repo_name: str,
    commit_summary: str,
    base_branch: str,
    *,
    head_branch: str = DEFAULT_HEAD_BRANCH,
    now: datetime | None = None,
) -> tuple[str, str]:
    title, body = generate_pr_text_with_ollama(
        repo_name,
        commit_summary,
        base_branch,
        head_branch=head_branch,
    )
    if title and body:
        return title, body
    return build_fallback_pr_text(
        commit_summary,
        base_branch,
        head_branch=head_branch,
        now=now,
    )
