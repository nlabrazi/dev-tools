import os
import subprocess
from datetime import datetime
from rich import print

ROOT_DIR = os.path.expanduser("~/code/pers")
CHANGELOG_FILENAME = "CHANGELOG.md"

EMOJI_MAP = {
    "feat": "✨",
    "fix": "🐛",
    "docs": "📝",
    "refactor": "🧹",
    "test": "✅",
    "chore": "🔧",
    "style": "🎨",
    "perf": "🚀",
    "ci": "🔁",
    "build": "🏗️",
}

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def get_repo_name(path):
    return os.path.basename(os.path.normpath(path))

def get_commits(path):
    try:
        # Trouve le dernier commit contenant "update changelog"
        result = subprocess.run(
            ["git", "log", "--grep=update changelog", "-n", "1", "--pretty=format:%H"],
            cwd=path,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        last_changelog_commit = result.stdout.strip()

        # Si un commit changelog est trouvé, on ne prend que les commits après
        if last_changelog_commit:
            log_range = f"{last_changelog_commit}..HEAD"
        else:
            log_range = "HEAD"  # Si aucun changelog existant, on prend tout

        # Récupère les messages (hors merge)
        result = subprocess.run(
            ["git", "log", log_range, "--pretty=format:%s", "--no-merges"],
            cwd=path,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        raw_commits = result.stdout.strip().split("\n")

        # Nettoyage des messages vides ou inutiles
        filtered = [
            c for c in raw_commits
            if c and "update changelog" not in c.lower()
        ]

        # Si aucun commit utile, on retourne une liste vide
        if not filtered:
            return []

        return [add_emoji_to_commit(c) for c in filtered]
    except Exception:
        return []

def add_emoji_to_commit(commit_msg):
    for keyword, emoji in EMOJI_MAP.items():
        if commit_msg.startswith(f"{keyword}:") or commit_msg.startswith(f"{keyword}("):
            return f"- {emoji} {commit_msg}"
    return f"- 🔸 {commit_msg}"

def preview_changelog(commits):
    today = datetime.now().strftime("%Y-%m-%d")
    block = [f"## [Unreleased] - {today}\n", "", *commits, "", ""]
    return "\n".join(block)

def write_changelog(path, changelog_block):
    changelog_path = os.path.join(path, CHANGELOG_FILENAME)

    if os.path.exists(changelog_path):
        with open(changelog_path, "r") as f:
            existing = f.read()
        with open(changelog_path, "w") as f:
            f.write(changelog_block + existing)
    else:
        with open(changelog_path, "w") as f:
            f.write("# 📅 CHANGELOG\n\n")
            f.write(changelog_block)

    return True

def git_push_staging(path):
    try:
        subprocess.run(["git", "add", CHANGELOG_FILENAME], cwd=path)
        subprocess.run(["git", "commit", "-m", "chore: update changelog"], cwd=path)
        subprocess.run(["git", "push", "origin", "staging"], cwd=path)
        print("🚀 Pushed to staging branch\n")
    except Exception as e:
        print(f"❌ Git push failed: {e}")

def update_all_repos_interactive(root_dir):
    print(f"\n🔄 Scanning repos in: {root_dir}\n")
    updated = 0

    for repo in os.listdir(root_dir):
        repo_path = os.path.join(root_dir, repo)
        if not os.path.isdir(repo_path) or not is_git_repo(repo_path):
            continue

        print(f"\n📘 Updating changelog for [bold cyan]{repo}...")
        commits = get_commits(repo_path)
        if not commits:
            print(f"⚠️ No new commits found")
            continue

        preview = preview_changelog(commits)
        print("\n--- Preview of changelog block ---\n")
        print(preview)
        print("\n--- End preview ---\n")

        user_input = input("✍️ Do you want to write this changelog to the project? (y/n): ").lower()
        if user_input == "y":
            if write_changelog(repo_path, preview):
                updated += 1
                print(f"✅ CHANGELOG.md updated for [bold cyan]{repo}\n")
                push_input = input("📤 Do you want to push to staging? (y/n): ").lower()
                if push_input == "y":
                    git_push_staging(repo_path)
                else:
                    print("⏭️ Skipped git push")
            else:
                print(f"❌ Failed to write changelog for [bold cyan]{repo}")
        else:
            print(f"⏹️ Skipped writing for [bold cyan]{repo}")

    return updated

if __name__ == "__main__":
    update_all_repos_interactive(ROOT_DIR)
