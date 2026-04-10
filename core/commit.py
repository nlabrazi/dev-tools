import os
from rich.console import Console

from utils.common import describe_command_failure, env_int, is_dry_run
from utils.console import ask_yes_no
from utils.git import git_command, git_output
from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE, ROOT_DIRS
from core.commit_message import generate_commit_message
from core.repositories import iter_git_repositories

console = Console()
DEFAULT_COMMIT_DIFF_MAX_CHARS = 4500


def get_commit_diff_max_chars() -> int:
    return env_int("OLLAMA_MAX_DIFF_CHARS", DEFAULT_COMMIT_DIFF_MAX_CHARS, minimum=800)


def _git_output_or_report(
    path: str,
    args: list[str],
    *,
    context: str,
    max_output_chars: int | None = None,
) -> str | None:
    result = git_command(
        path,
        args,
        silent=True,
        max_output_chars=max_output_chars,
    )
    if result.returncode != 0:
        print(f"❌ {context} failed:\n{describe_command_failure(result)}")
        return None
    return (result.stdout or "").rstrip("\n")


def git_status_porcelain(path: str) -> list[str] | None:
    """
    Returns lines like:
      ' M file.txt' (unstaged)
      'M  file.txt' (staged)
      '?? file.txt' (untracked)
    """
    out = _git_output_or_report(path, ["status", "--porcelain"], context="git status --porcelain")
    if out is None:
        return None
    lines = [l.rstrip("\n") for l in out.splitlines() if l.strip()]
    return lines


def get_current_branch(path: str) -> str:
    return git_output(path, ["branch", "--show-current"])


def resolve_auto_commit_target(path: str) -> tuple[str, str] | None:
    target_branch = (DEFAULT_HEAD_BRANCH or "").strip()
    target_remote = (DEFAULT_REMOTE or "").strip()

    if not target_branch:
        print("❌ Auto-commit target branch is empty. Check DEVTOOLS_HEAD_BRANCH.")
        return None
    if not target_remote:
        print("❌ Auto-commit remote is empty. Check DEVTOOLS_REMOTE.")
        return None

    current_branch = get_current_branch(path)
    if not current_branch:
        print("❌ Could not resolve the current branch. Auto-commit skipped.")
        return None
    if current_branch != target_branch:
        print(
            f"⚠️ Auto-commit expects branch '{target_branch}', current branch is '{current_branch}'. "
            "Repo skipped to avoid committing on one branch and pushing another."
        )
        return None

    remote_check = git_command(path, ["remote", "get-url", target_remote], silent=True)
    if remote_check.returncode != 0:
        print(f"❌ Remote '{target_remote}' is not configured for this repo. Auto-commit skipped.")
        return None

    return target_remote, target_branch


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


def get_diff_content_cached(path: str) -> str | None:
    return _git_output_or_report(
        path,
        ["diff", "--cached", "--no-ext-diff"],
        context="git diff --cached",
        max_output_chars=get_commit_diff_max_chars(),
    )


def get_diff_content_worktree(path: str) -> str | None:
    return _git_output_or_report(
        path,
        ["diff", "--no-ext-diff"],
        context="git diff",
        max_output_chars=get_commit_diff_max_chars(),
    )


def get_modified_files_names_cached(path: str) -> list[str] | None:
    out = _git_output_or_report(path, ["diff", "--cached", "--name-only"], context="git diff --cached --name-only")
    if out is None:
        return None
    return out.splitlines() if out else []


def get_modified_files_names_worktree(path: str) -> list[str] | None:
    out = _git_output_or_report(path, ["diff", "--name-only"], context="git diff --name-only")
    if out is None:
        return None
    return out.splitlines() if out else []


def commit_with_message(repo_path: str, full_message: str) -> str:
    """
    Commits with header + body without losing formatting.
    We do: git commit -m <header> -m <body>
    """
    lines = [l.rstrip() for l in full_message.splitlines()]
    header = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip()

    if not header:
        print("❌ Empty commit header, abort.")
        return "failed"

    if is_dry_run():
        print("🧪 Dry-run: would create the commit shown above.")
        return "dry-run"

    cmd = ["git", "commit", "-m", header]
    if body:
        cmd += ["-m", body]

    res = git_command(repo_path, cmd[1:])
    if res.returncode != 0:
        print(f"❌ git commit failed:\n{describe_command_failure(res)}")
        return "failed"
    return "committed"


