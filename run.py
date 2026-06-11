import argparse
import os
from pathlib import Path

from pyfiglet import figlet_format
from rich import print
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

console = Console()
MENU_CHOICES = ("1", "2", "3", "4", "5", "q")


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
    from core.review import DEFAULT_REVIEW_DIFF_MAX_CHARS
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
  OLLAMA_MAX_REVIEW_DIFF_CHARS  Diff chars sent to review prompt. Default: {DEFAULT_REVIEW_DIFF_MAX_CHARS}
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


def render_main_menu(
    *,
    mode_label: str,
    root_dirs: list[str],
    head_branch: str,
    base_branch_strategy: str,
) -> None:
    root_count = len(root_dirs)
    root_label = "root" if root_count == 1 else "roots"

    console.print(
        Panel(
            (
                "[bold white]Choose a workflow and keep moving.[/]\n"
                f"[dim]Mode:[/] {mode_label}  "
                f"[dim]Head branch:[/] [cyan]{head_branch}[/]  "
                f"[dim]Repository roots:[/] [cyan]{root_count} {root_label}[/]\n"
                f"[dim]Base branch strategy:[/] {base_branch_strategy}"
            ),
            title="[bold cyan]Dev Tools Control Deck[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    table = Table(
        box=box.ROUNDED,
        show_lines=False,
        header_style="bold cyan",
        border_style="bright_black",
    )
    table.add_column("Key", justify="center", style="bold cyan", no_wrap=True)
    table.add_column("Workflow", style="bold white", no_wrap=True)
    table.add_column("What it does", style="white")
    table.add_column("Status", justify="center", no_wrap=True)

    table.add_row("1", "Auto Commit", "Scan repositories, build commit messages, then commit/push after confirmation.", "[green]Ready[/]")
    table.add_row("2", "Merge", "Create and auto-merge release PRs into resolved base branches.", "[green]Ready[/]")
    table.add_row("3", "Review Code", "Prepare AI-assisted code comments for changed code.", "[yellow]Soon[/]")
    table.add_row("4", "Changelog", "Update changelogs from Conventional Commits.", "[green]Ready[/]")
    table.add_row("5", "Sync", "Checkout and fast-forward local base branches.", "[green]Ready[/]")
    table.add_row("q", "Quit", "Leave the tool without running another workflow.", "[dim]Exit[/]")

    console.print(table)


def ask_main_action() -> str:
    return Prompt.ask(
        "\n[bold white]What do you want to do?[/]",
        choices=list(MENU_CHOICES),
        default="q",
        show_choices=False,
    ).lower()


def show_review_placeholder() -> None:
    section_title("Review Code", "🔎")
    console.print(
        Panel(
            (
                "[bold yellow]AI code review is coming next.[/]\n\n"
                "This menu entry is intentionally wired now so the upcoming review workflow has a stable place.\n"
                "Next steps will add Git context selection, Ollama suggestions, preview, and safe application."
            ),
            title="[bold yellow]Placeholder[/]",
            border_style="yellow",
            padding=(1, 2),
        )
    )


def run_selected_action(
    choice: str,
    *,
    root_dirs: list[str],
    head_branch: str,
    base_branch_strategy: str,
) -> bool:
    if choice == "q":
        return False

    if choice == "1":
        from core.commit import auto_commit_all_repos

        section_title(f"Auto-Commit {head_branch}", "🔧")
        auto_commit_all_repos(root_dirs)
        return True

    if choice == "2":
        import core.merge as merge

        section_title("Merge Into Base Branches", "🔁")
        console.print(Panel.fit(f"Strategy: {base_branch_strategy}", border_style="cyan"))
        merge.main(root_dirs)
        return True

    if choice == "3":
        show_review_placeholder()
        return True

    if choice == "4":
        from core.changelog import update_all_repos_interactive

        section_title("Update Changelogs", "📝")
        update_all_repos_interactive(root_dirs)
        return True

    if choice == "5":
        import core.sync as sync

        section_title("Sync Base Branches", "⏳")
        console.print(Panel.fit(sync.describe_sync_plan(), border_style="cyan"))
        sync.main(root_dirs)
        return True

    return True


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)

    from utils.common import set_dry_run
    from core.config import DEFAULT_HEAD_BRANCH, DEFAULT_REMOTE, ROOT_DIRS, describe_base_branch_strategy

    if args.dry_run:
        set_dry_run(True)
        console.print("\n🚀 [bold cyan][DRY-RUN MODE ENABLED][/]\n")
    elif args.prod:
        set_dry_run(False)
        console.print("\n🚀 [bold green][PRODUCTION MODE - REAL EXECUTION][/]\n")

    # Banner
    print(f"\n[bold green]{figlet_format('Dev Tools', font='slant')}[/]")

    mode_label = "DRY RUN" if args.dry_run else "PRODUCTION"
    base_branch_strategy = describe_base_branch_strategy(DEFAULT_REMOTE)

    while True:
        render_main_menu(
            mode_label=mode_label,
            root_dirs=ROOT_DIRS,
            head_branch=DEFAULT_HEAD_BRANCH,
            base_branch_strategy=base_branch_strategy,
        )
        choice = ask_main_action()
        should_continue = run_selected_action(
            choice,
            root_dirs=ROOT_DIRS,
            head_branch=DEFAULT_HEAD_BRANCH,
            base_branch_strategy=base_branch_strategy,
        )
        if not should_continue:
            break

    print(f"\n[bold cyan]{figlet_format('All Done!', font='slant')}[/]")

if __name__ == "__main__":
    main()
