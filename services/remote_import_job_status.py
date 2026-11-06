"""Canonical status semantics for remote account-import jobs."""

from __future__ import annotations

from datetime import datetime, timezone


REMOTE_IMPORT_ACTIVE_STATUSES = frozenset({"pending", "running"})
# A worker that cannot persist its own failure leaves the job active; after this long
# without any progress write the job no longer blocks new imports.
REMOTE_IMPORT_STALE_SECONDS = 3600


def import_job_status_is_active(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    status = str(raw.get("status") or "").strip().lower()
    return status in REMOTE_IMPORT_ACTIVE_STATUSES


def import_job_is_stale(raw: object, *, now: datetime | None = None) -> bool:
    if not import_job_status_is_active(raw):
        return False
    value = str(raw.get("updated_at") or raw.get("created_at") or "").strip()
    try:
        updated_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - updated_at).total_seconds() > REMOTE_IMPORT_STALE_SECONDS


def import_job_is_active(raw: object) -> bool:
    return import_job_status_is_active(raw) and not import_job_is_stale(raw)
