import os
import json
import urllib.request
import urllib.error
from datetime import datetime
from rich import print
from rich.console import Console

from utils.common import run_command

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

# --- Ollama config (optional) ---
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))
ENABLE_OLLAMA = os.getenv("ENABLE_OLLAMA", "1") == "1"

console = Console()


def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def run_git_command(path: str, args: list[str]) -> str:
    """
    Run git command in repo path and return stdout stripped.
    """
    result = run_command(["git"] + args, cwd=path)
    return (result.stdout or "").strip()


def repo_has_diff_between_staging_and_master(path: str) -> bool:
    # Fetch remote refs
    run_command(["git", "fetch"], cwd=path, silent=True)

    base_commit = run_git_command(
        path, ["merge-base", f"origin/{DEFAULT_BASE_BRANCH}", f"origin/{DEFAULT_HEAD_BRANCH}"]
    )
    head_commit = run_git_command(path, ["rev-parse", f"origin/{DEFAULT_HEAD_BRANCH}"])

    return bool(base_commit) and bool(head_commit) and base_commit != head_commit


def get_commit_summary(path: str) -> str:
    return run_git_command(
        path,
        ["log", f"origin/{DEFAULT_BASE_BRANCH}..origin/{DEFAULT_HEAD_BRANCH}", "--pretty=format:- %s"],
    )


def existing_pr_number(path: str) -> str:
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


def checkout_update_master(repo_path: str) -> None:
    run_command(["git", "fetch", "--all", "--prune"], cwd=repo_path, silent=True)
    run_command(["git", "fetch", "--tags"], cwd=repo_path, silent=True)
    run_command(["git", "checkout", DEFAULT_BASE_BRANCH], cwd=repo_path)
    run_command(["git", "pull", "--ff-only"], cwd=repo_path)


def tag_release_interactive(repo_path: str, repo_name: str, commit_summary: str) -> None:
    """
    After merge, propose a semver tag.
    """
    last_tag = get_last_semver_tag(repo_path)
    auto_bump = determine_bump_from_commits(commit_summary)
    suggested = compute_next_version(repo_path, auto_bump, default_first="v0.1.0")

    print(f"\n🏷️  Versioning for [bold green]{repo_name}[/]")
    print(f"Last tag: {last_tag or '(none)'}")
    print(f"Auto bump suggestion: [bold cyan]{auto_bump}[/]")
    print(f"Suggested next tag: [bold yellow]{suggested}[/]")

    choice = input("Choose bump (major/minor/patch) or press Enter to accept suggestion: ").strip().lower()
    bump = choice if choice in ("major", "minor", "patch") else auto_bump
    tag = compute_next_version(repo_path, bump, default_first="v0.1.0")

    confirm = input(f"Create and push tag {tag}? (y/n): ").strip().lower()
    if confirm != "y":
        print("⏭️  Skipped tagging.")
        return

    create_and_push_tag(repo_path, tag, message=f"Release {tag}")
    print(f"✅ Tag created and pushed: {tag}")


# ---------------- Ollama helpers ----------------

class OllamaError(RuntimeError):
    pass


def safe_parse_json(raw: str) -> dict | None:
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        return None


def ollama_chat_json(messages: list[dict], model: str | None = None, temperature: float = 0.2) -> str:
    """
    Calls Ollama /api/chat and returns assistant content (string).
    """
    payload = {
        "model": model or OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }

    url = f"{OLLAMA_HOST}/api/chat"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama unreachable: {e}") from e
    except Exception as e:
        raise OllamaError(f"Ollama error: {e}") from e

    content = (data.get("message") or {}).get("content")
    if not content:
        raise OllamaError(f"Unexpected Ollama response shape: {data}")
    return content


def generate_pr_text_with_ollama(repo_name: str, commit_summary: str) -> tuple[str | None, str | None]:
    """
    Returns (title, body) if success, else (None, None).
    """
    if not ENABLE_OLLAMA:
        return None, None

    pr_system = """You are a senior engineer writing a Pull Request for merging staging into master.

Rules:
- Output MUST be valid JSON only. No markdown fences, no extra text.
- JSON shape:
{
  "mr": {
    "title": "...",
    "description": "..."
  }
}
- title: <= 80 chars
- description: markdown with sections:
  ## What
  ## Why
  ## Testing
  ## Notes
- Keep it concise and accurate from the commit summary.
"""

    # Guard size (Ollama can choke on huge text)
    commit_summary_trimmed = commit_summary.strip()
    if len(commit_summary_trimmed) > 12000:
        commit_summary_trimmed = commit_summary_trimmed[:12000] + "\n- (truncated)"

    pr_user = f"""Repository: {repo_name}
Base: {DEFAULT_BASE_BRANCH}
Head: {DEFAULT_HEAD_BRANCH}

Commits included:
{commit_summary_trimmed}
"""

    raw = ollama_chat_json(
        [
            {"role": "system", "content": pr_system},
            {"role": "user", "content": pr_user},
        ],
        temperature=0.2,
    )

    data = safe_parse_json(raw)
    if not data:
        return None, None

    mr = data.get("mr") or {}
    title = (mr.get("title") or "").strip()
    body = (mr.get("description") or "").strip()
    if not title or not body:
        return None, None

    return title, body


# ---------------- Main PR flow ----------------

def create_and_merge_pr(path: str, repo_name: str) -> None:
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

    pr_number = existing_pr_number(path)
    if pr_number:
        print(f"🔗 Existing Pull Request detected: #{pr_number}")
    else:
        print(f"\n📘 Repository: [bold orange]{repo_name}[/]")
        print(f"--- Pull Request Preview ---\nTitle: {title}\n\n{body}\n---\n")

        confirm = input("🚀 Do you want to create and auto-merge this PR? (y/n): ").strip().lower()
        if confirm != "y":
            print("❌ Skipped.\n")
            return

        with console.status("[bold green]Creating pull request...", spinner="dots"):
            result = run_command(
                ["gh", "pr", "create",
                 "--base", DEFAULT_BASE_BRANCH,
                 "--head", DEFAULT_HEAD_BRANCH,
                 "--title", title,
                 "--body", body],
                cwd=path,
            )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            print(f"❌ Failed to create Pull Request for {repo_name}. Reason:\n{stderr}")
            return

        pr_url = None
        for line in (result.stdout or "").strip().splitlines():
            if line.startswith("https://github.com/"):
                pr_url = line
                break

        if pr_url:
            print(f"🔗 Pull Request created: {pr_url}")
        else:
            print("❌ Pull Request URL not found")
            return

    # Merge the PR (new or existing)
    with console.status("[bold cyan]Merging pull request...", spinner="dots"):
        merge_result = run_command(
            ["gh", "pr", "merge", "--merge", "--auto"],
            cwd=path,
        )

    if merge_result.returncode != 0:
        stderr = (merge_result.stderr or "").strip()
        print(f"❌ Failed to merge PR for {repo_name}. Reason:\n{stderr}")
        return

    print(f"✅ PR merged successfully for [bold green]{repo_name}[/]\n")

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
