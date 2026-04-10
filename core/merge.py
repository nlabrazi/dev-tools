import os
import time

from rich import print
from rich.console import Console

from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE, ROOT_DIRS, resolve_repo_base_branch
from core.pr_message import generate_pr_text
from core.repositories import iter_git_repositories
from utils.common import env_int, is_dry_run, run_command
from utils.git import git_command, git_command_checked, git_output
from utils.console import ask_yes_no
from core.versioning import (
    compute_next_version,
    determine_bump_from_commits,
    create_and_push_tag,
    get_last_semver_tag,
)

console = Console()


def get_current_branch(path: str) -> str:
    return git_output(path, ["branch", "--show-current"])


def ensure_clean_worktree(path: str) -> None:
    """
    Ensure no pending changes and no merge in progress (avoid undefined state).
    """
    status = git_output(path, ["status", "--porcelain"])
    if status.strip():
        raise RuntimeError("Working tree is not clean (uncommitted changes detected).")

    merge_head = git_output(path, ["rev-parse", "--git-path", "MERGE_HEAD"])
    if merge_head:
        merge_head_path = merge_head if os.path.isabs(merge_head) else os.path.join(path, merge_head)
    else:
        merge_head_path = os.path.join(path, ".git", "MERGE_HEAD")
    if os.path.exists(merge_head_path):
        raise RuntimeError("Merge in progress detected (.git/MERGE_HEAD exists). Resolve/abort it first.")


def checkout_update_base_branch(repo_path: str, base_branch: str) -> None:
    git_command_checked(
        repo_path,
        ["fetch", "--all", "--prune"],
        silent=True,
        context="fetch remote branches",
    )
    git_command_checked(
        repo_path,
        ["fetch", "--tags"],
        silent=True,
        context="fetch tags",
    )
    git_command_checked(
        repo_path,
        ["checkout", base_branch],
        context=f"checkout {base_branch}",
    )
    git_command_checked(
        repo_path,
        ["pull", "--ff-only", DEFAULT_REMOTE, base_branch],
        context=f"pull {DEFAULT_REMOTE}/{base_branch}",
    )


def resolve_merge_base_branch(path: str) -> tuple[str | None, str]:
    git_command(path, ["fetch", DEFAULT_REMOTE, "--prune"], silent=True)
    return resolve_repo_base_branch(path, DEFAULT_REMOTE)


def repo_has_branch_diff(path: str, base_branch: str) -> bool:
    base_commit = git_output(
        path,
        ["merge-base", f"{DEFAULT_REMOTE}/{base_branch}", f"{DEFAULT_REMOTE}/{DEFAULT_HEAD_BRANCH}"],
    )
    head_commit = git_output(path, ["rev-parse", f"{DEFAULT_REMOTE}/{DEFAULT_HEAD_BRANCH}"])

    return bool(base_commit) and bool(head_commit) and base_commit != head_commit


def get_commit_summary(path: str, base_branch: str) -> str:
    return git_output(
        path,
        ["log", f"{DEFAULT_REMOTE}/{base_branch}..{DEFAULT_REMOTE}/{DEFAULT_HEAD_BRANCH}", "--pretty=format:- %s"],
    )


# ---------------- GitHub CLI helpers ----------------

