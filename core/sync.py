# core/sync.py

import os

from core.config import DEFAULT_REMOTE, ROOT_DIRS, describe_base_branch_strategy, resolve_repo_base_branch
from core.repositories import iter_git_repositories
from rich.console import Console
from utils.common import is_dry_run, run_command
from utils.console import ask_yes_no

console = Console()
REMOTE = DEFAULT_REMOTE


def git_output(repo_path: str, args: list[str]) -> str:
    res = run_command(["git"] + args, cwd=repo_path, silent=True)
    return (res.stdout or "").strip()


def repo_is_clean(repo_path: str) -> bool:
    res = run_command(["git", "status", "--porcelain"], cwd=repo_path, silent=True)
    return (res.stdout or "").strip() == ""


def fetch(repo_path: str, repo_name: str) -> bool:
    res = run_command(["git", "fetch", "--all", "--prune"], cwd=repo_path, silent=True)
    if res.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: fetch failed:\n{(res.stderr or '').strip()}")
        return False
    return True


def ensure_local_branch_exists(repo_path: str, repo_name: str, branch: str) -> str:
    """
    Ensure local branch exists; if not, try to create it tracking origin/<branch>.
    """
    # Does local branch exist?
    res = run_command(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path, silent=True)
    if res.returncode == 0:
        return "ready"

    if is_dry_run():
        console.print(
            f"🧪 [cyan]{repo_name}[/]: would create local branch '{branch}' tracking {REMOTE}/{branch}."
        )
        return "dry-run"

    # Create local branch tracking origin
    res2 = run_command(["git", "checkout", "-b", branch, f"{REMOTE}/{branch}"], cwd=repo_path, silent=True)
    if res2.returncode != 0:
        return "failed"
    return "ready"


def checkout_branch(repo_path: str, repo_name: str, branch: str) -> str:
    current_branch = git_output(repo_path, ["branch", "--show-current"])
    if current_branch == branch:
        return "ready"

    if is_dry_run():
        console.print(f"🧪 [cyan]{repo_name}[/]: would checkout {branch}.")
        return "dry-run"

    res = run_command(["git", "checkout", branch], cwd=repo_path, silent=True)
    if res.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: checkout {branch} failed:\n{(res.stderr or '').strip()}")
        return "failed"
    return "ready"


def get_ahead_behind(repo_path: str, branch: str) -> tuple[int, int] | None:
    """
    Returns (ahead, behind) counts comparing local branch to origin branch.
    Uses: git rev-list --left-right --count <local>...<remote>
    Output: "<ahead>\t<behind>"
    """
    out = git_output(repo_path, ["rev-list", "--left-right", "--count", f"{branch}...{REMOTE}/{branch}"])
    if not out:
        return None
    parts = out.replace("\t", " ").split()
    if len(parts) != 2:
        return None
    try:
        ahead = int(parts[0])
        behind = int(parts[1])
        return ahead, behind
    except ValueError:
        return None


def pull_ff_only(repo_path: str, repo_name: str, branch: str) -> str:
    if is_dry_run():
        console.print(f"🧪 [cyan]{repo_name}[/]: would pull --ff-only {REMOTE}/{branch}.")
        return "dry-run"

    res = run_command(["git", "pull", "--ff-only", REMOTE, branch], cwd=repo_path, silent=True)
    if res.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: pull --ff-only failed:\n{(res.stderr or '').strip()}")
        return "failed"
    return "pulled"


def describe_sync_plan() -> str:
    strategy = describe_base_branch_strategy(REMOTE)
    return f"Checkout and pull each repo base branch from {REMOTE}? Strategy: {strategy}."


def sync_default_branch(repo_path: str, repo_name: str) -> None:
    if not repo_is_clean(repo_path):
        console.print(f"⚠️  [yellow]{repo_name}[/]: repo not clean, skip sync (stash/commit first).")
        return

    if not fetch(repo_path, repo_name):
        return

    default_branch, resolution = resolve_repo_base_branch(repo_path, REMOTE)
    if not default_branch:
        console.print(f"⚠️  [yellow]{repo_name}[/]: {resolution}. Skip.")
        return

    # Ensure local branch exists (some repos only have main locally or nothing checked out)
    branch_status = ensure_local_branch_exists(repo_path, repo_name, default_branch)
    if branch_status == "failed":
        console.print(f"❌ [red]{repo_name}[/]: could not create/find local branch '{default_branch}'.")
        return
    if branch_status == "dry-run":
        return

    checkout_status = checkout_branch(repo_path, repo_name, default_branch)
    if checkout_status == "failed":
        return

    counts = get_ahead_behind(repo_path, default_branch)
    if not counts:
        console.print(f"⚠️  [yellow]{repo_name}[/]: cannot compute ahead/behind. Skip.")
        return

    ahead, behind = counts

    if behind <= 0 and ahead <= 0:
        head = git_output(repo_path, ["rev-parse", "--short", default_branch])
        console.print(f"✔️  [green]{repo_name}[/]: {default_branch} up-to-date (HEAD {head})")
        return
    if behind <= 0 and ahead > 0:
        head = git_output(repo_path, ["rev-parse", "--short", default_branch])
        console.print(
            f"ℹ️  [cyan]{repo_name}[/]: {default_branch} is ahead of {REMOTE}/{default_branch} "
            f"by {ahead} commit(s) (HEAD {head})"
        )
        return

    # There is an actual need to pull => ask y/n
    if ahead > 0:
        console.print(
            f"⚠️  [yellow]{repo_name}[/]: {default_branch} diverged from {REMOTE}/{default_branch} "
            f"(ahead {ahead}, behind {behind})."
        )
        return

    if not ask_yes_no(f"{repo_name}: {default_branch} is behind {REMOTE} by {behind} commit(s). Pull now?", default="y"):
        console.print(f"⏭️  [yellow]{repo_name}[/]: skipped pull.")
        return

    pull_status = pull_ff_only(repo_path, repo_name, default_branch)
    if pull_status == "pulled":
        head = git_output(repo_path, ["rev-parse", "--short", default_branch])
        console.print(f"✅ [green]{repo_name}[/]: pulled {default_branch} (HEAD {head})")


def sync_all_repos(root_dirs: list[str]) -> None:
    for root_dir in root_dirs:
        if not os.path.isdir(root_dir):
            console.print(f"⚠️  Root directory not found: {root_dir}")
            continue

        found_repos = False
        for repo, path in iter_git_repositories(root_dir):
            found_repos = True
            sync_default_branch(path, repo)

        if not found_repos:
            console.print(f"⚠️  No repositories found in {root_dir}")


def main(root_dirs: list[str] = ROOT_DIRS) -> None:
    sync_all_repos(root_dirs)
