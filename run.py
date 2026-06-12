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
MENU_CHOICES = ("1", "2", "3", "4", "5", "6", "q")
REVIEW_TARGET_CHOICES = ("1", "2", "3", "4", "5", "q")


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
    from core.review import DEFAULT_REVIEW_DIFF_MAX_CHARS, DEFAULT_REVIEW_FILE_MAX_CHARS
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
  OLLAMA_MAX_REVIEW_FILE_CHARS  File chars sent to review prompt. Default: {DEFAULT_REVIEW_FILE_MAX_CHARS}
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
    table.add_row("3", "Review Code", "Explain selected code context in French without changing files.", "[cyan]Ready[/]")
    table.add_row(
        "4",
        "Comment Code",
        "Preview and optionally apply AI-assisted source comments.",
        "[green]Ready[/]",
    )
    table.add_row("5", "Changelog", "Update changelogs from Conventional Commits.", "[green]Ready[/]")
    table.add_row("6", "Sync", "Checkout and fast-forward local base branches.", "[green]Ready[/]")
    table.add_row("q", "Quit", "Leave the tool without running another workflow.", "[dim]Exit[/]")

    console.print(table)


def ask_main_action() -> str:
    return Prompt.ask(
        "\n[bold white]What do you want to do?[/]",
        choices=list(MENU_CHOICES),
        default="q",
        show_choices=False,
    ).lower()


