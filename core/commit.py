import os
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

TYPE_PRIORITY = ["fix", "feat", "refactor", "docs", "ui", "chore"]

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def is_comment_line(line):
    stripped = line.lstrip()
    return stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")

def get_diff_content(path):
    result = subprocess.run(["git", "diff", "--cached"], cwd=path, capture_output=True, text=True)
    return result.stdout.strip()

def detect_commit_type_from_diff(diff_content):
    if not diff_content:
        return "chore"

    diff_lines = diff_content.lower().splitlines()
    type_counter = Counter()

    for line in diff_lines:
        if not (line.startswith('+') or line.startswith('-')):
            continue

        if is_comment_line(line):
            if "fix" in line or "bug" in line or "error" in line or "typo" in line:
                type_counter["fix"] += 0.5
            if "refactor" in line:
                type_counter["refactor"] += 0.5
            continue

        if "fix" in line or "bug" in line or "error" in line or "typo" in line:
            type_counter["fix"] += 1
        if "function" in line or "def " in line or "class " in line:
            type_counter["feat"] += 2
        if "refactor" in line or ("remove" in line and len(line) > 30):
            type_counter["refactor"] += 1
        if ".md" in line or "documentation" in line:
            type_counter["docs"] += 1
        if ".css" in line or ".scss" in line or ".html" in line:
            type_counter["ui"] += 1
        if ".json" in line or ".yml" in line or "config" in line or "build" in line:
            type_counter["chore"] += 1

    if not type_counter:
        return "chore"

    selected_type, _ = type_counter.most_common(1)[0]
    return selected_type

def get_modified_files_names(path):
    result = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=path, capture_output=True, text=True)
    files = result.stdout.strip().splitlines()
    keywords = []
    for f in files:
        basename = os.path.basename(f).lower()
        name_without_ext = os.path.splitext(basename)[0]
        keywords.append(name_without_ext)
    return keywords

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
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            keywords = get_modified_files_names(repo_path)

            if keywords:
                title_keywords = " and ".join(keywords[:2])
                commit_title = f"{commit_type}: update {title_keywords} ({date_str})"
            else:
                commit_title = f"{commit_type}: auto commit based on diff analysis ({date_str})"

            print("\n--- Preview of commit message ---\n")
            print(commit_title)
            print("\n--- End preview ---\n")

            user_input = input("✍️ Do you want to commit this change? (y/n): ").strip().lower()
            if user_input != "y":
                print("⏹️ Skipped commit.")
                continue

            with console.status("[bold green]Committing changes...[/]", spinner="dots"):
                subprocess.run(["git", "add", "."], cwd=repo_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(["git", "commit", "-m", commit_title], cwd=repo_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                results["committed"] += 1
            print(f"✅ Commit done.\n")

            push_input = input("📤 Do you want to push to staging? (y/n): ").strip().lower()
            if push_input == "y":
                with console.status("[bold cyan]Pushing to staging...[/]", spinner="dots"):
                    subprocess.run(["git", "push", "origin", DEFAULT_BRANCH], cwd=repo_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                results["pushed"] += 1
                print("🚀 Pushed to staging branch\n")
            else:
                print("⏭️ Skipped git push")

        if not found_repos:
            print(f"⚠️ No repositories found in {root_dir}")

    return results

if __name__ == "__main__":
    auto_commit_all_repos(ROOT_DIRS)
