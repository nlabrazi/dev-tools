from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
UI_TONES = {
    "info": "cyan",
    "success": "green",
    "warning": "yellow",
    "danger": "red",
    "accent": "magenta",
    "muted": "bright_black",
}


def ui_panel(
    content: str,
    *,
    title: str,
    tone: str = "info",
    compact: bool = False,
) -> Panel:
    style = UI_TONES.get(tone, UI_TONES["info"])
    options = {
        "title": f"[bold {style}]{title}[/]",
        "border_style": style,
        "padding": (1, 2),
    }
    if compact:
        return Panel.fit(content, **options)
    return Panel(content, **options)


def ui_table(
    *,
    title: str | None = None,
    caption: str | None = None,
    show_lines: bool = False,
) -> Table:
    return Table(
        title=f"[bold cyan]{title}[/]" if title else None,
        caption=f"[dim]{caption}[/]" if caption else None,
        caption_justify="left",
        box=box.ROUNDED,
        show_lines=show_lines,
        header_style="bold cyan",
        border_style="bright_black",
    )


def ask_yes_no(question: str, default: str = "n") -> bool:
    """
    Ask a styled yes/no question with magenta (y/n).
    Returns True if yes, False otherwise.

    default:
        "y" -> default yes
        "n" -> default no
    """
    default = default.lower()
    suffix = "Y/n" if default == "y" else "y/N"

    console.print(
        f"[white]{question}[/] [bold magenta]({suffix})[/]: ",
        end=""
    )

    raw = input().strip().lower()

    if raw == "":
        raw = default

    return raw == "y"
