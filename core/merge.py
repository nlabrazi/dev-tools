import os
import time
from datetime import datetime

from rich import print
from rich.console import Console

from utils.common import run_command
from utils.console import ask_yes_no
from core.ollama import chat_json, OllamaError
from core.prompts import PR_SYSTEM, PR_USER_TEMPLATE
from core.formatters import safe_parse_json, build_pr
from core.versioning import (
    compute_next_version,
    determine_bump_from_commits,
    create_and_push_tag,
    get_last_semver_tag,
)

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage"),
]

DEFAULT_BASE_BRANCH = "master"
DEFAULT_HEAD_BRANCH = "staging"

console = Console()


# ---------------- Git helpers ----------------

def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def run_git_command(path: str, args: list[str]) -> str:
    """
    Run git command in repo path and return stdout stripped.
    """
    result = run_command(["git"] + args, cwd=path)
    return (result.stdout or "").strip()


def get_current_branch(path: str) -> str:
    return run_git_command(path, ["branch", "--show-current"])


def ensure_clean_worktree(path: str) -> None:
    """
    Ensure no pending changes and no merge in progress (avoid undefined state).
    """
    status = run_git_command(path, ["status", "--porcelain"])
    if status.strip():
        raise RuntimeError("Working tree is not clean (uncommitted changes detected).")

    # Merge in progress?
    merge_head = os.path.join(path, ".git", "MERGE_HEAD")
    if os.path.exists(merge_head):
        raise RuntimeError("Merge in progress detected (.git/MERGE_HEAD exists). Resolve/abort it first.")


def checkout_update_master(repo_path: str) -> None:
    run_command(["git", "fetch", "--all", "--prune"], cwd=repo_path, silent=True)
    run_command(["git", "fetch", "--tags"], cwd=repo_path, silent=True)
    run_command(["git", "checkout", DEFAULT_BASE_BRANCH], cwd=repo_path)
    run_command(["git", "pull", "--ff-only"], cwd=repo_path)


def repo_has_diff_between_staging_and_master(path: str) -> bool:
    # Fetch remote refs
    run_command(["git", "fetch"], cwd=path, silent=True)

    base_commit = run_git_command(
        path,
        ["merge-base", f"origin/{DEFAULT_BASE_BRANCH}", f"origin/{DEFAULT_HEAD_BRANCH}"],
    )
    head_commit = run_git_command(path, ["rev-parse", f"origin/{DEFAULT_HEAD_BRANCH}"])

    return bool(base_commit) and bool(head_commit) and base_commit != head_commit


def get_commit_summary(path: str) -> str:
    return run_git_command(
        path,
        ["log", f"origin/{DEFAULT_BASE_BRANCH}..origin/{DEFAULT_HEAD_BRANCH}", "--pretty=format:- %s"],
    )


# ---------------- GitHub CLI helpers ----------------

def existing_pr_number(path: str) -> str:
    """
    Returns PR number if a PR already exists for base=head pair, else "".
    """
    result = run_command(
        [
            "gh",
            "pr",
            "list",
            "--base",
            DEFAULT_BASE_BRANCH,
            "--head",
            DEFAULT_HEAD_BRANCH,
            "--json",
            "number",
            "--jq",
            ".[0].number",
        ],
        cwd=path,
    )
    return (result.stdout or "").strip()


def get_pr_number_from_url(repo_path: str, pr_url: str) -> str:
    result = run_command(
        ["gh", "pr", "view", pr_url, "--json", "number", "--jq", ".number"],
        cwd=repo_path,
    )
    return (result.stdout or "").strip()


def merge_pr_with_retry(repo_path: str, repo_name: str, pr_number: str, max_attempts: int = 8) -> bool:
    """
    Enables auto-merge for a PR. Retries on transient GitHub states:
    - mergeability not computed yet
    - required checks not started/attached yet
    - temporary GraphQL issues
    Returns True if command succeeds, else False.
    """
    transient_markers = [
        "unstable",
        "not mergeable",
        "mergeable",
        "required checks",
        "checks",
        "queued",
        "merge queue",
        "GraphQL".lower(),
        "timed out",
        "try again",
        "must be up to date",  # can happen briefly right after PR creation / checks
    ]

    for attempt in range(1, max_attempts + 1):
        merge_result = run_command(
            ["gh", "pr", "merge", pr_number, "--merge", "--auto"],
            cwd=repo_path,
        )

        if merge_result.returncode == 0:
            return True

        combined = ((merge_result.stderr or "") + "\n" + (merge_result.stdout or "")).strip()
        lower = combined.lower()

        # Decide if we retry
        is_transient = any(marker in lower for marker in transient_markers)
        if not is_transient:
            print(f"❌ Failed to merge PR for {repo_name} (non-transient). Reason:\n{combined}")
            return False

        wait_s = min(2 * attempt, 12)  # 2s, 4s, 6s... capped
        print(f"⏳ PR not ready yet for {repo_name} (attempt {attempt}/{max_attempts}). Retrying in {wait_s}s...")
        time.sleep(wait_s)

    print(f"❌ Failed to merge PR for {repo_name} after {max_attempts} attempts (still transient).")
    return False


