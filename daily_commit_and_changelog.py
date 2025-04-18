import auto_commit_all
import update_changelog_all

if __name__ == "__main__":
    print("🚀 Starting daily commit + changelog workflow...\n")

    # Commits
    commit_results = auto_commit_all.auto_commit_all_repos("~/code/pers")

    # Changelogs
    changelogs_updated = update_changelog_all.update_all_repos_interactive("~/code/pers")

    # Résumé final
    print("\n✅ Summary:")
    print(f"- 🗂 {commit_results['committed']} repositories committed")
    print(f"- 📝 {changelogs_updated} changelogs updated")
    print(f"- 🚀 {commit_results['pushed']} pushes performed")
