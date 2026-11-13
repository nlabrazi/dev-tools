from dataclasses import asdict, dataclass, field
from typing import Any

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"
STATUS_PENDING = "PENDING"
STATUS_UNKNOWN = "UNKNOWN"
VALID_STATUSES = {
    STATUS_PASS,
    STATUS_FAIL,
    STATUS_SKIPPED,
    STATUS_PENDING,
    STATUS_UNKNOWN,
}

RISK_LOW = "LOW RISK"
RISK_REVIEW = "REVIEW REQUIRED"
RISK_BREAKING = "BREAKING CHANGE"
RISK_BLOCKED = "BLOCKED"
RISK_UNKNOWN = "UNKNOWN"
VALID_RISK_LEVELS = {
    RISK_LOW,
    RISK_REVIEW,
    RISK_BREAKING,
    RISK_BLOCKED,
    RISK_UNKNOWN,
}

DRIFT_ALIGNED = "ALIGNED"
DRIFT_PROBLEMATIC = "PROBLEMATIC DRIFT"
DRIFT_ACCEPTED = "ACCEPTED DIFFERENCE"
DRIFT_INFO_UNAVAILABLE = "INFORMATION UNAVAILABLE"

DEV_PLATFORM_PRESET = "github>nlabrazi/dev-platform//configs/renovate/base"


@dataclass(frozen=True)
class GitStateInfo:
    branch: str
    base_branch: str | None
    base_branch_resolution: str
    is_clean: bool
    staged_count: int = 0
    unstaged_count: int = 0
    untracked_count: int = 0
    ahead_of_base: int | None = None
    behind_base: int | None = None
    venv_exists: bool = False
    venv_is_symlink: bool = False
    venv_target: str | None = None
    venv_has_pip: bool | None = None
    untracked_venv_entries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StackComponent:
    ecosystem: str
    name: str
    manager: str
    files: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    pinned_count: int = 0
    unpinned_dependencies: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeclaredVersion:
    component: str
    environment: str
    version: str
    source_file: str


@dataclass(frozen=True)
class EnvironmentMatrixRow:
    component: str
    local: str = STATUS_UNKNOWN
    github_actions: str = STATUS_UNKNOWN
    jenkins: str = STATUS_UNKNOWN
    docker: str = STATUS_UNKNOWN
    production: str = STATUS_UNKNOWN
    drift_status: str = DRIFT_INFO_UNAVAILABLE
    drift_details: str = ""
    sources: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenovateConfigInfo:
    exists: bool
    path: str = "renovate.json"
    valid_json: bool = False
    extends_dev_platform: bool = False
    extends: list[str] = field(default_factory=list)
    base_branch_patterns: list[str] = field(default_factory=list)
    pr_concurrent_limit: int | None = None
    automerge: bool | None = None
    has_private_registry: bool = False
    private_registries: list[str] = field(default_factory=list)
    local_overrides: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RenovateUpdateItem:
    package: str
    update_type: str
    from_version: str
    to_version: str
    raw_change: str = ""


@dataclass(frozen=True)
class RenovatePRAnalysis:
    risk_level: str
    breaking_change: bool
    breaking_summary: str
    summary: str
    key_changes: list[str] = field(default_factory=list)
    project_impacts: list[str] = field(default_factory=list)
    checks_before_merge: list[str] = field(default_factory=list)
    verdict: str = ""
    source: str = "heuristic"


@dataclass(frozen=True)
class RenovatePRInfo:
    number: int
    title: str
    url: str = ""
    author: str = ""
    head_branch: str = ""
    base_branch: str = ""
    state: str = "OPEN"
    is_onboarding: bool = False
    ci_status: str = STATUS_UNKNOWN
    merge_state: str = STATUS_UNKNOWN
    changed_files: list[str] = field(default_factory=list)
    updates: list[RenovateUpdateItem] = field(default_factory=list)
    has_release_notes: bool = False
    has_renovate_warning: bool = False
    release_notes_excerpt: str = ""
    analysis: RenovatePRAnalysis | None = None


@dataclass(frozen=True)
class DependencyDashboardInfo:
    status: str = STATUS_UNKNOWN
    issue_number: int | None = None
    state: str = STATUS_UNKNOWN
    no_open_or_pending: bool = False
    open_items: list[str] = field(default_factory=list)
    pending_or_rate_limited_items: list[str] = field(default_factory=list)
    detected_managers: list[str] = field(default_factory=list)
    details: str = ""


@dataclass(frozen=True)
class CIStatusSummary:
    github_actions_status: str = STATUS_UNKNOWN
    github_actions_workflows: list[str] = field(default_factory=list)
    github_actions_details: str = ""
    jenkins_status: str = STATUS_UNKNOWN
    jenkins_file: str | None = None
    jenkins_details: str = ""


@dataclass(frozen=True)
class AuditReport:
    repo_name: str
    repo_path: str
    generated_at: str
    git_state: GitStateInfo
    stacks: list[StackComponent]
    renovate_config: RenovateConfigInfo
    dashboard: DependencyDashboardInfo
    open_prs: list[RenovatePRInfo]
    ci_summary: CIStatusSummary
    env_matrix: list[EnvironmentMatrixRow]
    deployment_files: list[str] = field(default_factory=list)
    reservations: list[str] = field(default_factory=list)
    overall_status: str = "AUDIT COMPLETE"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
