import os
import time

from rich import box
from rich.console import Console
from rich.live import Live
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


def is_clear_screen_disabled() -> bool:
    return os.getenv("DEVTOOLS_NO_CLEAR", "").strip().lower() in {"1", "true", "yes", "on"}


def clear_screen(
    target_console: Console | None = None,
    *,
    animated: bool = True,
    label: str | None = None,
    duration: float = 0.14,
) -> bool:
    """Clear the visible terminal screen with a short subtle sweep animation.

    No-op when stdout is not an interactive terminal or when DEVTOOLS_NO_CLEAR=1.
    Returns True if the screen was cleared, False otherwise.
    """
    out = target_console or console
    if is_clear_screen_disabled() or not out.is_terminal:
        return False

    if animated and duration > 0:
        frames = 6
        step_delay = duration / frames
        width = max(16, min(out.size.width - 4, 48))
        suffix = f"  [dim]{label}[/]" if label else ""
        with Live(console=out, transient=True, refresh_per_second=60) as live:
            for step in range(1, frames + 1):
                filled = int(width * step / frames)
                bar = f"[bold cyan]{'━' * filled}[/][dim]{'─' * (width - filled)}[/]"
                live.update(f"  {bar}{suffix}")
                time.sleep(step_delay)

    out.clear()
    return True
