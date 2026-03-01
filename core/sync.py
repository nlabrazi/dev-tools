# core/sync.py

import os
from rich.console import Console
from utils.common import run_command

DEFAULT_BRANCH = "master"
REMOTE = "origin"

console = Console()


def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def git_output(repo_path: str, args: list[str]) -> str:
    res = run_command(["git"] + args, cwd=repo_path, silent=True)
    return (res.stdout or "").strip()


def repo_is_clean(repo_path: str) -> bool:
    res = run_command(["git", "status", "--porcelain"], cwd=repo_path, silent=True)
    return (res.stdout or "").strip() == ""


def sync_master(repo_path: str, repo_name: str) -> None:
    if not repo_is_clean(repo_path):
        console.print(f"⚠️  [yellow]{repo_name}[/]: repo not clean, skip sync (stash/commit first).")
        return

    res_fetch = run_command(["git", "fetch", "--all", "--prune"], cwd=repo_path, silent=True)
    if res_fetch.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: fetch failed:\n{(res_fetch.stderr or '').strip()}")
        return

    res_checkout = run_command(["git", "checkout", DEFAULT_BRANCH], cwd=repo_path, silent=True)
    if res_checkout.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: checkout {DEFAULT_BRANCH} failed:\n{(res_checkout.stderr or '').strip()}")
        return

    res_pull = run_command(
        ["git", "pull", "--ff-only", REMOTE, DEFAULT_BRANCH],
        cwd=repo_path,
        silent=True,
    )
    if res_pull.returncode != 0:
        console.print(f"❌ [red]{repo_name}[/]: pull --ff-only failed:\n{(res_pull.stderr or '').strip()}")
        return

    head = git_output(repo_path, ["rev-parse", "--short", "HEAD"])
    console.print(f"✅ [green]{repo_name}[/]: {DEFAULT_BRANCH} synced (HEAD {head})")


def sync_all_repos(root_dirs: list[str]) -> None:
    for root_dir in root_dirs:
        if not os.path.isdir(root_dir):
            console.print(f"⚠️  Root directory not found: {root_dir}")
            continue

        for repo in os.listdir(root_dir):
            path = os.path.join(root_dir, repo)
            if not is_git_repo(path):
                continue

            sync_master(path, repo)


def main(root_dirs: list[str]) -> None:
    sync_all_repos(root_dirs)
