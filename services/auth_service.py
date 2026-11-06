from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from time import monotonic
from typing import Literal

from services.config import config
from services.storage.base import (
    StorageBackend,
    StorageMutation,
    StorageRevisionConflictError,
)

AuthRole = Literal["admin", "user"]
_CAS_ATTEMPTS = 4
_AUTH_SNAPSHOT_REFRESH_INTERVAL_SECONDS = 5.0
_AUTH_SNAPSHOT_MAX_STALE_SECONDS = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthKeyStoreUnavailableError(RuntimeError):
    """The User Key snapshot is too stale to authenticate against."""


class AuthService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage
        # Guards in-memory state only; storage I/O never runs while it is held,
        # because authenticate() takes it on the event loop thread.
        self._lock = Lock()
        self._mutation_lock = Lock()
        self._snapshot_refresh_lock = Lock()
        self._items: list[dict[str, object]] = []
        self._revision: str | None = None
        self._last_used_flush_at: dict[str, datetime] = {}
        self._pending_last_used: dict[str, str] = {}
        self._last_snapshot_refresh_attempt_at = 0.0
        self._last_snapshot_refresh_success_at = 0.0
        self._background_lock = Lock()
        self._background_wakeup = Event()
        self._background_thread: Thread | None = None
        self._reload_locked(suppress_errors=True)

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    @staticmethod
    def _default_name(role: object) -> str:
        return "管理员密钥" if str(role or "").strip().lower() == "admin" else "普通用户"

    def _normalize_item(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        role = self._clean(raw.get("role")).lower()
        if role not in {"admin", "user"}:
            return None
        key_hash = self._clean(raw.get("key_hash"))
        if not key_hash:
            return None
        item_id = self._clean(raw.get("id"))
        if not item_id:
            return None
        name = self._clean(raw.get("name")) or self._default_name(role)
        created_at = self._clean(raw.get("created_at")) or _now_iso()
        last_used_at = self._clean(raw.get("last_used_at")) or None
        return {
            "id": item_id,
            "name": name,
            "role": role,
            "key_hash": key_hash,
            "enabled": bool(raw.get("enabled", True)),
            "created_at": created_at,
            "last_used_at": last_used_at,
        }

    def _load_snapshot(self) -> tuple[list[dict[str, object]], str]:
        snapshot = self.storage.load_auth_keys_snapshot()
        items = snapshot.items if isinstance(snapshot.items, list) else []
        normalized = [
            normalized_item
            for item in items
            if (normalized_item := self._normalize_item(item)) is not None
        ]
        return normalized, snapshot.revision

    def _install_snapshot_locked(self, items: list[dict[str, object]], revision: str) -> None:
        self._items = items
        self._revision = revision
        refreshed_at = monotonic()
        self._last_snapshot_refresh_attempt_at = refreshed_at
        self._last_snapshot_refresh_success_at = refreshed_at

    def _reload_locked(self, *, suppress_errors: bool = False) -> bool:
        try:
            items, revision = self._load_snapshot()
        except Exception:
            if suppress_errors:
                return False
            raise
        self._install_snapshot_locked(items, revision)
        return True

    def _reload_outside_lock(self, *, suppress_errors: bool = False) -> None:
        """Load the stored snapshot without holding _lock, then install it.

        A snapshot loaded before a concurrent local write finished is older than
        memory, so it is only installed when the local revision did not move.
        """
        with self._lock:
            expected_revision = self._revision
        try:
            items, revision = self._load_snapshot()
        except Exception:
            if suppress_errors:
                return
            raise
        with self._lock:
            if self._revision == expected_revision:
                self._install_snapshot_locked(items, revision)

    def _snapshot_is_usable(self, now: float | None = None) -> bool:
        checked_at = monotonic() if now is None else now
        return (
            self._last_snapshot_refresh_success_at > 0
            and checked_at - self._last_snapshot_refresh_success_at
            <= _AUTH_SNAPSHOT_MAX_STALE_SECONDS
        )

    def _refresh_snapshot_if_due(self) -> bool:
        now = monotonic()
        if (
            now - self._last_snapshot_refresh_attempt_at
            < _AUTH_SNAPSHOT_REFRESH_INTERVAL_SECONDS
        ):
            return self._snapshot_is_usable(now)
        if not self._snapshot_refresh_lock.acquire(blocking=False):
            return self._snapshot_is_usable(now)
        try:
            now = monotonic()
            if (
                now - self._last_snapshot_refresh_attempt_at
                < _AUTH_SNAPSHOT_REFRESH_INTERVAL_SECONDS
            ):
                return self._snapshot_is_usable(now)
            self._last_snapshot_refresh_attempt_at = now
            with self._lock:
                expected_local_revision = self._revision
            try:
                items, revision = self._load_snapshot()
            except Exception:
                return self._snapshot_is_usable()
            with self._lock:
                if self._revision != expected_local_revision:
                    return self._snapshot_is_usable()
                self._items = items
                self._revision = revision
                self._last_snapshot_refresh_success_at = monotonic()
                active_ids = {self._clean(item.get("id")) for item in items}
                self._last_used_flush_at = {
                    item_id: flushed_at
                    for item_id, flushed_at in self._last_used_flush_at.items()
                    if item_id in active_ids
                }
                return True
        finally:
            self._snapshot_refresh_lock.release()

    def _set_cached_item_locked(self, item: dict[str, object], revision: str) -> None:
        item_id = self._clean(item.get("id"))
        self._items = [
            current
            for current in self._items
            if self._clean(current.get("id")) != item_id
        ]
        self._items.append(item)
        self._revision = revision

    def _delete_cached_item_locked(self, item_id: str, revision: str) -> None:
        self._items = [
            item
            for item in self._items
            if self._clean(item.get("id")) != item_id
        ]
        self._revision = revision

    def _find_item_index_locked(
        self,
        item_id: str,
        *,
        role: AuthRole | None = None,
    ) -> int | None:
        for index, item in enumerate(self._items):
            if self._clean(item.get("id")) != item_id:
                continue
            if role is not None and item.get("role") != role:
                return None
            return index
        return None

    def _find_hash_index_locked(self, candidate_hash: str) -> int | None:
        for index, item in enumerate(self._items):
            if not bool(item.get("enabled", True)):
                continue
            stored_hash = self._clean(item.get("key_hash"))
            if stored_hash and hmac.compare_digest(stored_hash, candidate_hash):
                return index
        return None

    @staticmethod
    def _public_item(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "role": item.get("role"),
            "enabled": bool(item.get("enabled", True)),
            "created_at": item.get("created_at"),
            "last_used_at": item.get("last_used_at"),
        }

    def list_keys(self, role: AuthRole | None = None) -> list[dict[str, object]]:
        self._reload_outside_lock(suppress_errors=True)
        with self._lock:
            items = [item for item in self._items if role is None or item.get("role") == role]
            return [self._public_item(item) for item in items]

    def _has_key_hash_locked(self, key_hash: str, *, exclude_id: str = "") -> bool:
        for item in self._items:
            item_id = self._clean(item.get("id"))
            if exclude_id and item_id == exclude_id:
                continue
            stored_hash = self._clean(item.get("key_hash"))
            if stored_hash and hmac.compare_digest(stored_hash, key_hash):
                return True
        return False

    def _build_key_hash_locked(self, raw_key: str, *, exclude_id: str = "") -> str:
        candidate = self._clean(raw_key)
        if not candidate:
            raise ValueError("请输入新的专用密钥")
        admin_key = self._clean(config.auth_key)
        if admin_key and hmac.compare_digest(candidate, admin_key):
            raise ValueError("这个密钥和管理员密钥冲突了，请换一个新的密钥")
        key_hash = _hash_key(candidate)
        if self._has_key_hash_locked(key_hash, exclude_id=exclude_id):
            raise ValueError("这个专用密钥已经存在，请换一个新的密钥")
        return key_hash

    def _has_name_locked(self, name: str, *, role: AuthRole | None = None, exclude_id: str = "") -> bool:
        candidate = self._clean(name)
        if not candidate:
            return False
        for item in self._items:
            item_id = self._clean(item.get("id"))
            if exclude_id and item_id == exclude_id:
                continue
            if role is not None and item.get("role") != role:
                continue
            if self._clean(item.get("name")) == candidate:
                return True
        return False

    def _build_default_name_locked(self, role: AuthRole, *, exclude_id: str = "") -> str:
        base_name = self._default_name(role)
        if not self._has_name_locked(base_name, role=role, exclude_id=exclude_id):
            return base_name
        suffix = 2
        while True:
            candidate = f"{base_name} {suffix}"
            if not self._has_name_locked(candidate, role=role, exclude_id=exclude_id):
                return candidate
            suffix += 1

    def _build_name_locked(self, name: str, *, role: AuthRole, exclude_id: str = "") -> str:
        candidate = self._clean(name)
        if not candidate:
            return self._build_default_name_locked(role, exclude_id=exclude_id)
        if self._has_name_locked(candidate, role=role, exclude_id=exclude_id):
            raise ValueError("这个名称已经在使用中了，换一个更容易区分的名称吧")
        return candidate

    def create_key(self, *, role: AuthRole, name: str = "") -> tuple[dict[str, object], str]:
        with self._mutation_lock:
            for attempt in range(_CAS_ATTEMPTS):
                self._reload_outside_lock()
                with self._lock:
                    normalized_name = self._build_name_locked(name, role=role)
                    while True:
                        raw_key = f"sk-{secrets.token_urlsafe(24)}"
                        try:
                            key_hash = self._build_key_hash_locked(raw_key)
                            break
                        except ValueError:
                            continue
                    existing_ids = {self._clean(item.get("id")) for item in self._items}
                    while True:
                        item_id = uuid.uuid4().hex[:12]
                        if item_id not in existing_ids:
                            break
                    item = {
                        "id": item_id,
                        "name": normalized_name,
                        "role": role,
                        "key_hash": key_hash,
                        "enabled": True,
                        "created_at": _now_iso(),
                        "last_used_at": None,
                    }
                    expected_revision = self._revision
                try:
                    result = self.storage.mutate_auth_keys(
                        StorageMutation(
                            upserts=(item,),
                            expected_revision=expected_revision,
                        )
                    )
                except StorageRevisionConflictError:
                    if attempt + 1 >= _CAS_ATTEMPTS:
                        raise
                    continue
                with self._lock:
                    self._set_cached_item_locked(item, result.revision)
                return self._public_item(item), raw_key
        raise RuntimeError("auth key mutation retry exhausted")

    def update_key(
        self,
        key_id: str,
        updates: dict[str, object],
        *,
        role: AuthRole | None = None,
    ) -> dict[str, object] | None:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return None
        with self._mutation_lock:
            for attempt in range(_CAS_ATTEMPTS):
                self._reload_outside_lock()
                with self._lock:
                    index = self._find_item_index_locked(normalized_id, role=role)
                    if index is None:
                        return None
                    next_item = dict(self._items[index])
                    next_role = "admin" if str(next_item.get("role") or "").strip().lower() == "admin" else "user"
                    if "name" in updates and updates.get("name") is not None:
                        next_item["name"] = self._build_name_locked(
                            str(updates.get("name") or ""),
                            role=next_role,
                            exclude_id=normalized_id,
                        )
                    if "enabled" in updates and updates.get("enabled") is not None:
                        next_item["enabled"] = bool(updates.get("enabled"))
                    if "key" in updates and updates.get("key") is not None:
                        next_item["key_hash"] = self._build_key_hash_locked(str(updates.get("key") or ""), exclude_id=normalized_id)
                    expected_revision = self._revision
                try:
                    result = self.storage.mutate_auth_keys(
                        StorageMutation(
                            upserts=(next_item,),
                            expected_revision=expected_revision,
                        )
                    )
                except StorageRevisionConflictError:
                    if attempt + 1 >= _CAS_ATTEMPTS:
                        raise
                    continue
                with self._lock:
                    self._set_cached_item_locked(next_item, result.revision)
                return self._public_item(next_item)
        return None

    def delete_key(self, key_id: str, *, role: AuthRole | None = None) -> bool:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return False
        with self._mutation_lock:
            for attempt in range(_CAS_ATTEMPTS):
                self._reload_outside_lock()
                with self._lock:
                    if self._find_item_index_locked(normalized_id, role=role) is None:
                        return False
                    expected_revision = self._revision
                try:
                    result = self.storage.mutate_auth_keys(
                        StorageMutation(
                            delete_keys=(normalized_id,),
                            expected_revision=expected_revision,
                        )
                    )
                except StorageRevisionConflictError:
                    if attempt + 1 >= _CAS_ATTEMPTS:
                        raise
                    continue
                with self._lock:
                    self._delete_cached_item_locked(normalized_id, result.revision)
                    self._last_used_flush_at.pop(normalized_id, None)
                return result.deleted > 0
        return False

    def authenticate(self, raw_key: str) -> dict[str, object] | None:
        """Validate a User Key against the cached snapshot without blocking on storage.

        Snapshot refreshes and last-used writes run on a background thread. A
        snapshot too stale to trust raises AuthKeyStoreUnavailableError.
        """
        candidate = self._clean(raw_key)
        if not candidate:
            return None
        candidate_hash = _hash_key(candidate)
        self._ensure_background_worker()
        if not self._snapshot_is_usable():
            # Never load on the caller's thread (the event loop); let the
            # background refresher catch up and report the store as unavailable.
            self._background_wakeup.set()
            raise AuthKeyStoreUnavailableError("user key store is unavailable")
        schedule_flush = False
        with self._lock:
            index = self._find_hash_index_locked(candidate_hash)
            if index is None:
                return None
            now = datetime.now(timezone.utc)
            next_item = dict(self._items[index])
            next_item["last_used_at"] = now.isoformat()
            self._items[index] = next_item
            item_id = self._clean(next_item.get("id"))
            last_flush_at = self._last_used_flush_at.get(item_id)
            if last_flush_at is None or (now - last_flush_at).total_seconds() >= 60:
                self._last_used_flush_at[item_id] = now
                self._pending_last_used[item_id] = str(next_item["last_used_at"])
                schedule_flush = True
        if schedule_flush:
            self._background_wakeup.set()
        return self._public_item(next_item)

    def _ensure_background_worker(self) -> None:
        thread = self._background_thread
        if thread is not None and thread.is_alive():
            return
        with self._background_lock:
            thread = self._background_thread
            if thread is not None and thread.is_alive():
                return
            thread = Thread(target=self._background_loop, name="auth-key-snapshot", daemon=True)
            self._background_thread = thread
            thread.start()

    def _background_loop(self) -> None:
        while True:
            try:
                self._refresh_snapshot_if_due()
                self._flush_last_used()
            except Exception:
                pass
            self._background_wakeup.wait(_AUTH_SNAPSHOT_REFRESH_INTERVAL_SECONDS)
            self._background_wakeup.clear()

    def _flush_last_used(self) -> None:
        with self._lock:
            pending = dict(self._pending_last_used)
            self._pending_last_used.clear()
        if not pending:
            return
        for _attempt in range(_CAS_ATTEMPTS):
            with self._lock:
                expected_revision = self._revision
                if expected_revision is None:
                    return
                upserts = tuple(
                    {**item, "last_used_at": pending[item_id]}
                    for item in self._items
                    if (item_id := self._clean(item.get("id"))) in pending
                )
            if not upserts:
                return
            try:
                result = self.storage.mutate_auth_keys(
                    StorageMutation(upserts=upserts, expected_revision=expected_revision)
                )
            except StorageRevisionConflictError:
                try:
                    self._reload_outside_lock()
                except Exception:
                    return
                continue
            except Exception:
                return
            with self._lock:
                if self._revision != expected_revision:
                    return
                updated_by_id = {self._clean(item.get("id")): item for item in upserts}
                self._items = [
                    updated_by_id.get(self._clean(item.get("id")), item)
                    for item in self._items
                ]
                self._revision = result.revision
            return


auth_service = AuthService(config.get_storage_backend())
