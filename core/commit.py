import os
import subprocess
from datetime import datetime
from collections import Counter

ROOT_DIR = os.path.expanduser("~/code/pers")
DEFAULT_BRANCH = "staging"

FILE_TYPE_TO_COMMIT_TYPE = {
    ".ts": "feat",
    ".html": "ui",
    ".scss": "ui",
    ".css": "ui",
    ".tsx": "feat",
    ".jsx": "feat",
    ".rb": "feat",
    ".py": "chore",
    ".js": "feat",
    ".json": "chore",
    ".yml": "chore",
    ".yaml": "chore",
    ".md": "docs",
    "README.md": "docs",
    "CHANGELOG.md": "docs",
    "default": "chore"
}

def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def get_modified_files(path):
    result = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True)
    lines = result.stdout.strip().splitlines()
    return [line[3:] for line in lines if line]

def detect_commit_type(files):
    types = []
    for file in files:
        filename = os.path.basename(file).lower()
        if "test" in file.lower():
            types.append("test")
        elif filename in ["readme.md", "changelog.md"]:
            types.append("docs")
        else:
            ext = os.path.splitext(file)[1]
            types.append(FILE_TYPE_TO_COMMIT_TYPE.get(ext, FILE_TYPE_TO_COMMIT_TYPE["default"]))
    if not types:
        return "chore"
    return Counter(types).most_common(1)[0][0]

def preview_commit_message(commit_type):
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"{commit_type}: auto commit of modified files ({date_str})"

def auto_commit_all_repos(root_dir):
    print(f"\n🔄 Scanning repos in: {root_dir}\n")
    results = {
        "committed": 0,
        "pushed": 0
    }

    for repo in os.listdir(root_dir):
        repo_path = os.path.join(root_dir, repo)
        if not os.path.isdir(repo_path) or not is_git_repo(repo_path):
            continue

        modified = get_modified_files(repo_path)
        if not modified:
            print(f"⚪ {repo}: No changes to commit")
            continue

        print(f"\n📦 Committing for [bold green]{repo}...")
        commit_type = detect_commit_type(modified)
        message = preview_commit_message(commit_type)

        print("\n--- Preview of commit message ---\n")
        print(message)
        print("\n--- End preview ---\n")

        user_input = input("✍️ Do you want to commit this change? (y/n): ").strip().lower()
        if user_input != "y":
            print("⏹️ Skipped commit.")
            continue

        subprocess.run(["git", "add", "."], cwd=repo_path)
        subprocess.run(["git", "commit", "-m", message], cwd=repo_path)
        results["committed"] += 1
        print(f"✅ Commit done.\n")

        push_input = input("📤 Do you want to push to staging? (y/n): ").strip().lower()
        if push_input == "y":
            subprocess.run(["git", "push", "origin", DEFAULT_BRANCH], cwd=repo_path)
            results["pushed"] += 1
            print("🚀 Pushed to staging branch\n")
        else:
            print("⏭️ Skipped git push")

    return results

if __name__ == "__main__":
    auto_commit_all_repos(ROOT_DIR)