def push_head_to_branch(repo_path: str, remote: str, branch: str) -> str:
    if is_dry_run():
        print(f"🧪 Dry-run: would push HEAD to {remote}/{branch}.")
        return "dry-run"

    refspec = f"HEAD:refs/heads/{branch}"
    res = git_command(repo_path, ["push", remote, refspec])
    if res.returncode != 0:
        print(f"❌ git push failed:\n{describe_command_failure(res)}")
        return "failed"
    return "pushed"


def auto_commit_all_repos(root_dirs: list[str]):
    print(f"\n🔄 Scanning repos in: {', '.join(root_dirs)}\n")
    results = {"committed": 0, "pushed": 0}

    for root_dir in root_dirs:
        console.print(f"\n📂 [bold yellow]Scanning root directory:[/] {root_dir}\n")
        found_repos = False

        if not os.path.isdir(root_dir):
            print(f"⚠️ Root directory not found: {root_dir}")
            continue

        for repo, repo_path in iter_git_repositories(root_dir):
            found_repos = True
            use_worktree_diff = False

            # 1) Status first (key fix)
            status_lines = git_status_porcelain(repo_path)
            if status_lines is None:
                continue

            if not status_lines:
                print(f"⚪ {repo}: Clean working tree")
                continue

            staged = has_staged_changes(status_lines)
            unstaged = has_unstaged_changes(status_lines)

            console.print(f"\n📦 Repo: [bold green]{repo}[/]")
            target = resolve_auto_commit_target(repo_path)
            if not target:
                continue
            target_remote, target_branch = target
            print(f"🎯 Auto-commit target: {target_remote}/{target_branch}")

            if unstaged and not staged:
                print("🟡 Changes detected but nothing staged yet.")
                print("   Tip: we need staged changes to build commit message from --cached.")

                choice = ask_yes_no("➕ Stage ALL changes (git add -A) ?", default="n")
                if choice:
                    if is_dry_run():
                        print("🧪 Dry-run: would stage all changes with git add -A.")
                        use_worktree_diff = True
                    else:
                        add_result = git_command(repo_path, ["add", "-A"])
                        if add_result.returncode != 0:
                            print(f"❌ git add failed:\n{describe_command_failure(add_result)}")
                            continue
                    staged = True
                else:
                    print("⏭️ Skipped (nothing staged).")
                    continue

            if not staged:
                print("⏭️ Skipped (no staged changes).")
                continue

            # 2) Now we can read cached diff
            diff_content = get_diff_content_worktree(repo_path) if use_worktree_diff else get_diff_content_cached(repo_path)
            if diff_content is None:
                continue
            if not diff_content:
                print(f"⚪ {repo}: No staged diff to commit")
                continue

            files = (
                get_modified_files_names_worktree(repo_path)
                if use_worktree_diff
                else get_modified_files_names_cached(repo_path)
            )
            if files is None:
                continue

            # 3) Generate message (LLM first, heuristic fallback second)
            commit_message = generate_commit_message(repo, files, diff_content)

            # 4) Preview
            print("\n--- Preview of commit message ---\n")
            print(commit_message)
            print("\n--- End preview ---\n")

            user_input = ask_yes_no("✍️ Do you want to commit this change?", default="n")
            if not user_input:
                print("⏹️ Skipped commit.")
                continue

            with console.status("[bold green]Committing changes...[/]", spinner="dots"):
                commit_status = commit_with_message(repo_path, commit_message)
                if commit_status == "failed":
                    continue
                if commit_status == "committed":
                    results["committed"] += 1

            if commit_status == "committed":
                print("✅ Commit done.\n")
            else:
                print("🧪 Dry-run: commit skipped.\n")

            push_input = ask_yes_no(
                f"📤 Do you want to push current HEAD to {target_remote}/{target_branch} ?",
                default="n",
            )
            if push_input:
                with console.status("[bold cyan]Pushing...[/]", spinner="dots"):
                    push_status = push_head_to_branch(repo_path, target_remote, target_branch)
                    if push_status == "pushed":
                        results["pushed"] += 1
                        print(f"🚀 Pushed HEAD to {target_remote}/{target_branch}\n")
                    elif push_status == "dry-run":
                        print(f"🧪 Dry-run: push skipped for {target_remote}/{target_branch}\n")
            else:
                print("⏭️ Skipped git push")

        if not found_repos:
            print(f"⚠️ No repositories found in {root_dir}")

    return results


if __name__ == "__main__":
    auto_commit_all_repos(ROOT_DIRS)
