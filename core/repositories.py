import os
from collections.abc import Iterator

from core.config import is_unity_excluded, is_unity_project


def is_git_repo(path: str) -> bool:
    git_entry = os.path.join(path, ".git")
    return os.path.isdir(git_entry) or os.path.isfile(git_entry)


def iter_git_repositories(
    root_dir: str,
    *,
    exclude_unity: bool | None = None,
) -> Iterator[tuple[str, str]]:
    if not os.path.isdir(root_dir):
        return

    should_exclude_unity = is_unity_excluded() if exclude_unity is None else exclude_unity

    for repo_name in sorted(os.listdir(root_dir)):
        repo_path = os.path.join(root_dir, repo_name)
        if not os.path.isdir(repo_path) or not is_git_repo(repo_path):
            continue
        if should_exclude_unity and is_unity_project(repo_path):
            continue
        yield repo_name, repo_path


def collect_repositories(
    root_dirs: list[str],
    *,
    exclude_unity: bool | None = None,
) -> list[tuple[str, str]]:
    return [
        repo
        for root_dir in root_dirs
        for repo in iter_git_repositories(root_dir, exclude_unity=exclude_unity)
    ]
