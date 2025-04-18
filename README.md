# 🧠 Daily Git Workflow Automation

This repository contains two powerful Python scripts to automate your daily Git tasks across multiple projects:

- ✅ Automatically generate and validate commits (`auto_commit_all.py`)
- 📝 Update and commit `CHANGELOG.md` based on recent Git history (`update_changelog_all.py`)

---

## 🚀 Features

- Scan all Git repositories in a specified folder
- Detect uncommitted changes and propose a clean commit message (Conventional Commits style)
- Optional interactive confirmation for each commit and push
- Intelligent detection of commit type based on file extensions
- Generate changelog entries per repository with emoji-enhanced summaries
- Final summary: committed projects, changelogs updated, and pushes performed

---

## 📦 Scripts

### `auto_commit_all.py`
> Auto-detects changed files, generates a commit message based on dominant file types, and lets you optionally push to the `staging` branch.

### `update_changelog_all.py`
> Scans Git history and creates a formatted changelog block. Commit and push are also validated interactively.

### `daily_commit_and_changelog.py`
> Runs both scripts sequentially and gives you a global summary of the actions.

---

## 📁 Folder structure

```
+-- auto_commit_all.py
+-- update_changelog_all.py
+-- daily_commit_and_changelog.py
+-- README.md
+-- CHANGELOG.md
```

---

## 🛠️ Requirements

- Python 3.8+
- Git installed and available in your `$PATH`

No external dependencies required.

---

## 🧪 Usage

```bash
python daily_commit_and_changelog.py
```

You'll be asked to confirm each commit and push.
Perfect for wrapping up your coding day across all your personal or work repositories.

---

## 🕵️‍♂️💻 Author

Made with ❤️ by [@nlabrazi](https://github.com/nlabrazi)