# ---------------- Versioning / tagging ----------------

def tag_release_interactive(repo_path: str, repo_name: str, commit_summary: str) -> None:
    """
    After merge, propose a semver tag.
    """
    last_tag = get_last_semver_tag(repo_path)
    auto_bump = determine_bump_from_commits(commit_summary)
    suggested = compute_next_version(repo_path, auto_bump, default_first="v0.1.0")

    print(f"\n🏷️  Versioning for [bold green]{repo_name}[/]")
    print(f"Last tag: {last_tag or '(none)'}")
    print(
        "Choose bump: "
        "[bold red]major[/] / "
        "[bold yellow]minor[/] / "
        "[bold green]patch[/]"
    )
    print(f"Auto suggestion: [bold cyan]{auto_bump}[/]")
    print(f"Suggested next tag: [bold magenta]{suggested}[/]")

    choice = input("👉 Choose bump (major/minor/patch) or press Enter to accept suggestion: ").strip().lower()
    bump = choice if choice in ("major", "minor", "patch") else auto_bump
    tag = compute_next_version(repo_path, bump, default_first="v0.1.0")

    confirm = ask_yes_no(f"Create and push tag {tag} ?", default="n")
    if not confirm:
        print("⏭️  Skipped tagging.")
        return

    create_and_push_tag(repo_path, tag, message=f"Release {tag}")
    print(f"✅ Tag created and pushed: {tag}")


def is_ollama_enabled() -> bool:
    return os.getenv("ENABLE_OLLAMA", "1") == "1"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
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


def extract_plain_pr(raw: str) -> tuple[str | None, str | None]:
    """
    Best-effort extraction when model output is not valid JSON.
    Expects lines with "TITLE:" and markdown sections.
    """
    if not raw:
        return None, None

    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").splitlines():
        s = line.rstrip()
        if s.strip().startswith("```"):
            continue
        lines.append(s)

    # Find title
    title = ""
    title_idx = -1
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith("title:"):
            title = s.split(":", 1)[1].strip()
            title_idx = idx
            break
        if s.startswith("#") and not s.startswith("## "):
            title = s.lstrip("#").strip()
            title_idx = idx
            break
        title = s
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


def generate_pr_text_with_ollama(repo_name: str, commit_summary: str) -> tuple[str | None, str | None]:
    """
    Returns (title, body) if success, else (None, None).
    """
    if not is_ollama_enabled():
        return None, None

    max_summary_chars = _env_int("OLLAMA_MAX_PR_SUMMARY_CHARS", 5000, minimum=1200)
    commit_summary_trimmed = trim_text_middle(commit_summary.strip(), max_summary_chars)

    pr_user = PR_USER_TEMPLATE.format(
        repo=repo_name,
        base=DEFAULT_BASE_BRANCH,
        head=DEFAULT_HEAD_BRANCH,
        commit_summary=commit_summary_trimmed,
    )
    messages = [
        {"role": "system", "content": PR_SYSTEM},
        {"role": "user", "content": pr_user},
    ]

    raw = chat_json(messages, temperature=0.2, json_mode=True)
    if os.getenv("OLLAMA_DEBUG", "0") == "1":
        print("\n[DEBUG] Raw Ollama PR output (attempt 1):\n", raw, "\n")

    data = safe_parse_json(raw)
    if data:
        try:
            return build_pr(data)
        except Exception:
            pass
    plain_title, plain_body = extract_plain_pr(raw)
    if plain_title and plain_body:
        print("⚠️ Ollama PR JSON invalid, using plain-text PR from model output.")
        return plain_title, plain_body

    print("⚠️ Ollama PR output invalid, retrying once.")
    raw_retry = chat_json(messages, temperature=0.0, json_mode=True)
    if os.getenv("OLLAMA_DEBUG", "0") == "1":
        print("\n[DEBUG] Raw Ollama PR output (attempt 2):\n", raw_retry, "\n")

    data_retry = safe_parse_json(raw_retry)
    if data_retry:
        try:
            return build_pr(data_retry)
        except Exception:
            pass
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
    if os.getenv("OLLAMA_DEBUG", "0") == "1":
        print("\n[DEBUG] Raw Ollama PR output (plain recovery):\n", raw_plain, "\n")

    plain_recovery_title, plain_recovery_body = extract_plain_pr(raw_plain)
    if plain_recovery_title and plain_recovery_body:
        print("⚠️ Ollama PR JSON invalid, plain recovery mode used.")
        return plain_recovery_title, plain_recovery_body

    print("⚠️ Ollama PR output unusable, fallback used.")
    return None, None


