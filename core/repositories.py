import os
from collections.abc import Iterator


def is_git_repo(path: str) -> bool:
    git_entry = os.path.join(path, ".git")
    return os.path.isdir(git_entry) or os.path.isfile(git_entry)


def iter_git_repositories(root_dir: str) -> Iterator[tuple[str, str]]:
    if not os.path.isdir(root_dir):
        return

    for repo_name in sorted(os.listdir(root_dir)):
        repo_path = os.path.join(root_dir, repo_name)
        if os.path.isdir(repo_path) and is_git_repo(repo_path):
            yield repo_name, repo_path
