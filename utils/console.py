# utils/console.py

from rich.console import Console

console = Console()


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
