import os
from datetime import datetime
from collections import Counter
from rich.console import Console

from utils.common import run_command
from core.ollama import chat_json, OllamaError
from core.prompts import COMMIT_SYSTEM, COMMIT_USER_TEMPLATE
from core.formatters import safe_parse_json, build_conventional_commit

console = Console()

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage"),
]
DEFAULT_BRANCH = "staging"

TYPE_PRIORITY = ["fix", "feat", "refactor", "docs", "ui", "chore"]


def is_git_repo(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".git"))


def is_comment_line(line: str) -> bool:
    stripped = line.lstrip()
    return (
        stripped.startswith("#")
        or stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
    )


def git_status_porcelain(path: str) -> list[str]:
    """
    Returns lines like:
      ' M file.txt' (unstaged)
      'M  file.txt' (staged)
      '?? file.txt' (untracked)
    """
    out = run_command(["git", "status", "--porcelain"], cwd=path).stdout or ""
    lines = [l.rstrip("\n") for l in out.splitlines() if l.strip()]
    return lines


def has_staged_changes(status_lines: list[str]) -> bool:
    # XY format. If X != ' ' then staged changes exist.
    for line in status_lines:
        if line.startswith("??"):
            continue
        if len(line) >= 2 and line[0] != " ":
            return True
    return False


def has_unstaged_changes(status_lines: list[str]) -> bool:
    # If Y != ' ' then unstaged changes exist (including untracked handled below)
    for line in status_lines:
        if line.startswith("??"):
            return True
        if len(line) >= 2 and line[1] != " ":
            return True
    return False


def get_diff_content_cached(path: str) -> str:
    return (run_command(["git", "diff", "--cached"], cwd=path).stdout or "").strip()


def get_modified_files_names_cached(path: str) -> list[str]:
    out = (run_command(["git", "diff", "--cached", "--name-only"], cwd=path).stdout or "").strip()
    return out.splitlines() if out else []


def detect_commit_type_from_diff(diff_content: str) -> str:
    if not diff_content:
        return "chore"

    diff_lines = diff_content.lower().splitlines()
    type_counter = Counter()

    for line in diff_lines:
        if not (line.startswith("+") or line.startswith("-")):
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


def generate_commit_message_with_ollama(repo: str, files: list[str], diff_content: str) -> str | None:
    """
    Returns full commit message (header + body) or None if Ollama fails / bad JSON.
    """
    try:
        user_prompt = COMMIT_USER_TEMPLATE.format(
            repo=repo,
            files="\n".join(f"- {f}" for f in files) or "- (unknown)",
            diff=diff_content[:12000],  # garde-fou
        )
        with console.status("[bold cyan]🤖 Generating commit message...[/]", spinner="dots"):
            raw = chat_json(
                [
                    {"role": "system", "content": COMMIT_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
            )
        data = safe_parse_json(raw)
        if not data:
            return None
        return build_conventional_commit(data)
    except OllamaError as e:
        print(f"⚠️ Ollama unavailable, fallback used. Reason: {e}")
        return None
    except Exception as e:
        print(f"⚠️ Ollama output invalid, fallback used. Reason: {e}")
        return None


def commit_with_message(repo_path: str, full_message: str) -> bool:
    """
    Commits with header + body without losing formatting.
    We do: git commit -m <header> -m <body>
    """
    lines = [l.rstrip() for l in full_message.splitlines()]
    header = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip()

    if not header:
        print("❌ Empty commit header, abort.")
        return False

    cmd = ["git", "commit", "-m", header]
    if body:
        cmd += ["-m", body]

    res = run_command(cmd, cwd=repo_path)
    if res.returncode != 0:
        print(f"❌ git commit failed:\n{(res.stderr or '').strip()}")
        return False
    return True


def auto_commit_all_repos(root_dirs: list[str]):
    print(f"\n🔄 Scanning repos in: {', '.join(root_dirs)}\n")
    results = {"committed": 0, "pushed": 0}

    for root_dir in root_dirs:
        console.print(f"\n📂 [bold yellow]Scanning root directory:[/] {root_dir}\n")
        found_repos = False

        if not os.path.isdir(root_dir):
            print(f"⚠️ Root directory not found: {root_dir}")
            continue

        for repo in os.listdir(root_dir):
            repo_path = os.path.join(root_dir, repo)
            if not os.path.isdir(repo_path) or not is_git_repo(repo_path):
                continue

            found_repos = True

            # 1) Status first (key fix)
            status_lines = git_status_porcelain(repo_path)

            if not status_lines:
                print(f"⚪ {repo}: Clean working tree")
                continue

            staged = has_staged_changes(status_lines)
            unstaged = has_unstaged_changes(status_lines)

            console.print(f"\n📦 Repo: [bold green]{repo}[/]")
            if unstaged and not staged:
                print("🟡 Changes detected but nothing staged yet.")
                print("   Tip: we need staged changes to build commit message from --cached.")

                choice = input("➕ Stage ALL changes (git add -A) ? (y/n): ").strip().lower()
                if choice == "y":
                    run_command(["git", "add", "-A"], cwd=repo_path)
                    staged = True
                else:
                    print("⏭️ Skipped (nothing staged).")
                    continue

            if not staged:
                print("⏭️ Skipped (no staged changes).")
                continue

            # 2) Now we can read cached diff
            diff_content = get_diff_content_cached(repo_path)
            if not diff_content:
                print(f"⚪ {repo}: No staged diff to commit")
                continue

            files = get_modified_files_names_cached(repo_path)

            # 3) Generate message (Ollama first, fallback second)
            commit_message = generate_commit_message_with_ollama(repo, files, diff_content)

            if not commit_message:
                # fallback heuristic
                commit_type = detect_commit_type_from_diff(diff_content)
                date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                if files:
                    title_keywords = " and ".join(files[:2])
                    commit_message = f"{commit_type}: update {title_keywords} ({date_str})"
                else:
                    commit_message = f"{commit_type}: auto commit based on diff analysis ({date_str})"

            # 4) Preview
            print("\n--- Preview of commit message ---\n")
            print(commit_message)
            print("\n--- End preview ---\n")

            user_input = input("✍️ Do you want to commit this change? (y/n): ").strip().lower()
            if user_input != "y":
                print("⏹️ Skipped commit.")
                continue

            with console.status("[bold green]Committing changes...[/]", spinner="dots"):
                ok = commit_with_message(repo_path, commit_message)
                if not ok:
                    continue
                results["committed"] += 1

            print("✅ Commit done.\n")

            push_input = input(f"📤 Do you want to push to {DEFAULT_BRANCH}? (y/n): ").strip().lower()
            if push_input == "y":
                with console.status("[bold cyan]Pushing...[/]", spinner="dots"):
                    res = run_command(["git", "push", "origin", DEFAULT_BRANCH], cwd=repo_path)
                    if res.returncode != 0:
                        print(f"❌ git push failed:\n{(res.stderr or '').strip()}")
                    else:
                        results["pushed"] += 1
                        print(f"🚀 Pushed to {DEFAULT_BRANCH}\n")
            else:
                print("⏭️ Skipped git push")

        if not found_repos:
            print(f"⚠️ No repositories found in {root_dir}")

    return results


if __name__ == "__main__":
    auto_commit_all_repos(ROOT_DIRS)
