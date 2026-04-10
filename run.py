import argparse
import os
from pathlib import Path

from pyfiglet import figlet_format
from rich import print
from rich.console import Console
from rich.panel import Panel

console = Console()


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


def load_env_file(path: str | Path | None = None) -> None:
    env_path = Path(path) if path is not None else Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE, ROOT_DIRS, describe_base_branch_strategy
    from core.ollama import (
        DEFAULT_DEBUG_MAX_CHARS,
        DEFAULT_HOST,
        DEFAULT_MODEL,
        DEFAULT_TIMEOUT,
    )
    from utils.git import DEFAULT_GIT_TIMEOUT

    root_dirs_preview = ", ".join(ROOT_DIRS) if ROOT_DIRS else "(none)"
    help_epilog = f"""Examples:
  py run.py --prod
  py run.py --dry-run
  py run.py --help

Main environment variables:
  DEVTOOLS_ROOT_DIRS            Repositories roots to scan. Current value: {root_dirs_preview}
  DEVTOOLS_REMOTE               Git remote name. Current default: {DEFAULT_REMOTE}
  DEVTOOLS_HEAD_BRANCH          Integration branch. Current default: {DEFAULT_HEAD_BRANCH}
  DEVTOOLS_BASE_BRANCH          Force a base branch instead of resolving origin/HEAD
  DEVTOOLS_GIT_TIMEOUT          Git command timeout in seconds. Default: {DEFAULT_GIT_TIMEOUT}
  GH_PR_MERGE_TIMEOUT           Wait for effective PR merge. Default: 90

Ollama:
  ENABLE_OLLAMA                 1 enabled, 0 disabled
  OLLAMA_HOST                   Default: {DEFAULT_HOST}
  OLLAMA_MODEL                  Default: {DEFAULT_MODEL}
  OLLAMA_TIMEOUT                Default: {DEFAULT_TIMEOUT}
  OLLAMA_NUM_CTX                Optional context size >= 512
  OLLAMA_MAX_FILES              Files sent to commit prompt. Default: 80
  OLLAMA_MAX_DIFF_CHARS         Diff chars sent to commit prompt. Default: 4500
  OLLAMA_MAX_PR_SUMMARY_CHARS   Summary chars sent to PR prompt. Default: 5000
  OLLAMA_ALLOW_REMOTE           Allow non-local Ollama host
  OLLAMA_ALLOW_REMOTE_CONTEXT   Allow sending git diffs/commit summaries to remote host
  OLLAMA_DEBUG                  0, 1, or full
  OLLAMA_DEBUG_MAX_CHARS        Truncated debug preview length. Default: {DEFAULT_DEBUG_MAX_CHARS}

Notes:
  - run.py auto-loads a local .env file if present.
  - shell environment variables override .env values.
  - Base branch strategy: {describe_base_branch_strategy(DEFAULT_REMOTE)}."""

    parser = argparse.ArgumentParser(
        description="Interactive runner for multi-repo Git workflows.",
        epilog=help_epilog,
        formatter_class=HelpFormatter,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Simulate actions without mutating repositories")
    group.add_argument("--prod", action="store_true", help="Execute real actions")

    return parser


def section_title(title: str, emoji: str) -> None:
    print("\n")
    console.print(Panel.fit(f"{emoji}  {title.upper()}", style="bold green", border_style="cyan"))


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)

    from utils.common import set_dry_run
    from utils.console import ask_yes_no
    from core.changelog import update_all_repos_interactive
    from core.commit import auto_commit_all_repos
    from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE, ROOT_DIRS, describe_base_branch_strategy
    import core.merge as merge
    import core.sync as sync

    if args.dry_run:
        set_dry_run(True)
        console.print("\n🚀 [bold cyan][DRY-RUN MODE ENABLED][/]\n")
    elif args.prod:
        set_dry_run(False)
        console.print("\n🚀 [bold green][PRODUCTION MODE - REAL EXECUTION][/]\n")

    # Banner
    print(f"\n[bold green]{figlet_format('Dev Tools', font='slant')}[/]")

    # --- STEP 1: AUTO-COMMIT ---
    section_title(f"Auto-commit {DEFAULT_HEAD_BRANCH}", "🔧")
    if ask_yes_no("Browse repos and run auto-commit ?", default="n"):
        auto_commit_all_repos(ROOT_DIRS)

    # --- STEP 2: MERGE ---
    section_title("Merge Into Base Branches", "🔁")
    merge_prompt = (
        f"Merge {DEFAULT_HEAD_BRANCH} into each repo base branch ? "
        f"Strategy: {describe_base_branch_strategy(DEFAULT_REMOTE)}."
    )
    if ask_yes_no(merge_prompt, default="n"):
        merge.main(ROOT_DIRS)

    # --- STEP 3: CHANGELOG ---
    section_title("Update changelogs", "📝")
    if ask_yes_no("Update changelogs ?", default="n"):
        update_all_repos_interactive(ROOT_DIRS)

    # --- STEP 4: SYNC BASE BRANCHES ---
    section_title("Sync Base Branches", "⏳")
    sync_prompt = sync.describe_sync_plan()
    if ask_yes_no(sync_prompt, default="n"):
        sync.main(ROOT_DIRS)

    print(f"\n[bold cyan]{figlet_format('All Done!', font='slant')}[/]")

if __name__ == "__main__":
    main()
