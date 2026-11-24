import os
import time
from collections.abc import Iterable

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

console = Console()

# Retro Amstrad CPC phosphor palette (default): bright green + yellow on dark terminal
UI_TONES = {
    "info": "bright_green",
    "success": "green",
    "warning": "bright_yellow",
    "danger": "red",
    "accent": "yellow",
    "muted": "dim",
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
        "title": f"[bold {style}] {title} [/]",
        "title_align": "left",
        "border_style": style,
        "box": box.SQUARE,
        "padding": (0, 1),
    }
    if compact:
        return Panel.fit(content, **options)
    return Panel(content, **options)


def ui_banner(
    breadcrumb: str,
    *,
    mode: str | None = None,
    status_items: Iterable[str] | None = None,
    tone: str = "info",
) -> Panel:
    """Compact 3-line retro Amstrad double-border header bar."""
    style = UI_TONES.get(tone, UI_TONES["info"])
    parts: list[str] = ["[bold bright_yellow]DEV-TOOLS[/]"]
    if mode:
        mode_style = "bold bright_yellow" if "DRY" in mode.upper() else "bold bright_green"
        parts.append(f"[{mode_style}][{mode}][/]")
    if breadcrumb:
        parts.append(f"[bold white]▸ {breadcrumb}[/]")
    if status_items:
        for item in status_items:
            if item:
                parts.append(item)

    line = "  [dim]│[/]  ".join(parts)
    return Panel(
        line,
        box=box.DOUBLE,
        border_style=style,
        padding=(0, 1),
    )


def ui_command_bar(commands: Iterable[tuple[str, str]]) -> str:
    """Single-line retro command bar: [1] Renovate  [2] Bump  ...  [q] Retour."""
    segments = [f"[bold bright_yellow]\\[{key}][/] [bright_green]{label}[/]" for key, label in commands]
    return "  ".join(segments)


def ui_step_line(label: str, *, status: str = "OK", detail: str = "") -> str:
    """Single-line technical step output (hides noisy tool stdout unless failed)."""
    upper = status.upper()
    if upper in {"OK", "PASS"}:
        badge = "[bold bright_green]OK[/]"
    elif upper in {"FAIL", "ERROR"}:
        badge = "[bold red]FAIL[/]"
    else:
        badge = f"[bold bright_yellow]{upper}[/]"
    suffix = f"  [dim]({detail})[/]" if detail else ""
    return f"  [bright_green]▸[/] [white]{label:<30}[/] {badge}{suffix}"


def ui_table(
    *,
    title: str | None = None,
    caption: str | None = None,
    show_lines: bool = False,
) -> Table:
    return Table(
        title=f"[bold bright_yellow]{title}[/]" if title else None,
        title_justify="left",
        caption=f"[dim]{caption}[/]" if caption else None,
        caption_justify="left",
        box=box.SQUARE,
        show_lines=show_lines,
        header_style="bold bright_yellow",
        border_style="green",
        padding=(0, 1),
    )


def ask_yes_no(question: str, default: str = "n") -> bool:
    """Ask a styled retro yes/no confirmation prompt."""
    default = default.lower()
    suffix = "Y/n" if default == "y" else "y/N"

    console.print(
        f"[bright_green]▸[/] [white]{question}[/] [bold bright_yellow]({suffix})[/]: ",
        end="",
    )

    raw = input().strip().lower()

    if raw == "":
        raw = default

    return raw == "y"


def resolve_console_command(
    raw_input: str,
    *,
    valid_choices: Iterable[str],
    aliases: dict[str, str] | None = None,
    default: str = "q",
) -> str | None:
    """Normalize a user prompt entry (shortcut key or retro text command) into a canonical action."""
    cleaned = (raw_input or "").strip().lower()
    if not cleaned:
        return default
    allowed = {c.lower() for c in valid_choices}
    if cleaned in allowed:
        return cleaned
    if aliases:
        mapped = aliases.get(cleaned)
        if mapped is not None and mapped.lower() in allowed:
            return mapped.lower()
    return None


def ask_console_command(
    prompt_context: str,
    *,
    valid_choices: Iterable[str],
    aliases: dict[str, str] | None = None,
    default: str = "q",
    target_console: Console | None = None,
) -> str:
    """Interactive retro prompt loop (e.g. `argandici> `) accepting keys or text commands."""
    out = target_console or console
    allowed = {c.lower() for c in valid_choices}
    while True:
        out.print(f"\n[bold bright_yellow]{prompt_context}[/][bold bright_green]>[/] ", end="")
        raw = input()
        resolved = resolve_console_command(
            raw,
            valid_choices=allowed,
            aliases=aliases,
            default=default,
        )
        if resolved is not None:
            return resolved
        out.print(
            f"  [bright_yellow]Commande inconnue '{raw.strip()}'. Tapez [bold]?[/bold] pour l'aide ou [bold]{default}[/bold] pour quitter.[/]"
        )


def is_clear_screen_disabled() -> bool:
    return os.getenv("DEVTOOLS_NO_CLEAR", "").strip().lower() in {"1", "true", "yes", "on"}


def clear_screen(
    target_console: Console | None = None,
    *,
    animated: bool = True,
    label: str | None = None,
    duration: float = 0.09,
) -> bool:
    """Clear the visible terminal screen with a snappy retro phosphor sweep animation.

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
        suffix = f"  [bright_yellow]{label}[/]" if label else ""
        with Live(console=out, transient=True, refresh_per_second=60) as live:
            for step in range(1, frames + 1):
                filled = int(width * step / frames)
                bar = f"[bold bright_green]{'█' * filled}[/][dim green]{'░' * (width - filled)}[/]"
                live.update(f"  {bar}{suffix}")
                time.sleep(step_delay)

    out.clear()
    return True
