import os


def _resolve_root_dirs() -> list[str]:
    raw = os.getenv("DEVTOOLS_ROOT_DIRS", "").strip()
    if raw:
        return [
            os.path.expanduser(part.strip())
            for part in raw.split(os.pathsep)
            if part.strip()
        ]

    return [
        os.path.expanduser("~/code/pers"),
        os.path.expanduser("~/code/bricolage"),
        os.path.expanduser("/mnt/d/Unity/Projects"),
    ]


ROOT_DIRS = _resolve_root_dirs()
DEFAULT_REMOTE = os.getenv("DEVTOOLS_REMOTE", "origin")
DEFAULT_BASE_BRANCH = os.getenv("DEVTOOLS_BASE_BRANCH", "master")
DEFAULT_HEAD_BRANCH = os.getenv("DEVTOOLS_HEAD_BRANCH", "staging")
CHANGELOG_FILENAME = "CHANGELOG.md"
