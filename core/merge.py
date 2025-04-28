import os
import subprocess
from datetime import datetime
from time import sleep
from rich import print
from rich.console import Console
from rich.spinner import Spinner

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage")
]

DEFAULT_BASE_BRANCH = "master"
DEFAULT_HEAD_BRANCH = "staging"

console = Console()

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def run_git_command(path, args):
    result = subprocess.run(
        ["git"] + args,
        cwd=path,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()

def repo_has_diff_between_staging_and_master(path):
    subprocess.run(["git", "fetch"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Utilisation de merge-base pour vérifier si staging est en avance
    base_commit = run_git_command(path, ["merge-base", f"origin/{DEFAULT_BASE_BRANCH}", f"origin/{DEFAULT_HEAD_BRANCH}"])
    head_commit = run_git_command(path, ["rev-parse", f"origin/{DEFAULT_HEAD_BRANCH}"])

    return base_commit != head_commit

def get_commit_summary(path):
    return run_git_command(path, ["log", f"origin/{DEFAULT_BASE_BRANCH}..origin/{DEFAULT_HEAD_BRANCH}", "--pretty=format:- %s"])

def create_and_merge_pr(path, repo_name):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"chore: merge staging into master ({date_str})"
    commit_summary = get_commit_summary(path)

    if not commit_summary:
        print("⚠️  No new commits found to merge.")
        return

    body = f"""This pull request merges the latest validated commits from `staging` into `master`.

**Summary of changes:**

{commit_summary}

_Auto-generated on {date_str}_
"""
    print(f"\n📘 Repository: [bold orange]{repo_name}[/]")
    print(f"--- Pull Request Preview ---\nTitle: {title}\n\n{body}\n---\n")

    confirm = input("🚀 Do you want to create and auto-merge this PR? (y/n): ").strip().lower()
    if confirm != "y":
        print("❌ Skipped.")
        return

    # Création de la PR
    spinner = console.status("[bold green]Creating pull request...", spinner="dots")
    with spinner:
        result = subprocess.run(
            ["gh", "pr", "create", "--base", DEFAULT_BASE_BRANCH, "--head", DEFAULT_HEAD_BRANCH, "--title", title, "--body", body],
            cwd=path,
            capture_output=True,
            text=True
        )

    pr_url = None
    for line in result.stdout.strip().splitlines():
        if line.startswith("https://github.com/"):
            pr_url = line
            break

    if pr_url:
        print(f"🔗 Pull Request created: {pr_url}")
    else:
        print(f"❌ Failed to create Pull Request for {repo_name}")
        return

    # Merge automatique de la PR
    spinner = console.status("[bold cyan]Merging pull request...", spinner="dots")
    with spinner:
        subprocess.run(["gh", "pr", "merge", "--merge", "--auto"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    spinner = console.status("[bold cyan]Waiting for merge confirmation...", spinner="dots")
    with spinner:
        merged = False
        for _ in range(5):
            sleep(5)
            check = subprocess.run(
                ["gh", "pr", "view", "--json", "merged,isInMergeQueue", "--jq", ".merged or .isInMergeQueue"],
                cwd=path,
                capture_output=True,
                text=True
            )
            if check.stdout.strip() == "true":
                merged = True
                break

    if merged:
        print(f"✅ PR merged successfully for [bold green]{repo_name}[/]\n")
    else:
        print(f"❌ Merge failed or not completed for [bold red]{repo_name}[/]\n")

def main():
    print(f"\n🔄 Scanning for repos with pending staging → master merges\n")

    for root_dir in ROOT_DIRS:
        console.print(f"\n📂 [bold yellow]Scanning root directory:[/] {root_dir}\n")
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
