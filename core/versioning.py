# core/versioning.py
import re
from dataclasses import dataclass

from core.config import DEFAULT_REMOTE
from core.conventional_commits import determine_bump_from_messages
from utils.common import run_command, run_command_checked


SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"

    def bump(self, kind: str) -> "SemVer":
        kind = kind.lower().strip()
        if kind == "major":
            return SemVer(self.major + 1, 0, 0)
        if kind == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if kind == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        raise ValueError("kind must be major|minor|patch")


def parse_semver(tag: str) -> SemVer | None:
    tag = (tag or "").strip()
    m = SEMVER_RE.match(tag)
    if not m:
        return None
    return SemVer(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def get_last_semver_tag(repo_path: str) -> str | None:
    """
    Return latest semver-like tag (vX.Y.Z) by version sort.
    """
    res = run_command(["git", "tag", "--list", "v*.*.*", "--sort=-v:refname"], cwd=repo_path)
    if res.returncode != 0:
        return None
    tags = (res.stdout or "").strip().splitlines()
    for t in tags:
        if parse_semver(t):
            return t
    return None


def determine_bump_from_commits(commit_subjects: str) -> str:
    """
    Very simple heuristic:
    - If 'BREAKING CHANGE' or '!:': major
    - Else if any 'feat:' : minor
    - Else if any 'fix:'/'perf:' : patch
    - Else: patch
    """
    s = (commit_subjects or "").lower()
    if "breaking change" in s:
        return "major"

    lines = [line.strip() for line in commit_subjects.splitlines() if line.strip()]
    return determine_bump_from_messages(lines)


def compute_next_version(repo_path: str, bump_kind: str, default_first: str = "v0.1.0") -> str:
    last_tag = get_last_semver_tag(repo_path)
    if not last_tag:
        # no tags -> first version
        # you can choose v0.0.1 if you prefer
        base = parse_semver(default_first)
        if not base:
            raise ValueError("default_first must be a semver tag like v0.1.0")
        return str(base)

    v = parse_semver(last_tag)
    if not v:
        # should not happen because get_last_semver_tag filters
        return default_first

    return str(v.bump(bump_kind))


def create_and_push_tag(repo_path: str, tag: str, message: str | None = None) -> None:
    """
    Create annotated tag and push it.
    """
    msg = message or f"Release {tag}"
    run_command_checked(["git", "tag", "-a", tag, "-m", msg], cwd=repo_path, context=f"create tag {tag}")
    run_command_checked(
        ["git", "push", DEFAULT_REMOTE, tag],
        cwd=repo_path,
        context=f"push tag {tag} to {DEFAULT_REMOTE}",
    )
