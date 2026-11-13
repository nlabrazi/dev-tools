from rich.console import Console
from rich.prompt import Prompt

from utils.console import ui_panel, ui_table

console = Console()
MAINTENANCE_MENU_CHOICES = ("1", "2", "3", "q")


def render_maintenance_menu(repo_name: str) -> None:
    table = ui_table(
        title=f"Dependency Maintenance · {repo_name}",
        caption="Read-only audit and Ollama-assisted Renovate PR analysis (Phase 1).",
    )
    table.add_column("Key", justify="center", style="bold cyan", no_wrap=True)
    table.add_column("Action", style="bold white", no_wrap=True)
    table.add_column("Description", style="white")
    table.add_row("1", "Full Audit & PR Analysis", "Audit stack, Renovate, version matrix, CI and analyze open PRs with Ollama.")
    table.add_row("2", "Inspect Specific PR", "Analyze any PR number (open or merged) with Ollama for breaking changes.")
    table.add_row("3", "Export Audit Report", "Save the audit report as JSON and Markdown.")
    table.add_row("q", "Back", "Return to the main menu.")
    console.print(table)


def ask_maintenance_action() -> str:
    return Prompt.ask(
        "\n[bold white]Maintenance action[/]",
        choices=list(MAINTENANCE_MENU_CHOICES),
        default="1",
        show_choices=False,
    ).lower()


def run_dependency_maintenance_for_repo(repo_name: str, repo_path: str) -> None:
    while True:
        render_maintenance_menu(repo_name)
        choice = ask_maintenance_action()
        if choice == "q":
            break

        console.print(
            ui_panel(
                f"[bold white]Repository:[/] [cyan]{repo_name}[/]\n[dim]{repo_path}[/]",
                title="Dependency Maintenance",
                compact=True,
            )
        )
