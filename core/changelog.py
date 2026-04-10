import os
import re
from datetime import datetime
from collections import defaultdict
from pathlib import Path

from rich import print
from rich.panel import Panel
from rich.console import Console

from core.config import CHANGELOG_FILENAME, DEFAULT_REMOTE, ROOT_DIRS
from core.conventional_commits import parse_conventional_commit
from core.repositories import iter_git_repositories
from core.versioning import compute_next_version_from_messages, get_last_semver_tag
from utils.common import is_dry_run, run_command, run_command_checked
from utils.console import ask_yes_no

console = Console()
CHANGELOG_TITLE = "# 📅 CHANGELOG"
CHANGELOG_SECTION_RE = re.compile(
    r"^## \[(?P<label>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)

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
    return get_last_semver_tag(path)


def read_changelog(repo_path: str) -> str:
    changelog_path = Path(repo_path) / CHANGELOG_FILENAME
    if not changelog_path.exists():
        return ""
    return changelog_path.read_text(encoding="utf-8")


def write_changelog(repo_path: str, content: str) -> None:
    changelog_path = Path(repo_path) / CHANGELOG_FILENAME
    changelog_path.write_text(content, encoding="utf-8")


def get_changelog_file_status(path: str) -> list[str]:
    output = run_git_command(path, ["status", "--porcelain", "--", CHANGELOG_FILENAME])
    return [line.rstrip() for line in output.splitlines() if line.strip()]


def changelog_has_any_changes(path: str) -> bool:
    return bool(get_changelog_file_status(path))


def unstage_changelog(path: str) -> None:
    run_command_checked(
        ["git", "restore", "--staged", "--", CHANGELOG_FILENAME],
        cwd=path,
        context=f"unstage {CHANGELOG_FILENAME}",
    )


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


def has_changelog_section(content: str, version_label: str) -> bool:
    return any(match.group("label") == version_label for match in CHANGELOG_SECTION_RE.finditer(content or ""))


def split_changelog_sections(content: str) -> tuple[str, list[tuple[str, str]]]:
    normalized = (content or "").replace("\r\n", "\n")
    matches = list(CHANGELOG_SECTION_RE.finditer(normalized))
    if not matches:
        return normalized, []

    prefix = normalized[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        label = match.group("label").strip()
        section_text = normalized[start:end].strip()
        sections.append((label, section_text))

    return prefix, sections


def compose_changelog(prefix: str, sections: list[tuple[str, str]]) -> str:
    prefix_block = (prefix or "").strip()
    if not prefix_block:
        prefix_block = CHANGELOG_TITLE
    elif CHANGELOG_TITLE not in prefix_block:
        prefix_block = f"{CHANGELOG_TITLE}\n\n{prefix_block}"

    parts = [prefix_block]
    for _, section_text in sections:
        cleaned = section_text.strip()
        if cleaned:
            parts.append(cleaned)

    return "\n\n".join(parts).rstrip() + "\n"


def upsert_changelog_section(existing_content: str, section_content: str, version_label: str) -> str:
    prefix, sections = split_changelog_sections(existing_content)
    remaining_sections = [(label, text) for label, text in sections if label != version_label]

    insert_index = 0
    if version_label != "Unreleased":
        for index, (label, _) in enumerate(remaining_sections):
            if label == "Unreleased":
                insert_index = index + 1
                break

    remaining_sections.insert(insert_index, (version_label, section_content.strip()))
    return compose_changelog(prefix, remaining_sections)


def resolve_changelog_version_label(repo_path: str, commits: list[str], existing_content: str) -> str:
    if has_changelog_section(existing_content, "Unreleased"):
        return "Unreleased"

    last_tag = get_last_tag(repo_path)
    if not last_tag:
        return "Unreleased"

    return compute_next_version_from_messages(repo_path, commits, default_first="v0.1.0")


def generate_changelog(commits: list[str], version_label: str) -> str:
    categorized, uncategorized = classify_commits(commits)
    if version_label == "Unreleased":
        header = "## [Unreleased]"
    else:
        header = f"## [{version_label}] - {datetime.now().strftime('%Y-%m-%d')}"

    block = [header, ""]

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

    return "\n".join(block).rstrip() + "\n"


def update_changelog(repo_path: str, changelog_content: str, version_label: str) -> str:
    existing_content = read_changelog(repo_path)
    updated_content = upsert_changelog_section(existing_content, changelog_content, version_label)
    if updated_content == existing_content:
        return "noop"
    if is_dry_run():
        return "dry-run"

    write_changelog(repo_path, updated_content)
    return "updated"


def commit_and_push_changelog(repo_path: str) -> bool:
    branch = get_current_branch(repo_path)
    if not branch:
        print("❌ Could not resolve the current branch. Changelog push skipped.")
        return False

    if is_dry_run():
        print(
            f"🧪 Dry-run: would stage, commit and push {CHANGELOG_FILENAME} "
            f"to {DEFAULT_REMOTE}/{branch}."
        )
        return False

    staged_files = set(get_staged_files(repo_path))
    extras = staged_files - {CHANGELOG_FILENAME}
    if extras:
        extras_list = ", ".join(sorted(extras))
        print(f"⚠️ Other staged files detected ({extras_list}). Commit aborted to avoid bundling unrelated changes.")
        return False

    changelog_already_staged = CHANGELOG_FILENAME in staged_files
    if not changelog_already_staged and not changelog_has_any_changes(repo_path):
        print("⚪ No changelog changes to commit.")
        return False

    with console.status("[bold green]Committing and pushing changelog...", spinner="dots"):
        staged_by_us = False
        committed = False
        try:
            if not changelog_already_staged:
                run_command_checked(
                    ["git", "add", "--", CHANGELOG_FILENAME],
                    cwd=repo_path,
                    context=f"stage {CHANGELOG_FILENAME}",
                )
                staged_by_us = True

            staged_after = set(get_staged_files(repo_path))
            if staged_after != {CHANGELOG_FILENAME}:
                extras_after = ", ".join(sorted(staged_after - {CHANGELOG_FILENAME}))
                if staged_by_us:
                    unstage_changelog(repo_path)
                if staged_after:
                    print(
                        f"⚠️ Other staged files detected ({extras_after}). "
                        "Commit aborted to avoid bundling unrelated changes."
                    )
                else:
                    print("⚪ No changelog changes staged.")
                return False

            message = f"docs: update changelog ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
            run_command_checked(
                ["git", "commit", "-m", message],
                cwd=repo_path,
                context="commit changelog",
            )
            committed = True
            run_command_checked(
                ["git", "push", DEFAULT_REMOTE, branch],
                cwd=repo_path,
                context=f"push changelog to {DEFAULT_REMOTE}/{branch}",
            )
        except Exception as exc:
            if staged_by_us and not committed:
                try:
                    unstage_changelog(repo_path)
                except Exception:
                    pass
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
            existing_changelog = read_changelog(repo_path)
            commits = get_commits_since_tag(repo_path, last_tag)
            if not commits:
                print(f"⚪ {repo}: No new commits to update changelog")
                continue

            version_label = resolve_changelog_version_label(repo_path, commits, existing_changelog)
            changelog_preview = generate_changelog(commits, version_label)

            repo_panel = Panel.fit(
                changelog_preview,
                title=f"[bold green]{repo}[/]",
                subtitle=(
                    f"[bold blue]Last Release: {last_tag if last_tag else 'None'}"
                    f" | Target: {version_label}[/]"
                ),
                border_style="cyan",
            )
            console.print(repo_panel)

            if not ask_yes_no("✍️ Write changelog ?", default="n"):
                print("⏹️ Skipped changelog.")
                continue

            update_status = update_changelog(repo_path, changelog_preview, version_label)
            if update_status == "updated":
                print(f"✅ Changelog updated for {repo}")
            elif update_status == "dry-run":
                print(f"🧪 Dry-run: would update {CHANGELOG_FILENAME} for {repo}")
            elif update_status == "noop":
                print(f"⚪ {repo}: {CHANGELOG_FILENAME} already up to date")
            else:
                print(f"❌ Failed to update {CHANGELOG_FILENAME} for {repo}")

            if ask_yes_no("📤 Do you want to commit and push the changelog ?", default="n"):
                commit_and_push_changelog(repo_path)

        if not found_repos:
            print(f"⚠️ No repositories found in {root_dir}")


if __name__ == "__main__":
    update_all_repos_interactive(ROOT_DIRS)
