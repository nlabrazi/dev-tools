## [Unreleased] - 2026-03-01

### ✨ Feat
- update core/commit.py and core/formatters.py (2026-03-01 00:15)

### 🐛 Fix
- typo into commit regarding color

### 🔧 Chore
- refactor into core modules and improve CLI UX

## [Unreleased] - 2025-04-28

### 🐛 Fix
- typo into commit regarding color

### 🔧 Chore
- refactor into core modules and improve CLI UX

## [Unreleased] - 2025-04-28

### 🐛 Fix
- typo into commit regarding color

### 🔧 Chore
- refactor into core modules and improve CLI UX

## [Unreleased] - 2025-04-28

### 🐛 Fix
- typo into commit regarding color

### 🔧 Chore
- refactor into core modules and improve CLI UX

## [Unreleased] - 2025-04-28

### 🐛 Fix
- typo into commit regarding color

### 🔧 Chore
- refactor into core modules and improve CLI UX

# 📅 CHANGELOG

## [Unreleased] - 2025-04-19

- ✨ Project reorganization with `core/` folder
- 🚀 New `run.py` orchestrator with clean figlet sections and Rich UI
- 🔁 `core/merge.py`: interactive GitHub CLI merge with preview and auto-merge
- 🟠 Color support: repo names in orange + clear section spacing
- ✅ Fixed changelog generation to skip when no new commits exist
- ✅ Clean import usage in `run.py`, calling `core.merge.main()` correctly

## [Unreleased] - 2025-04-18

- ✨ Initial release: automatic commit and changelog automation
- ✅ `auto_commit_all.py`: detects changes, generates commits, and pushes to staging
- 📝 `update_changelog_all.py`: generates `CHANGELOG.md` with interactive validation
- 🔁 `daily_commit_and_changelog.py`: runs both scripts and prints a global summary
