import os
import subprocess
from datetime import datetime
from collections import defaultdict
from rich import print
from rich.panel import Panel
from rich.console import Console

console = Console()

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage")
]
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

EXCLUDED_KEYWORDS = [
    "changelog", "readme", "merge", "auto commit", "autocommit", "bump", "version", "initial commit"
]

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

def get_last_tag(path):
    tags = run_git_command(path, ["tag", "--sort=-creatordate"]).splitlines()
    return tags[0] if tags else None

def get_commits_since_tag(path, last_tag=None):
    if last_tag:
        range_spec = f"{last_tag}..HEAD"
    else:
        range_spec = "HEAD"

    log_output = run_git_command(path, ["log", range_spec, "--pretty=format:%s", "--no-merges"])
    commits = log_output.splitlines()
    filtered = [
        c for c in commits
        if c and not any(keyword in c.lower() for keyword in EXCLUDED_KEYWORDS)
    ]
    return filtered

def classify_commits(commits):
    categorized = defaultdict(list)
    uncategorized = []

    for commit in commits:
        found = False
        for prefix, emoji in EMOJI_MAP.items():
            if commit.lower().startswith(f"{prefix}:"):
                categorized[prefix].append(commit)
                found = True
                break
        if not found:
            uncategorized.append(commit)
    return categorized, uncategorized

def generate_changelog(commits, version_label):
    categorized, uncategorized = classify_commits(commits)

    block = [f"## [{version_label}] - {datetime.now().strftime('%Y-%m-%d')}", ""]

    for type_commit, messages in categorized.items():
        emoji = EMOJI_MAP.get(type_commit, "")
        block.append(f"### {emoji} {type_commit.capitalize()}")
        for msg in messages:
            clean_msg = msg.split(":", 1)[-1].strip()
            block.append(f"- {clean_msg}")
        block.append("")

    if uncategorized:
        block.append("### 🔖 Others")
        for msg in uncategorized:
            block.append(f"- {msg}")
        block.append("")

    return "\n".join(block)

def update_changelog(repo_path, changelog_content):
    changelog_path = os.path.join(repo_path, CHANGELOG_FILENAME)
    if os.path.exists(changelog_path):
        with open(changelog_path, "r", encoding="utf-8") as f:
            existing_content = f.read()
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write(changelog_content + "\n" + existing_content)
    else:
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write(changelog_content)

def commit_and_push_changelog(repo_path):
    with console.status("[bold green]Committing and pushing changelog...", spinner="dots"):
        subprocess.run(["git", "add", CHANGELOG_FILENAME], cwd=repo_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "commit", "-m", f"docs: update changelog ({datetime.now().strftime('%Y-%m-%d %H:%M')})"], cwd=repo_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "push"], cwd=repo_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("[green]✅ Changelog committed and pushed.[/green]")

def update_all_repos_interactive(root_dirs):
    print(f"\n🔄 Scanning repos for changelog updates\n")

    for root_dir in root_dirs:
        print(f"\n📂 Scanning root directory: {root_dir}\n")
        for repo in os.listdir(root_dir):
            repo_path = os.path.join(root_dir, repo)
            if not os.path.isdir(repo_path) or not is_git_repo(repo_path):
                continue

            last_tag = get_last_tag(repo_path)
            commits = get_commits_since_tag(repo_path, last_tag)
            if not commits:
                print(f"⚪ {repo}: No new commits to update changelog")
                continue

            version_label = last_tag if last_tag else "Unreleased"
            changelog_preview = generate_changelog(commits, version_label)

            repo_panel = Panel.fit(
                changelog_preview,
                title=f"[bold green]{repo}[/]",
                subtitle=f"[bold blue]Last Tag: {last_tag if last_tag else 'None'}[/]",
                border_style="cyan"
            )
            console.print(repo_panel)

            user_input = input("✍️ Write changelog? (y/n): ").strip().lower()
            if user_input == "y":
                update_changelog(repo_path, changelog_preview)
                print(f"✅ Changelog updated for {repo}")

                commit_input = input("📤 Do you want to commit and push the changelog? (y/n): ").strip().lower()
                if commit_input == "y":
                    commit_and_push_changelog(repo_path)
            else:
                print("⏹️ Skipped changelog.")

if __name__ == "__main__":
    update_all_repos_interactive(ROOT_DIRS)