def render_repository_picker(repositories: list[tuple[str, str]]) -> None:
    console.print(
        Panel(
            "Pick the repository that contains the code you want to inspect.",
            title="[bold cyan]Step 1/2: Repository[/]",
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
    table.add_column("Repository", style="bold white", no_wrap=True)
    table.add_column("Path", style="dim")

    for index, (repo_name, repo_path) in enumerate(repositories, start=1):
        table.add_row(str(index), repo_name, repo_path)
    table.add_row("q", "Back", "Return to main menu")

    console.print(table)


def ask_review_repository(repositories: list[tuple[str, str]]) -> tuple[str, str] | None:
    if not repositories:
        return None

    if len(repositories) == 1:
        repo_name, repo_path = repositories[0]
        console.print(
            Panel(
                f"[bold white]Repository:[/] [cyan]{repo_name}[/]\n[dim]{repo_path}[/]",
                title="[bold cyan]Repository Selected[/]",
                border_style="cyan",
                padding=(1, 2),
            )
        )
        return repositories[0]

    render_repository_picker(repositories)
    choices = [str(index) for index in range(1, len(repositories) + 1)] + ["q"]
    selected = Prompt.ask(
        "\n[bold white]Repository[/]",
        choices=choices,
        default="q",
        show_choices=False,
    ).lower()

    if selected == "q":
        return None
    return repositories[int(selected) - 1]


def render_review_target_menu() -> None:
    console.print(
        Panel(
            (
                "[bold cyan]1[/]  Current changes [dim](recommended)[/]\n"
                "   Best when you stopped coding and want to understand what is in progress.\n\n"
                "[bold cyan]2[/]  Staged changes\n"
                "   Inspect only what is already prepared for the next commit.\n\n"
                "[bold cyan]3[/]  Compare with a branch\n"
                "   Use this for a feature branch, for example main...HEAD.\n\n"
                "[bold cyan]4[/]  A specific commit/ref\n"
                "   Use this when you want to explain an older commit, for example HEAD~1.\n\n"
                "[bold cyan]5[/]  A specific file path\n"
                "   Focus on one file by typing its relative path.\n\n"
                "[bold cyan]q[/]  Back"
            ),
            title="[bold cyan]Step 2/2: Scope[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def ask_review_target() -> tuple[str, str | None] | None:
    from core.review import (
        REVIEW_TARGET_BRANCH,
        REVIEW_TARGET_COMMIT,
        REVIEW_TARGET_FILE,
        REVIEW_TARGET_STAGED,
        REVIEW_TARGET_WORKTREE,
    )

    render_review_target_menu()
    selected = Prompt.ask(
        "\n[bold white]Review scope[/]",
        choices=list(REVIEW_TARGET_CHOICES),
        default="1",
        show_choices=False,
    ).lower()

    if selected == "q":
        return None
    if selected == "1":
        return REVIEW_TARGET_WORKTREE, None
    if selected == "2":
        return REVIEW_TARGET_STAGED, None
    if selected == "3":
        ref = Prompt.ask("[bold white]Base branch[/]", default="main").strip()
        return REVIEW_TARGET_BRANCH, ref
    if selected == "5":
        path = Prompt.ask("[bold white]Relative file path[/]").strip()
        return REVIEW_TARGET_FILE, path

    ref = Prompt.ask("[bold white]Commit or ref[/]", default="HEAD~1").strip()
    return REVIEW_TARGET_COMMIT, ref


def render_review_setup(repo_name: str, context) -> None:
    file_count = len(context.files)
    file_label = "file" if file_count == 1 else "files"
    console.print(
        Panel(
            (
                f"[bold white]Repository:[/] [cyan]{repo_name}[/]\n"
                f"[bold white]Scope:[/] {context.label}\n"
                f"[bold white]Changed files:[/] [cyan]{file_count} {file_label}[/]\n\n"
                "[dim]Generating a French explanation only. Source files will not be modified.[/]"
            ),
            title="[bold cyan]Review Setup[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def render_code_review_explanation(repo_name: str, context, explanation) -> None:
    files_label = ", ".join(context.files[:5]) if context.files else "(none)"
    if len(context.files) > 5:
        files_label += f", +{len(context.files) - 5} more"

    console.print(
        Panel(
            (
                f"[bold white]Repository:[/] [cyan]{repo_name}[/]\n"
                f"[bold white]Scope:[/] {context.label}\n"
                f"[bold white]Files:[/] {files_label}\n\n"
                f"[bold cyan]{explanation.title or 'Explication du contexte'}[/]\n\n"
                f"{explanation.overview or 'Aucune synthèse fournie.'}\n\n"
                f"{explanation.technical_context or ''}"
            ).strip(),
            title="[bold cyan]Review Result[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    sections = [
        ("Important Files", explanation.important_files),
        ("Behavior", explanation.behavior),
        ("Points To Check", explanation.points_to_check),
        ("Risks", explanation.risks),
    ]
    for title, items in sections:
        if not items:
            continue
        body = "\n".join(f"- {item}" for item in items)
        console.print(
            Panel(
                body,
                title=f"[bold green]{title}[/]",
                border_style="green",
                padding=(1, 2),
            )
        )


def render_comment_plan(repo_name: str, context, plan) -> None:
    files_label = ", ".join(context.files[:5]) if context.files else "(none)"
    if len(context.files) > 5:
        files_label += f", +{len(context.files) - 5} more"

    console.print(
        Panel(
            (
                f"[bold white]Repository:[/] [cyan]{repo_name}[/]\n"
                f"[bold white]Target:[/] {context.label}\n"
                f"[bold white]Changed files:[/] {files_label}\n\n"
                f"{plan.summary or 'No summary provided.'}"
            ),
            title="[bold cyan]Comment Preview[/]",
            border_style="cyan",
            padding=(1, 2),
        )
    )

    if not plan.has_comments:
        console.print(
            Panel(
                "No useful code comments were proposed for this context.",
                title="[bold yellow]No Comments Proposed[/]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        return

    for index, suggestion in enumerate(plan.comments, start=1):
        console.print(
            Panel(
                (
                    f"[bold white]File:[/] [cyan]{suggestion.file}[/]\n"
                    f"[bold white]Placement:[/] {suggestion.placement} anchor\n"
                    f"[bold white]Anchor:[/] {suggestion.anchor}\n\n"
                    f"[bold white]Comment to insert:[/]\n{suggestion.comment}\n\n"
                    f"[bold white]Why:[/] {suggestion.reason or 'Not specified.'}"
                ),
                title=f"[bold green]Suggested Comment {index}[/]",
                border_style="green",
                padding=(1, 2),
            )
        )

    console.print(
        Panel(
            "Review each suggestion before applying. No source file has been modified yet.",
            title="[bold magenta]Confirmation Required[/]",
            border_style="magenta",
            padding=(1, 2),
        )
    )


def render_comment_application_report(report) -> None:
    table = Table(
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold cyan",
        border_style="bright_black",
    )
    table.add_column("File", style="bold white", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Details", style="white")

    status_styles = {
        "applied": "green",
        "dry-run": "cyan",
        "skipped": "yellow",
        "failed": "red",
    }
    for result in report.results:
        style = status_styles.get(result.status, "white")
        table.add_row(
            result.suggestion.file,
            f"[{style}]{result.status.upper()}[/]",
            result.message,
        )

    console.print(table)

    if report.modified_files:
        files = ", ".join(report.modified_files)
        console.print(
            Panel(
                f"Updated source files: {files}",
                title="[bold green]Comments Applied[/]",
                border_style="green",
                padding=(1, 2),
            )
        )
    elif report.simulated_files:
        files = ", ".join(report.simulated_files)
        console.print(
            Panel(
                f"Dry-run only. Files that would be updated: {files}",
                title="[bold cyan]Simulation Complete[/]",
                border_style="cyan",
                padding=(1, 2),
            )
        )
    else:
        console.print(
            Panel(
                "No source file was modified.",
                title="[bold yellow]No Changes Applied[/]",
                border_style="yellow",
                padding=(1, 2),
            )
        )


def run_review_workflow(root_dirs: list[str]) -> None:
    from core.repositories import iter_git_repositories
    from core.review import ReviewContextError, collect_review_context, generate_code_review

    section_title("Review Code", "🔎")

    repositories: list[tuple[str, str]] = []
    for root_dir in root_dirs:
        repositories.extend(iter_git_repositories(root_dir))

    if not repositories:
        console.print(
            Panel(
                "No Git repositories were found in the configured root directories.",
                title="[bold yellow]Nothing To Review[/]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        return

    selected_repo = ask_review_repository(repositories)
    if selected_repo is None:
        return

    repo_name, repo_path = selected_repo
    selected_target = ask_review_target()
    if selected_target is None:
        return

    target, ref = selected_target

    try:
        context = collect_review_context(repo_path, target, ref)
    except ReviewContextError as error:
        console.print(
            Panel(
                str(error),
                title="[bold red]Review Context Error[/]",
                border_style="red",
                padding=(1, 2),
            )
        )
        return

    render_review_setup(repo_name, context)

    with console.status("[bold cyan]Generating French review explanation...[/]", spinner="dots"):
        explanation = generate_code_review(repo_name, context)

    render_code_review_explanation(repo_name, context, explanation)


def run_comment_workflow(root_dirs: list[str]) -> None:
    from core.repositories import iter_git_repositories
    from core.review import (
        ReviewContextError,
        apply_review_comments,
        collect_review_context,
        generate_review_comments,
    )
    from utils.console import ask_yes_no

    section_title("Comment Code", "💬")

    repositories: list[tuple[str, str]] = []
    for root_dir in root_dirs:
        repositories.extend(iter_git_repositories(root_dir))

    if not repositories:
        console.print(
            Panel(
                "No Git repositories were found in the configured root directories.",
                title="[bold yellow]Nothing To Comment[/]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        return

    selected_repo = ask_review_repository(repositories)
    if selected_repo is None:
        return

    repo_name, repo_path = selected_repo
    selected_target = ask_review_target()
    if selected_target is None:
        return

    target, ref = selected_target

    try:
        context = collect_review_context(repo_path, target, ref)
    except ReviewContextError as error:
        console.print(
            Panel(
                str(error),
                title="[bold red]Comment Context Error[/]",
                border_style="red",
                padding=(1, 2),
            )
        )
        return

    with console.status("[bold cyan]Generating source comment suggestions...[/]", spinner="dots"):
        plan = generate_review_comments(repo_name, context)

    render_comment_plan(repo_name, context, plan)

    if not plan.has_comments:
        return
    if not ask_yes_no("Apply these comments to source files?", default="n"):
        console.print(
            Panel(
                "Comment application cancelled. No source file was modified.",
                title="[bold yellow]Cancelled[/]",
                border_style="yellow",
                padding=(1, 2),
            )
        )
        return

    report = apply_review_comments(repo_path, context, plan)
    render_comment_application_report(report)


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
        run_review_workflow(root_dirs)
        return True

    if choice == "4":
        run_comment_workflow(root_dirs)
        return True

    if choice == "5":
        from core.changelog import update_all_repos_interactive

        section_title("Update Changelogs", "📝")
        update_all_repos_interactive(root_dirs)
        return True

    if choice == "6":
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
