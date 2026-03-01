# core/sync.py

import os
from rich.console import Console
from utils.common import run_command
from utils.console import ask_yes_no

console = Console()
REMOTE = "origin"


def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


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


def get_default_remote_branch(repo_path: str) -> str | None:
    """
    Returns default branch name from origin/HEAD (e.g. 'main' or 'master').
    Requires fetch to be done before in many cases.
    """
    # Typical output: "refs/remotes/origin/main"
    ref = git_output(repo_path, ["symbolic-ref", f"refs/remotes/{REMOTE}/HEAD"])
    if ref.startswith(f"refs/remotes/{REMOTE}/"):
        return ref.split(f"refs/remotes/{REMOTE}/", 1)[1].strip() or None
    return None


def ensure_local_branch_exists(repo_path: str, branch: str) -> bool:
    """
    Ensure local branch exists; if not, try to create it tracking origin/<branch>.
    """
    # Does local branch exist?
    res = run_command(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path, silent=True)
    if res.returncode == 0:
        return True

    # Create local branch tracking origin
    res2 = run_command(["git", "checkout", "-b", branch, f"{REMOTE}/{branch}"], cwd=repo_path, silent=True)
    return res2.returncode == 0


def checkout_branch(repo_path: str, repo_name: str, branch: str) -> bool:
    res = run_command(["git", "checkout", branch], cwd=repo_path, silent=True)
    if res.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: checkout {branch} failed:\n{(res.stderr or '').strip()}")
        return False
    return True


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


def pull_ff_only(repo_path: str, repo_name: str, branch: str) -> bool:
    res = run_command(["git", "pull", "--ff-only", REMOTE, branch], cwd=repo_path, silent=True)
    if res.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: pull --ff-only failed:\n{(res.stderr or '').strip()}")
        return False
    return True


def sync_default_branch(repo_path: str, repo_name: str) -> None:
    if not repo_is_clean(repo_path):
        console.print(f"⚠️  [yellow]{repo_name}[/]: repo not clean, skip sync (stash/commit first).")
        return

    if not fetch(repo_path, repo_name):
        return

    default_branch = get_default_remote_branch(repo_path)
    if not default_branch:
        console.print(f"⚠️  [yellow]{repo_name}[/]: could not resolve {REMOTE}/HEAD default branch. Skip.")
        return

    # Ensure local branch exists (some repos only have main locally or nothing checked out)
    if not ensure_local_branch_exists(repo_path, default_branch):
        console.print(f"❌ [red]{repo_name}[/]: could not create/find local branch '{default_branch}'.")
        return

    if not checkout_branch(repo_path, repo_name, default_branch):
        return

    counts = get_ahead_behind(repo_path, default_branch)
    if not counts:
        console.print(f"⚠️  [yellow]{repo_name}[/]: cannot compute ahead/behind. Skip.")
        return

    ahead, behind = counts

    if behind <= 0:
        head = git_output(repo_path, ["rev-parse", "--short", "HEAD"])
        console.print(f"✔️  [green]{repo_name}[/]: {default_branch} up-to-date (HEAD {head})")
        return

    # There is an actual need to pull => ask y/n
    if not ask_yes_no(f"{repo_name}: {default_branch} is behind origin by {behind} commit(s). Pull now?", default="y"):
        console.print(f"⏭️  [yellow]{repo_name}[/]: skipped pull.")
        return

    if pull_ff_only(repo_path, repo_name, default_branch):
        head = git_output(repo_path, ["rev-parse", "--short", "HEAD"])
        console.print(f"✅ [green]{repo_name}[/]: pulled {default_branch} (HEAD {head})")


def sync_all_repos(root_dirs: list[str]) -> None:
    for root_dir in root_dirs:
        if not os.path.isdir(root_dir):
            console.print(f"⚠️  Root directory not found: {root_dir}")
            continue

        for repo in os.listdir(root_dir):
            path = os.path.join(root_dir, repo)
            if not is_git_repo(path):
                continue

            sync_default_branch(path, repo)


def main(root_dirs: list[str]) -> None:
    sync_all_repos(root_dirs)
