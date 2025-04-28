import os
import subprocess
from datetime import datetime
from time import sleep
from rich import print
from rich.console import Console
from rich.spinner import Spinner
from collections import defaultdict

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage")
]

DEFAULT_BASE_BRANCH = "master"
DEFAULT_HEAD_BRANCH = "staging"

console = Console()

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def repo_has_diff_between_staging_and_master(path):
    subprocess.run(["git", "fetch"], cwd=path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = subprocess.run(
        ["git", "rev-list", "--count", f"origin/{DEFAULT_BASE_BRANCH}..origin/{DEFAULT_HEAD_BRANCH}"],
        cwd=path,
        capture_output=True,
        text=True
    )
    return int(result.stdout.strip()) > 0

def get_commits(path):
    result = subprocess.run(
        ["git", "log", f"origin/{DEFAULT_BASE_BRANCH}..origin/{DEFAULT_HEAD_BRANCH}", "--pretty=format:%s"],
        cwd=path,
        capture_output=True,
        text=True
    )
    return result.stdout.strip().splitlines()

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
                # Placeholder for PR generation
            else:
                print(f"✔️  [bold dark_orange]{repo}[/]: staging is up to date with master.")

if __name__ == "__main__":
    main()