def existing_pr_number(path: str, base_branch: str) -> str:
    """
    Returns PR number if a PR already exists for base=head pair, else "".
    """
    result = run_command(
        [
            "gh",
            "pr",
            "list",
            "--base",
            base_branch,
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


def get_pr_status(repo_path: str, pr_number: str) -> dict | None:
    result = run_command(
        [
            "gh",
            "pr",
            "view",
            pr_number,
            "--json",
            "state,mergedAt,mergeStateStatus,isDraft",
        ],
        cwd=repo_path,
    )
    if result.returncode != 0:
        return None

    data = safe_parse_json(result.stdout or "")
    if isinstance(data, dict):
        return data
    return None


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


def wait_for_pr_merge(repo_path: str, repo_name: str, pr_number: str, timeout_seconds: int = 90) -> bool:
    deadline = time.time() + max(timeout_seconds, 5)

    while time.time() < deadline:
        status = get_pr_status(repo_path, pr_number)
        if status:
            if status.get("state") == "MERGED" or status.get("mergedAt"):
                return True
            if status.get("state") == "CLOSED":
                print(f"❌ PR #{pr_number} for {repo_name} is closed without merge.")
                return False

        time.sleep(5)

    print(
        f"⏭️ PR #{pr_number} for {repo_name} is not merged yet. "
        "Release sync/tagging skipped until the merge is effective."
    )
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


# ---------------- Main PR flow ----------------

def create_and_merge_pr(path: str, repo_name: str, base_branch: str | None = None) -> None:
    # Safety first
    try:
        ensure_clean_worktree(path)
    except Exception as e:
        print(f"❌ {repo_name}: {e}")
        return

    if not base_branch:
        base_branch, resolution = resolve_merge_base_branch(path)
        if not base_branch:
            print(f"⚠️  {repo_name}: {resolution}.")
            return

    # Debug: show current branch (we *do not* depend on it anymore)
    current = get_current_branch(path)
    if current:
        print(f"🔎 Current branch (info only): [bold]{current}[/]")

    commit_summary = get_commit_summary(path, base_branch)

    if not commit_summary:
        print("⚠️  No new commits found to merge.")
        return

    title, body = generate_pr_text(
        repo_name,
        commit_summary,
        base_branch,
        head_branch=DEFAULT_HEAD_BRANCH,
    )

    # Check existing PR
    pr_number = existing_pr_number(path, base_branch)
    created_pr_url = None

    if pr_number:
        print(f"🔗 Existing Pull Request detected: #{pr_number}")
        if is_dry_run():
            print(f"🧪 Dry-run: would enable auto-merge for PR #{pr_number} in {repo_name}.")
            return
    else:
        print(f"\n📘 Repository: [bold orange]{repo_name}[/]")
        print(f"--- Pull Request Preview ---\nTitle: {title}\n\n{body}\n---\n")

        if not ask_yes_no("🚀 Do you want to create and auto-merge this PR?", default="n"):
            print("❌ Skipped.\n")
            return

        if is_dry_run():
            print(
                f"🧪 Dry-run: would create a pull request from {DEFAULT_HEAD_BRANCH} "
                f"to {base_branch} and enable auto-merge for {repo_name}."
            )
            return

        with console.status("[bold green]Creating pull request...", spinner="dots"):
            result = run_command(
                [
                    "gh", "pr", "create",
                    "--base", base_branch,
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

    merge_timeout = env_int("GH_PR_MERGE_TIMEOUT", 90, minimum=5)
    if not wait_for_pr_merge(path, repo_name, pr_number, timeout_seconds=merge_timeout):
        return

    print(f"✅ PR #{pr_number} is effectively merged for [bold green]{repo_name}[/]\n")

    # Refresh local base branch and tag the release
    try:
        checkout_update_base_branch(path, base_branch)
        tag_release_interactive(path, repo_name, commit_summary)
    except Exception as e:
        print(f"⚠️  Tagging step failed/skipped for {repo_name}: {e}")


def main(root_dirs: list[str] = ROOT_DIRS) -> None:
    print(f"\n🔄 Scanning for repos with pending {DEFAULT_HEAD_BRANCH} → base branch merges\n")

    for root_dir in root_dirs:
        console.print(f"\n📂 [bold yellow]Scanning root directory:[/] {root_dir}\n")

        if not os.path.isdir(root_dir):
            print(f"⚠️  Root directory not found: {root_dir}")
            continue

        found_repos = False
        for repo, path in iter_git_repositories(root_dir):
            found_repos = True
            base_branch, resolution = resolve_merge_base_branch(path)
            if not base_branch:
                print(f"⚠️  {repo}: {resolution}.")
                continue

            if repo_has_branch_diff(path, base_branch):
                print(f"📦 [bold green]Found pending merge for {repo}[/]: {DEFAULT_HEAD_BRANCH} → {base_branch}")
                create_and_merge_pr(path, repo, base_branch)
            else:
                print(f"✔️  [bold dark_orange]{repo}[/]: {DEFAULT_HEAD_BRANCH} is up to date with {base_branch}.")

        if not found_repos:
            print(f"⚠️  No repositories found in {root_dir}")


if __name__ == "__main__":
    main()
