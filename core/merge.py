import os
import subprocess
from datetime import datetime
from rich import print

ROOT_DIR = os.path.expanduser("~/code/pers")

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def repo_has_diff_between_staging_and_master(path):
    subprocess.run(["git", "fetch"], cwd=path, stdout=subprocess.DEVNULL)
    result = subprocess.run(["git", "rev-list", "--count", "origin/master..origin/staging"], cwd=path, capture_output=True, text=True)
    return int(result.stdout.strip()) > 0

def get_commit_summary(path):
    result = subprocess.run(
        ["git", "log", "origin/master..origin/staging", "--pretty=format:- %s"],
        cwd=path,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()

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
    print(f"\n📘 Repository: {repo_name}")
    print(f"--- Pull Request Preview ---\nTitle: {title}\n\n{body}\n---\n")

    confirm = input("🚀 Do you want to create and auto-merge this PR? (y/n): ").strip().lower()
    if confirm != "y":
        print("❌ Skipped.")
        return

    subprocess.run(["gh", "pr", "create", "--base", "master", "--head", "staging", "--title", title, "--body", body], cwd=path)
    subprocess.run(["gh", "pr", "merge", "--merge", "--auto"], cwd=path)
    print("✅ PR created and merge scheduled.\n")

def main():
    print(f"\n🔄 Scanning for repos with pending staging → master merges in: {ROOT_DIR}\n")

    for repo in os.listdir(ROOT_DIR):
        path = os.path.join(ROOT_DIR, repo)
        if not is_git_repo(path):
            continue

        if repo_has_diff_between_staging_and_master(path):
            create_and_merge_pr(path, repo)
        else:
            print(f"✔️  [bold dark_orange]{repo}[/]: staging is up to date with master.")

if __name__ == "__main__":
    main()
