import re
from dataclasses import dataclass
from typing import Iterable


CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?P<type>feat|fix|refactor|docs|test|chore|perf|ci|build|style|ui)"
    r"(?:\((?P<scope>[^)]+)\))?(?P<breaking>!)?: (?P<subject>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ConventionalCommit:
    type: str
    scope: str
    subject: str
    breaking: bool

    @property
    def normalized_type(self) -> str:
        return "style" if self.type == "ui" else self.type


def parse_conventional_commit(message: str) -> ConventionalCommit | None:
    header = (message or "").splitlines()[0].strip()
    if header.startswith("- "):
        header = header[2:].strip()

    match = CONVENTIONAL_COMMIT_RE.match(header)
    if not match:
        return None

    return ConventionalCommit(
        type=match.group("type").lower(),
        scope=(match.group("scope") or "").strip(),
        subject=match.group("subject").strip(),
        breaking=bool(match.group("breaking")),
    )


def determine_bump_from_messages(messages: Iterable[str]) -> str:
    bump = "patch"

    for message in messages:
        parsed = parse_conventional_commit(message)
        if not parsed:
            continue
        if parsed.breaking:
            return "major"
        if parsed.normalized_type == "feat":
            bump = "minor"
        elif parsed.normalized_type in {"fix", "perf"} and bump != "minor":
            bump = "patch"

    return bump
