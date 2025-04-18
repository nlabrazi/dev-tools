import os
import auto_commit_all
import update_changelog_all

if __name__ == "__main__":
    print("🚀 Starting daily commit + changelog workflow...\n")

    # 📁 Fix path ~/code/pers
    root_path = os.path.expanduser("~/code/pers")

    # 🔧 Commits auto
    commit_results = auto_commit_all.auto_commit_all_repos(root_path)

    # 📝 Generate changelog
    changelogs_updated = update_changelog_all.update_all_repos_interactive(root_path)

    # ✅ Résumé
    print("\n\n✅ Summary:")
    print(f"- 🗂 {commit_results['committed']} repositories committed")
    print(f"- 📝 {changelogs_updated} changelogs updated")
    print(f"- 🚀 {commit_results['pushed']} pushes performed")
