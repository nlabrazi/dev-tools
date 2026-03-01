from rich import print
from rich.console import Console
from rich.panel import Panel
from pyfiglet import figlet_format
from utils.common import set_dry_run
from utils.console import ask_yes_no
from core.commit import auto_commit_all_repos
from core.changelog import update_all_repos_interactive
import core.merge as merge
import core.sync as sync
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

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Simulate actions (safe mode)")
    group.add_argument("--prod", action="store_true", help="Execute real actions")

    args = parser.parse_args()

    if args.dry_run:
        set_dry_run(True)
        console.print("\n🚀 [bold cyan][DRY-RUN MODE ENABLED][/]\n")
    elif args.prod:
        set_dry_run(False)
        console.print("\n🚀 [bold green][PRODUCTION MODE - REAL EXECUTION][/]\n")

    # Banner
    print(f"\n[bold green]{figlet_format('Dev Tools', font='slant')}[/]")

    # --- STEP 1: AUTO-COMMIT ---
    section_title("Auto-commit staging", "🔧")
    if ask_yes_no("Browse repos and run auto-commit ?", default="n"):
        auto_commit_all_repos(ROOT_DIRS)

    # --- STEP 2: MERGE ---
    section_title("Merge to master", "🔁")
    if ask_yes_no("Merge staging into master ?", default="n"):
        merge.main()

    # --- STEP 3: CHANGELOG ---
    section_title("Update changelogs", "📝")
    if ask_yes_no("Update changelogs ?", default="n"):
        update_all_repos_interactive(ROOT_DIRS)

    # --- STEP 4: SYNC MASTER ---
    section_title("Sync master from origin", "⏳")
    if ask_yes_no("Checkout master + pull origin/master on all repos ?", default="n"):
        sync.main(ROOT_DIRS)

    print(f"\n[bold cyan]{figlet_format('All Done!', font='slant')}[/]")

if __name__ == "__main__":
    main()
