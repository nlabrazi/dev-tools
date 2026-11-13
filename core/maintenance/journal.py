from datetime import datetime
import json
import os
from pathlib import Path

from core.maintenance.models import (
    MaintenanceOperationEntry,
    MaintenanceSessionJournal,
)
from utils.common import is_dry_run

STEP_ORDER_NEXT: dict[str, tuple[str, str]] = {
    "audit": ("4", "Prepare controlled local migration changes on staging (or 3 to inspect risk & migration plan)."),
    "plan": ("4", "Preview and apply controlled local migration changes on staging."),
    "prepare_changes": ("5", "Run local unit/integration validations and verify PASS/FAIL/SKIPPED counts."),
    "validate": ("6", "Commit on staging, merge Renovate PRs, or promote staging → base branch."),
    "git_workflow": ("7", "Inspect production .venv backups, deployment checklist, and Sentinel post-deploy status."),
    "production_check": ("8", "Export the final audit & maintenance report."),
    "export": ("q", "Maintenance session complete."),
}


def resolve_journal_path(
    repo_path: str,
    repo_name: str,
    *,
    journal_dir: str | Path | None = None,
) -> Path:
    effective_dir = (
        Path(journal_dir)
        if journal_dir is not None
        else (
            Path(os.environ["DEVTOOLS_MAINTENANCE_JOURNAL_DIR"])
            if os.environ.get("DEVTOOLS_MAINTENANCE_JOURNAL_DIR", "").strip()
            else None
        )
    )
    if effective_dir is not None:
        return effective_dir / f"{repo_name}-maintenance-journal.json"

    root = Path(repo_path)
    git_dir = root / ".git"
    if git_dir.is_dir():
        return git_dir / "devtools-maintenance-journal.json"
    return root / f".{repo_name}-maintenance-journal.json"


def load_maintenance_journal(
    repo_path: str,
    repo_name: str,
    *,
    journal_dir: str | Path | None = None,
) -> MaintenanceSessionJournal | None:
    target = resolve_journal_path(repo_path, repo_name, journal_dir=journal_dir)
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        entries: list[MaintenanceOperationEntry] = []
        for item in raw.get("completed_steps", []):
            if isinstance(item, dict):
                entries.append(
                    MaintenanceOperationEntry(
                        step=str(item.get("step", "")),
                        status=str(item.get("status", "")),
                        timestamp=str(item.get("timestamp", "")),
                        details=str(item.get("details", "")),
                    )
                )
        return MaintenanceSessionJournal(
            repo_name=str(raw.get("repo_name", repo_name)),
            repo_path=str(raw.get("repo_path", repo_path)),
            updated_at=str(raw.get("updated_at", "")),
            current_stage=str(raw.get("current_stage", "")),
            completed_steps=entries,
            pending_actions=[str(x) for x in raw.get("pending_actions", []) if x],
            is_complete=bool(raw.get("is_complete", False)),
        )
    except (OSError, json.JSONDecodeError):
        return None


def save_maintenance_journal(
    journal: MaintenanceSessionJournal,
    *,
    journal_dir: str | Path | None = None,
) -> tuple[str, str]:
    target = resolve_journal_path(
        journal.repo_path,
        journal.repo_name,
        journal_dir=journal_dir,
    )
    if is_dry_run():
        return "dry-run", str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(journal.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return "saved", str(target)


def record_maintenance_step(
    repo_path: str,
    repo_name: str,
    step: str,
    status: str,
    details: str = "",
    *,
    pending_actions: list[str] | None = None,
    is_complete: bool = False,
    journal_dir: str | Path | None = None,
    now: datetime | None = None,
) -> tuple[str, MaintenanceSessionJournal]:
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    existing = load_maintenance_journal(repo_path, repo_name, journal_dir=journal_dir)
    previous_steps = list(existing.completed_steps) if existing is not None else []
    new_entry = MaintenanceOperationEntry(
        step=step,
        status=status,
        timestamp=timestamp,
        details=details,
    )
    previous_steps.append(new_entry)

    effective_pending = (
        pending_actions
        if pending_actions is not None
        else (list(existing.pending_actions) if existing is not None else [])
    )

    updated = MaintenanceSessionJournal(
        repo_name=repo_name,
        repo_path=repo_path,
        updated_at=timestamp,
        current_stage=step,
        completed_steps=previous_steps,
        pending_actions=effective_pending,
        is_complete=is_complete,
    )
    save_status, _ = save_maintenance_journal(updated, journal_dir=journal_dir)
    return save_status, updated


def get_resume_recommendation(journal: MaintenanceSessionJournal) -> tuple[str, str]:
    if journal.is_complete:
        return "1", "Previous maintenance session is marked complete; run a fresh audit (option 1) if needed."
    return STEP_ORDER_NEXT.get(
        journal.current_stage,
        ("1", "Run a full read-only audit (option 1) to refresh repository state."),
    )
