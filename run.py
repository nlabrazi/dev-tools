from rich import print
from rich.console import Console
from rich.panel import Panel
from pyfiglet import figlet_format
from utils.common import set_dry_run
from core.commit import auto_commit_all_repos
from core.changelog import update_all_repos_interactive
import core.merge as merge
import argparse
import os

ROOT_DIRS = [
    os.path.expanduser("~/code/pers"),
    os.path.expanduser("~/code/bricolage")
]

console = Console()

def section_title(title, emoji):
    print("\n")
    console.print(Panel.fit(f"{emoji}  {title.upper()}", style="bold green", border_style="cyan"))

def main():
    parser = argparse.ArgumentParser(description="Dev Tools Runner")
    parser.add_argument("--dry-run", action="store_true", help="Enable dry-run mode (simulate actions)")
    args = parser.parse_args()

    if args.dry_run:
        set_dry_run(True)
        console.print("\n🚀 [bold cyan][DRY-RUN MODE ENABLED][/]\n")
    else:
        console.print("\n🚀 [bold green][PRODUCTION MODE - REAL EXECUTION][/]\n")

    # Banner
    print(f"\n[bold green]{figlet_format('Dev Tools', font='slant')}[/]")

    # --- STEP 1: AUTO-COMMIT ---
    section_title("Auto-commit staging", "🔧")
    run_commit = input("Run auto-commit on all repos? (y/n): ").strip().lower()
    if run_commit == "y":
        auto_commit_all_repos(ROOT_DIRS)

    # --- STEP 2: CHANGELOG ---
    section_title("Update changelogs", "📝")
    run_changelog = input("Update changelogs? (y/n): ").strip().lower()
    if run_changelog == "y":
        update_all_repos_interactive(ROOT_DIRS)

    # --- STEP 3: MERGE ---
    section_title("Merge to master", "🔁")
    run_merge = input("Merge staging into master? (y/n): ").strip().lower()
    if run_merge == "y":
        merge.main()

    print(f"\n[bold cyan]{figlet_format('All Done!', font='slant')}[/]")

if __name__ == "__main__":
    main()
