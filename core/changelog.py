import os
from datetime import datetime
from collections import defaultdict

from rich import print
from rich.panel import Panel
from rich.console import Console

from core.config import CHANGELOG_FILENAME, DEFAULT_REMOTE, ROOT_DIRS
from core.conventional_commits import parse_conventional_commit
from core.repositories import iter_git_repositories
from utils.common import prepend_text_file, run_command, run_command_checked
from utils.console import ask_yes_no

console = Console()

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


def run_git_command(path: str, args: list[str]) -> str:
    result = run_command(["git"] + args, cwd=path, silent=True)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def get_current_branch(path: str) -> str:
    return run_git_command(path, ["branch", "--show-current"])


def get_staged_files(path: str) -> list[str]:
    output = run_git_command(path, ["diff", "--cached", "--name-only"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def get_last_tag(path: str) -> str | None:
    tags = run_git_command(path, ["tag", "--sort=-creatordate"]).splitlines()
    return tags[0] if tags else None


def get_commits_since_tag(path: str, last_tag: str | None = None) -> list[str]:
    range_spec = f"{last_tag}..HEAD" if last_tag else "HEAD"
    log_output = run_git_command(path, ["log", range_spec, "--pretty=format:%s", "--no-merges"])
    commits = log_output.splitlines()
    return [
        commit for commit in commits
        if commit and not any(keyword in commit.lower() for keyword in EXCLUDED_KEYWORDS)
    ]


def classify_commits(commits: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    categorized: defaultdict[str, list[str]] = defaultdict(list)
    uncategorized: list[str] = []

    for commit in commits:
        parsed = parse_conventional_commit(commit)
        if not parsed:
            uncategorized.append(commit)
            continue
        categorized[parsed.normalized_type].append(parsed.subject)

    return categorized, uncategorized


def generate_changelog(commits: list[str], version_label: str) -> str:
    categorized, uncategorized = classify_commits(commits)
    block = [f"## [{version_label}] - {datetime.now().strftime('%Y-%m-%d')}", ""]

    for commit_type in EMOJI_MAP:
        messages = categorized.get(commit_type, [])
        if not messages:
            continue
        emoji = EMOJI_MAP[commit_type]
        block.append(f"### {emoji} {commit_type.capitalize()}")
        for msg in messages:
            block.append(f"- {msg}")
        block.append("")

    if uncategorized:
        block.append("### 🔖 Others")
        for msg in uncategorized:
            block.append(f"- {msg}")
        block.append("")

    return "\n".join(block)


def update_changelog(repo_path: str, changelog_content: str) -> bool:
    changelog_path = os.path.join(repo_path, CHANGELOG_FILENAME)
    return prepend_text_file(changelog_path, changelog_content)


def commit_and_push_changelog(repo_path: str) -> bool:
    branch = get_current_branch(repo_path)
    if not branch:
        print("❌ Could not resolve the current branch. Changelog push skipped.")
        return False

    with console.status("[bold green]Committing and pushing changelog...", spinner="dots"):
        try:
            run_command_checked(
                ["git", "add", "--", CHANGELOG_FILENAME],
                cwd=repo_path,
                context=f"stage {CHANGELOG_FILENAME}",
            )
            staged_files = set(get_staged_files(repo_path))
            if not staged_files:
                print("⚪ No changelog changes staged.")
                return False
            if staged_files != {CHANGELOG_FILENAME}:
                extras = ", ".join(sorted(staged_files - {CHANGELOG_FILENAME}))
                print(f"⚠️ Other staged files detected ({extras}). Commit aborted to avoid bundling unrelated changes.")
                return False

            message = f"docs: update changelog ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
            run_command_checked(
                ["git", "commit", "-m", message],
                cwd=repo_path,
                context="commit changelog",
            )
            run_command_checked(
                ["git", "push", DEFAULT_REMOTE, branch],
                cwd=repo_path,
                context=f"push changelog to {DEFAULT_REMOTE}/{branch}",
            )
        except Exception as exc:
            print(f"❌ {exc}")
            return False

    print("[green]✅ Changelog committed and pushed.[/green]")
    return True


def update_all_repos_interactive(root_dirs: list[str]) -> None:
    print("\n🔄 Scanning repos for changelog updates\n")

    for root_dir in root_dirs:
        print(f"\n📂 Scanning root directory: {root_dir}\n")
        if not os.path.isdir(root_dir):
            print(f"⚠️ Root directory not found: {root_dir}")
            continue

        found_repos = False
        for repo, repo_path in iter_git_repositories(root_dir):
            found_repos = True

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
                border_style="cyan",
            )
            console.print(repo_panel)

            if not ask_yes_no("✍️ Write changelog ?", default="n"):
                print("⏹️ Skipped changelog.")
                continue

            if update_changelog(repo_path, changelog_preview):
                print(f"✅ Changelog updated for {repo}")
            else:
                print(f"🧪 Dry-run active or write skipped for {repo}")

            if ask_yes_no("📤 Do you want to commit and push the changelog ?", default="n"):
                commit_and_push_changelog(repo_path)

        if not found_repos:
            print(f"⚠️ No repositories found in {root_dir}")


if __name__ == "__main__":
    update_all_repos_interactive(ROOT_DIRS)
