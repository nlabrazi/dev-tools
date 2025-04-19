from rich import print
from rich.console import Console
from rich.panel import Panel
from pyfiglet import figlet_format
from core.commit import auto_commit_all_repos
from core.changelog import update_all_repos_interactive
import core.merge as merge
import os

ROOT_DIR = os.path.expanduser("~/code/pers")
console = Console()

def section_title(title, emoji):
    print("\n")
    console.print(Panel.fit(f"{emoji}  {title.upper()}", style="bold green", border_style="cyan"))

if __name__ == "__main__":
    # Banner
    print(f"\n[bold green]{figlet_format('Dev Tools', font='slant')}[/]")

    # --- STEP 1: AUTO-COMMIT ---
    section_title("Auto-commit staging", "🔧")
    run_commit = input("Run auto-commit on all repos? (y/n): ").strip().lower()
    if run_commit == "y":
        auto_commit_all_repos(ROOT_DIR)

    # --- STEP 2: CHANGELOG ---
    section_title("Update changelogs", "📝")
    run_changelog = input("Update changelogs? (y/n): ").strip().lower()
    if run_changelog == "y":
        update_all_repos_interactive(ROOT_DIR)

    # --- STEP 3: MERGE ---
    section_title("Merge to master", "🔁")
    run_merge = input("Merge staging into master? (y/n): ").strip().lower()
    if run_merge == "y":
        merge.main()

    print(f"\n[bold cyan]{figlet_format('All Done!', font='slant')}[/]")
