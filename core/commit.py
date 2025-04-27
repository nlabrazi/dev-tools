import os
import re
import subprocess
from datetime import datetime
from collections import Counter
from rich.console import Console

console = Console()

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage")
]
DEFAULT_BRANCH = "staging"

# Types prioritaires dans l'ordre
TYPE_PRIORITY = ["fix", "feat", "refactor", "docs", "ui", "chore"]

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def is_comment_line(line):
    """Return True if the line looks like a comment."""
    stripped = line.lstrip()
    return stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")

def get_diff_content(path):
    """Get the git diff --cached content."""
    result = subprocess.run(["git", "diff", "--cached"], cwd=path, capture_output=True, text=True)
    return result.stdout.strip()

def detect_commit_type_from_diff(diff_content):
    """Detect the commit type based on diff analysis (improved)."""
    if not diff_content:
        return "chore"

    diff_lines = diff_content.lower().splitlines()
    type_counter = Counter()

    for line in diff_lines:
        if not (line.startswith('+') or line.startswith('-')):
            continue  # Only analyze additions/deletions

        if is_comment_line(line):
            # If line is a comment, weight it less
            if "fix" in line or "bug" in line or "error" in line or "typo" in line:
                type_counter["fix"] += 0.5
            if "refactor" in line:
                type_counter["refactor"] += 0.5
            continue

        # Real code changes (heavier weight)
        if "fix" in line or "bug" in line or "error" in line or "typo" in line:
            type_counter["fix"] += 1
        if "function" in line or "def " in line or "class " in line:
            type_counter["feat"] += 2  # heavily weight new features
        if "refactor" in line or ("remove" in line and len(line) > 30):
            type_counter["refactor"] += 1
        if ".md" in line or "documentation" in line:
            type_counter["docs"] += 1
        if ".css" in line or ".scss" in line or ".html" in line:
            type_counter["ui"] += 1
        if ".json" in line or ".yml" in line or ".yaml" in line or "config" in line or "build" in line:
            type_counter["chore"] += 1

    if not type_counter:
        return "chore"

    # Si plusieurs types détectés : choisir celui qui a le plus haut score
    selected_type, _ = type_counter.most_common(1)[0]
    return selected_type

def generate_commit_title(commit_type):
    """Generate the commit title based on detected type."""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"{commit_type}: auto commit based on diff analysis ({date_str})"

def auto_commit_all_repos(root_dirs):
    print(f"\n🔄 Scanning repos in: {', '.join(root_dirs)}\n")
    results = {
        "committed": 0,
        "pushed": 0
    }

    for root_dir in root_dirs:
        console.print(f"\n📂 [bold yellow]Scanning root directory:[/] {root_dir}\n")
        found_repos = False

        for repo in os.listdir(root_dir):
            repo_path = os.path.join(root_dir, repo)
            if not os.path.isdir(repo_path) or not is_git_repo(repo_path):
                continue

            found_repos = True
            diff_content = get_diff_content(repo_path)
            if not diff_content:
                print(f"⚪ {repo}: No changes to commit")
                continue

            console.print(f"\n📦 Committing for [bold green]{repo}...[/]")
            commit_type = detect_commit_type_from_diff(diff_content)
            commit_title = generate_commit_title(commit_type)

            print("\n--- Preview of commit message ---\n")
            print(commit_title)
            print("\n--- End preview ---\n")

            user_input = input("✍️ Do you want to commit this change? (y/n): ").strip().lower()
            if user_input != "y":
                print("⏹️ Skipped commit.")
                continue

            subprocess.run(["git", "add", "."], cwd=repo_path)
            subprocess.run(["git", "commit", "-m", commit_title], cwd=repo_path)
            results["committed"] += 1
            print(f"✅ Commit done.\n")

            push_input = input("📤 Do you want to push to staging? (y/n): ").strip().lower()
            if push_input == "y":
                subprocess.run(["git", "push", "origin", DEFAULT_BRANCH], cwd=repo_path)
                results["pushed"] += 1
                print("🚀 Pushed to staging branch\n")
            else:
                print("⏭️ Skipped git push")

        if not found_repos:
            print(f"⚠️ No repositories found in {root_dir}")

    return results

if __name__ == "__main__":
    auto_commit_all_repos(ROOT_DIRS)