# ---------------- Main PR flow ----------------

def create_and_merge_pr(path: str, repo_name: str) -> None:
    # Safety first
    try:
        ensure_clean_worktree(path)
    except Exception as e:
        print(f"❌ {repo_name}: {e}")
        return

    # Debug: show current branch (we *do not* depend on it anymore)
    current = get_current_branch(path)
    if current:
        print(f"🔎 Current branch (info only): [bold]{current}[/]")

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_summary = get_commit_summary(path)

    if not commit_summary:
        print("⚠️  No new commits found to merge.")
        return

    # Fallback PR text
    fallback_title = f"🔀 chore: merge staging into master ({date_str})"
    fallback_body = f"""## 📦 Merge Summary

This pull request merges the latest validated commits from `staging` into `master`.

---

**✨ Commits included:**

{commit_summary}

---

_Auto-generated on {date_str}_
"""

    # Try Ollama
    title, body = None, None
    try:
        title, body = generate_pr_text_with_ollama(repo_name, commit_summary)
    except OllamaError as e:
        print(f"⚠️  Ollama unavailable for PR text, fallback used. Reason: {e}")

    title = title or fallback_title
    body = body or fallback_body

    # Check existing PR
    pr_number = existing_pr_number(path)
    created_pr_url = None

    if pr_number:
        print(f"🔗 Existing Pull Request detected: #{pr_number}")
    else:
        print(f"\n📘 Repository: [bold orange]{repo_name}[/]")
        print(f"--- Pull Request Preview ---\nTitle: {title}\n\n{body}\n---\n")

        if not ask_yes_no("🚀 Do you want to create and auto-merge this PR?", default="n"):
            print("❌ Skipped.\n")
            return

        with console.status("[bold green]Creating pull request...", spinner="dots"):
            result = run_command(
                [
                    "gh", "pr", "create",
                    "--base", DEFAULT_BASE_BRANCH,
                    "--head", DEFAULT_HEAD_BRANCH,
                    "--title", title,
                    "--body", body,
                ],
                cwd=path,
            )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            print(f"❌ Failed to create Pull Request for {repo_name}. Reason:\n{stderr}")
            return

        # Extract URL from output
        for line in (result.stdout or "").strip().splitlines():
            if line.startswith("https://github.com/"):
                created_pr_url = line.strip()
                break

        if not created_pr_url:
            print("❌ Pull Request URL not found")
            return

        print(f"🔗 Pull Request created: {created_pr_url}")

        # Resolve PR number reliably
        pr_number = get_pr_number_from_url(path, created_pr_url)
        if not pr_number:
            print("❌ Could not resolve PR number from URL.")
            return

    # Merge the PR (existing or newly created) - ALWAYS target by PR number
    with console.status("[bold cyan]Merging pull request (auto)...", spinner="dots"):
        ok = merge_pr_with_retry(path, repo_name, pr_number)

    if not ok:
        return

    print(f"✅ PR merge/auto-merge successfully triggered for [bold green]{repo_name}[/]\n")

    # Refresh local master and tag the release
    try:
        checkout_update_master(path)
        tag_release_interactive(path, repo_name, commit_summary)
    except Exception as e:
        print(f"⚠️  Tagging step failed/skipped for {repo_name}: {e}")


def main() -> None:
    print("\n🔄 Scanning for repos with pending staging → master merges\n")

    for root_dir in ROOT_DIRS:
        console.print(f"\n📂 [bold yellow]Scanning root directory:[/] {root_dir}\n")

        if not os.path.isdir(root_dir):
            print(f"⚠️  Root directory not found: {root_dir}")
            continue

        for repo in os.listdir(root_dir):
            path = os.path.join(root_dir, repo)
            if not is_git_repo(path):
                continue

            if repo_has_diff_between_staging_and_master(path):
                print(f"📦 [bold green]Found pending merge for {repo}[/]")
                create_and_merge_pr(path, repo)
            else:
                print(f"✔️  [bold dark_orange]{repo}[/]: staging is up to date with master.")


if __name__ == "__main__":
    main()
