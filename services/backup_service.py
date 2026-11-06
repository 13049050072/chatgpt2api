from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import random
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from xml.sax.saxutils import escape as xml_escape

from curl_cffi import requests
from sqlalchemy import make_url

from services.application_database import database_backend_name
from services.config import DATA_DIR, config
from services.image_storage_service import IMAGE_INDEX_FILE
from services.image_tags_service import TAGS_FILE
from services.storage.coordination_repository import BackupExecutionStateRepository


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_backup_state(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    status = _clean(source.get("last_status")) or "idle"
    if status not in {"idle", "running", "success", "error"}:
        status = "idle"
    return {
        "last_started_at": _clean(source.get("last_started_at")) or None,
        "last_finished_at": _clean(source.get("last_finished_at")) or None,
        "last_status": status,
        "last_error": _clean(source.get("last_error")) or None,
        "last_object_key": _clean(source.get("last_object_key")) or None,
    }


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_backup_object(key: object) -> bool:
    name = _clean(key).rsplit("/", 1)[-1]
    return name.startswith("backup-") and (name.endswith(".tar.gz") or name.endswith(".tar.gz.enc"))


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


BACKUP_WORK_DIR_PREFIX = ".backup-work-"
BACKUP_PBKDF2_ITERATIONS = 600_000
BACKUP_HMAC_KEY_CONTEXT = b"chatgpt2api-backup-hmac-v1"
BACKUP_MULTIPART_THRESHOLD_BYTES = 64 * 1024 * 1024
BACKUP_MULTIPART_PART_BYTES = 32 * 1024 * 1024
BACKUP_MIN_UPLOAD_TIMEOUT_SECS = 60.0
BACKUP_MIN_UPLOAD_BYTES_PER_SEC = 256 * 1024
BACKUP_FILE_CHUNK_BYTES = 1024 * 1024


def _upload_timeout(size: int) -> float:
    return max(BACKUP_MIN_UPLOAD_TIMEOUT_SECS, size / BACKUP_MIN_UPLOAD_BYTES_PER_SEC + 30.0)


def _openssl_encrypt_file(source: Path, target: Path, passphrase: str) -> None:
    env = dict(os.environ)
    env["CHATGPT2API_BACKUP_PASSPHRASE"] = passphrase
    try:
        subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-iter",
                str(BACKUP_PBKDF2_ITERATIONS),
                "-salt",
                "-md",
                "sha256",
                "-in",
                str(source),
                "-out",
                str(target),
                "-pass",
                "env:CHATGPT2API_BACKUP_PASSPHRASE",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise BackupError("当前环境缺少 openssl，无法执行加密备份") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise BackupError(f"加密备份失败：{detail or 'openssl 执行失败'}") from exc


def _ciphertext_hmac(path: Path, passphrase: str) -> str:
    """HMAC-SHA256 over an openssl `Salted__` file, keyed from the passphrase and its salt."""
    with path.open("rb") as source:
        header = source.read(16)
        if len(header) != 16 or not header.startswith(b"Salted__"):
            raise BackupError("加密备份失败：openssl 输出缺少盐值")
        key = hashlib.pbkdf2_hmac(
            "sha256",
            passphrase.encode("utf-8"),
            BACKUP_HMAC_KEY_CONTEXT + header[8:],
            BACKUP_PBKDF2_ITERATIONS,
        )
        digest = hmac.new(key, header, hashlib.sha256)
        while chunk := source.read(BACKUP_FILE_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _xml_text(document: str, tag: str) -> str:
    opening = f"<{tag}>"
    if opening not in document:
        return ""
    return _clean(document.split(opening, 1)[1].split(f"</{tag}>", 1)[0])


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


class BackupError(RuntimeError):
    pass


class CloudflareR2Client:
    def __init__(self, settings: dict[str, object]) -> None:
        self.account_id = _clean(settings.get("account_id"))
        self.access_key_id = _clean(settings.get("access_key_id"))
        self.secret_access_key = _clean(settings.get("secret_access_key"))
        self.bucket = _clean(settings.get("bucket"))
        self.prefix = _clean(settings.get("prefix")) or "backups"
        self.session = requests.Session(impersonate="chrome", verify=True)

    def validate(self) -> None:
        missing = []
        if not self.account_id:
            missing.append("Account ID")
        if not self.access_key_id:
            missing.append("Access Key ID")
        if not self.secret_access_key:
            missing.append("Secret Access Key")
        if not self.bucket:
            missing.append("Bucket")
        if missing:
            raise BackupError(f"R2 配置不完整：缺少 {'、'.join(missing)}")

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    def _aws_v4_headers(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        now = _utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        encoded_query = urlencode(sorted((query or {}).items()))
        payload_hash = _sha256_hex(body)
        host = f"{self.account_id}.r2.cloudflarestorage.com"
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if extra_headers:
            for key, value in extra_headers.items():
                headers[key.lower()] = value.strip()
        sorted_items = sorted((key.lower(), " ".join(str(value).strip().split())) for key, value in headers.items())
        canonical_headers = "".join(f"{key}:{value}\n" for key, value in sorted_items)
        signed_headers = ";".join(key for key, _ in sorted_items)
        canonical_request = "\n".join([
            method.upper(),
            path,
            encoded_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{date_stamp}/auto/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ])
        k_date = _hmac_sha256(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp)
        k_region = hmac.new(k_date, b"auto", hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        request_headers = {key: value for key, value in headers.items()}
        request_headers["authorization"] = authorization
        return encoded_query, request_headers

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ):
        object_path = f"/{self.bucket}"
        if key:
            object_path += f"/{quote(key.lstrip('/'), safe='/')}"
        encoded_query, headers = self._aws_v4_headers(method, object_path, query=query, body=body, extra_headers=extra_headers)
        url = f"{self.endpoint}{object_path}"
        if encoded_query:
            url += f"?{encoded_query}"
        response = self.session.request(method.upper(), url, headers=headers, data=body, timeout=timeout)
        return response

    def test_connection(self) -> dict[str, object]:
        self.validate()
        response = self._request("GET", query={"list-type": "2", "max-keys": "1"}, timeout=30.0)
        if response.status_code >= 400:
            raise BackupError(f"连接 R2 失败：HTTP {response.status_code}")
        return {"ok": True, "status": int(response.status_code)}

    def upload_file(self, key: str, path: Path, *, content_type: str, metadata: dict[str, str] | None = None) -> dict[str, object]:
        headers = {"content-type": content_type}
        if metadata:
            for item_key, item_value in metadata.items():
                headers[f"x-amz-meta-{item_key}"] = str(item_value)
        size = path.stat().st_size
        if size > BACKUP_MULTIPART_THRESHOLD_BYTES:
            return self._upload_multipart(key, path, headers=headers)
        response = self._request(
            "PUT",
            key,
            body=path.read_bytes(),
            extra_headers=headers,
            timeout=_upload_timeout(size),
        )
        if response.status_code >= 400:
            raise BackupError(f"上传备份失败：HTTP {response.status_code}")
        return {"key": key, "etag": str(response.headers.get("etag") or "").strip('"')}

    def _upload_multipart(self, key: str, path: Path, *, headers: dict[str, str]) -> dict[str, object]:
        response = self._request("POST", key, query={"uploads": ""}, extra_headers=headers, timeout=60.0)
        upload_id = _xml_text(response.text, "UploadId") if response.status_code < 400 else ""
        if not upload_id:
            raise BackupError(f"上传备份失败：无法创建分片上传（HTTP {response.status_code}）")
        try:
            parts: list[tuple[int, str]] = []
            with path.open("rb") as source:
                while chunk := source.read(BACKUP_MULTIPART_PART_BYTES):
                    part_number = len(parts) + 1
                    part = self._request(
                        "PUT",
                        key,
                        query={"partNumber": str(part_number), "uploadId": upload_id},
                        body=chunk,
                        timeout=_upload_timeout(len(chunk)),
                    )
                    etag = _clean(part.headers.get("etag"))
                    if part.status_code >= 400 or not etag:
                        raise BackupError(f"上传备份分片 {part_number} 失败：HTTP {part.status_code}")
                    parts.append((part_number, etag))
            body = "".join(
                f"<Part><PartNumber>{number}</PartNumber><ETag>{xml_escape(etag)}</ETag></Part>"
                for number, etag in parts
            )
            complete = self._request(
                "POST",
                key,
                query={"uploadId": upload_id},
                body=f"<CompleteMultipartUpload>{body}</CompleteMultipartUpload>".encode("utf-8"),
                extra_headers={"content-type": "application/xml"},
                timeout=120.0,
            )
            if complete.status_code >= 400 or "<Error>" in complete.text:
                raise BackupError(f"上传备份失败：无法完成分片上传（HTTP {complete.status_code}）")
            return {"key": key, "etag": _xml_text(complete.text, "ETag").strip('"')}
        except Exception:
            try:
                self._request("DELETE", key, query={"uploadId": upload_id}, timeout=30.0)
            except Exception:
                pass
            raise

    def delete_object(self, key: str) -> None:
        response = self._request("DELETE", key, timeout=30.0)
        if response.status_code >= 400 and response.status_code != 404:
            raise BackupError(f"删除备份失败：HTTP {response.status_code}")

    def list_objects(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        continuation = ""
        while True:
            query = {"list-type": "2", "prefix": f"{self.prefix.rstrip('/')}/", "max-keys": "1000"}
            if continuation:
                query["continuation-token"] = continuation
            response = self._request("GET", query=query, timeout=30.0)
            if response.status_code >= 400:
                raise BackupError(f"获取备份列表失败：HTTP {response.status_code}")
            text = response.text
            for block in text.split("<Contents>")[1:]:
                key = _clean(block.split("<Key>", 1)[1].split("</Key>", 1)[0]) if "<Key>" in block else ""
                if not key:
                    continue
                size_text = _clean(block.split("<Size>", 1)[1].split("</Size>", 1)[0]) if "<Size>" in block else "0"
                updated = _clean(block.split("<LastModified>", 1)[1].split("</LastModified>", 1)[0]) if "<LastModified>" in block else ""
                items.append({
                    "key": key,
                    "size": int(size_text or 0),
                    "updated_at": updated,
                })
            truncated = "<IsTruncated>true</IsTruncated>" in text
            if not truncated:
                break
            if "<NextContinuationToken>" not in text:
                break
            continuation = _clean(text.split("<NextContinuationToken>", 1)[1].split("</NextContinuationToken>", 1)[0])
            if not continuation:
                break
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return items

    def close(self) -> None:
        self.session.close()


class BackupService:
    def __init__(
        self,
        *,
        repository: BackupExecutionStateRepository | None = None,
        database_url: str | None = None,
    ) -> None:
        if repository is not None and database_url is not None:
            raise ValueError("provide repository or database_url, not both")
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._repository = repository or BackupExecutionStateRepository(database_url)
        self._recover_interrupted_execution()

    def _recover_interrupted_execution(self) -> None:
        try:
            with self._repository.run_lock(timeout_seconds=0):
                # Holding the lease means no backup is writing a work directory now.
                for leftover in DATA_DIR.glob(f"{BACKUP_WORK_DIR_PREFIX}*"):
                    shutil.rmtree(leftover, ignore_errors=True)
                state = _normalize_backup_state(self._repository.load())
                if state["last_status"] != "running":
                    return
                state.update({
                    "last_finished_at": _iso_now(),
                    "last_status": "error",
                    "last_error": "备份执行被进程重启中断",
                })
                self._repository.replace(state)
        except TimeoutError:
            # Another process still owns the backup execution lease.
            return

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="r2-backup-scheduler")
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._recover_stale_running_state()
                self.run_scheduled_backup_if_needed()
            except Exception:
                pass
            self._stop_event.wait(30)

    def _recover_stale_running_state(self) -> None:
        if self._running:
            return
        if _normalize_backup_state(self._repository.load())["last_status"] == "running":
            self._recover_interrupted_execution()

    def run_scheduled_backup_if_needed(self) -> None:
        settings = config.get_backup_settings()
        if not settings.get("enabled"):
            return
        state = self.get_status()
        if state.get("running"):
            return
        interval_minutes = int(settings.get("interval_minutes") or 360)
        last_finished_raw = _clean(state.get("last_finished_at"))
        if last_finished_raw:
            try:
                last_finished = datetime.fromisoformat(last_finished_raw.replace("Z", "+00:00"))
                elapsed = (_utc_now() - last_finished.astimezone(UTC)).total_seconds()
                if elapsed < interval_minutes * 60:
                    return
            except Exception:
                pass
        self.run_backup(trigger="schedule")

    def get_status(self) -> dict[str, object]:
        state = _normalize_backup_state(self._repository.load())
        return {**state, "running": self._running or state["last_status"] == "running"}

    def is_configured(self) -> bool:
        settings = config.get_backup_settings()
        return all([
            _clean(settings.get("account_id")),
            _clean(settings.get("access_key_id")),
            _clean(settings.get("secret_access_key")),
            _clean(settings.get("bucket")),
        ])

    def get_settings(self) -> dict[str, object]:
        settings = dict(config.get_backup_settings())
        settings["secret_access_key"] = "********" if _clean(settings.get("secret_access_key")) else ""
        settings["passphrase"] = "********" if _clean(settings.get("passphrase")) else ""
        return settings

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        current = config.get_backup_settings()
        merged = dict(current)
        merged.update(dict(payload or {}))
        if "include" in payload and isinstance(payload.get("include"), dict):
            include = dict(current.get("include") or {})
            include.update(payload.get("include") or {})
            merged["include"] = include
        if payload.get("secret_access_key") == "********":
            merged["secret_access_key"] = current.get("secret_access_key")
        if payload.get("passphrase") == "********":
            merged["passphrase"] = current.get("passphrase")
        updated = config.update({"backup": merged})
        return dict(updated.get("backup") or {})

    def test_connection(self) -> dict[str, object]:
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            return client.test_connection()
        finally:
            client.close()

    def list_backups(self) -> list[dict[str, object]]:
        if not self.is_configured():
            return []
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            items = client.list_objects()
        finally:
            client.close()
        parsed: list[dict[str, object]] = []
        for item in items:
            key = _clean(item.get("key"))
            name = key.rsplit("/", 1)[-1]
            encrypted = name.endswith(".enc")
            parsed.append({
                "key": key,
                "name": name,
                "size": int(item.get("size") or 0),
                "updated_at": item.get("updated_at"),
                "encrypted": encrypted,
            })
        return parsed

    def delete_backup(self, key: str) -> None:
        candidate = _clean(key)
        if not candidate:
            raise BackupError("备份对象 key 不能为空")
        client = CloudflareR2Client(config.get_backup_settings())
        try:
            client.delete_object(candidate)
        finally:
            client.close()

    def run_backup(self, *, trigger: str = "manual") -> dict[str, object]:
        try:
            with self._repository.run_lock(timeout_seconds=0):
                with self._lock:
                    current = self.get_status()
                    if self._running:
                        raise BackupError("当前已有备份任务正在执行")
                    started_at = _iso_now()
                    self._running = True
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": current.get("last_finished_at"),
                        "last_status": "running",
                        "last_error": None,
                        "last_object_key": current.get("last_object_key"),
                    })
                try:
                    result = self._run_backup_once(trigger=trigger)
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": _iso_now(),
                        "last_status": "success",
                        "last_error": None,
                        "last_object_key": result["key"],
                    })
                    return result
                except Exception as exc:
                    self._repository.replace({
                        "last_started_at": started_at,
                        "last_finished_at": _iso_now(),
                        "last_status": "error",
                        "last_error": str(exc) or exc.__class__.__name__,
                        "last_object_key": current.get("last_object_key"),
                    })
                    raise
                finally:
                    self._running = False
        except TimeoutError as exc:
            raise BackupError("当前已有备份任务正在执行") from exc

    def _run_backup_once(self, *, trigger: str) -> dict[str, object]:
        settings = config.get_backup_settings()
        client = CloudflareR2Client(settings)
        try:
            client.validate()
            encrypted = bool(settings.get("encrypt"))
            passphrase = _clean(settings.get("passphrase"))
            if encrypted and not passphrase:
                raise BackupError("已启用备份加密，但未设置加密口令")
            # On the data volume: archives can be large and the system temp dir small.
            with tempfile.TemporaryDirectory(prefix=BACKUP_WORK_DIR_PREFIX, dir=DATA_DIR) as workspace:
                archive_path = Path(workspace) / "backup.tar.gz"
                self._build_backup_archive(settings, trigger=trigger, destination=archive_path)
                metadata = {
                    "created-at": _iso_now(),
                    "encrypted": "true" if encrypted else "false",
                    "trigger": trigger,
                }
                if encrypted:
                    payload_path = Path(workspace) / "backup.tar.gz.enc"
                    _openssl_encrypt_file(archive_path, payload_path, passphrase)
                    archive_path.unlink(missing_ok=True)
                    metadata["kdf"] = f"pbkdf2-sha256-{BACKUP_PBKDF2_ITERATIONS}"
                    metadata["ciphertext-hmac-sha256"] = _ciphertext_hmac(payload_path, passphrase)
                    suffix = ".tar.gz.enc"
                else:
                    payload_path = archive_path
                    suffix = ".tar.gz"
                timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
                random_tag = f"{random.randint(0, 0xFFFF):04x}"
                object_key = f"{client.prefix.rstrip('/')}/backup-{timestamp}-{random_tag}{suffix}"
                size = payload_path.stat().st_size
                result = client.upload_file(
                    object_key,
                    payload_path,
                    content_type="application/octet-stream",
                    metadata=metadata,
                )
            self._apply_rotation(client, int(settings.get("rotation_keep") or 0))
            return {
                "key": result["key"],
                "size": size,
                "encrypted": encrypted,
            }
        finally:
            client.close()

    def _apply_rotation(self, client: CloudflareR2Client, keep: int) -> None:
        if keep <= 0:
            return
        items = [item for item in client.list_objects() if _is_backup_object(item.get("key"))]
        if len(items) <= keep:
            return
        for item in items[keep:]:
            key = _clean(item.get("key"))
            if key:
                client.delete_object(key)

    def _build_backup_archive(self, settings: dict[str, object], *, trigger: str, destination: Path) -> None:
        include = settings.get("include") if isinstance(settings.get("include"), dict) else {}
        database_backend = database_backend_name(self._repository.database_url)
        metadata = {
            "version": 3,
            "created_at": _iso_now(),
            "trigger": trigger,
            "app_version": config.app_version,
            "database_backend": database_backend,
            "application_database": config.get_storage_backend().get_backend_info(),
        }
        with tarfile.open(destination, mode="w:gz") as archive:
            self._add_bytes_to_archive(archive, "backup-metadata.json", _json_bytes(metadata))
            database_name = (
                "data/application-database.sqlite3"
                if database_backend == "sqlite"
                else "data/application-database.pgdump"
            )
            database_snapshot = destination.with_name("application-database.snapshot")
            try:
                self._application_database_backup(database_backend, database_snapshot)
                archive.add(database_snapshot, arcname=database_name)
            finally:
                database_snapshot.unlink(missing_ok=True)
            if include.get("image_tasks"):
                self._add_file_to_archive(archive, DATA_DIR / "image_tasks.json", "data/image_tasks.json")
                self._add_file_to_archive(archive, IMAGE_INDEX_FILE, "data/image_index.json")
            if include.get("editable_files"):
                self._add_directory_to_archive(archive, DATA_DIR / "files", "data/files")
            if include.get("images"):
                self._add_file_to_archive(archive, TAGS_FILE, "data/image_tags.json")
                self._add_directory_to_archive(archive, config.images_dir, "data/images")

    def _application_database_backup(self, backend: str, destination: Path) -> None:
        if backend == "sqlite":
            return self._sqlite_database_backup(destination)
        if backend == "postgresql":
            return self._postgresql_database_backup(destination)
        raise BackupError(f"不支持备份数据库类型：{backend}")

    def _sqlite_database_backup(self, destination_path: Path) -> None:
        raw_connection = self._repository.engine.raw_connection()
        try:
            destination = sqlite3.connect(str(destination_path))
            try:
                raw_connection.driver_connection.backup(destination)
            finally:
                destination.close()
        except Exception as exc:
            raise BackupError("创建 SQLite Application Database 快照失败") from exc
        finally:
            raw_connection.close()

    def _postgresql_database_backup(self, destination: Path) -> None:
        url = make_url(self._repository.database_url)
        command = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(destination)]
        if url.host:
            command.extend(["--host", url.host])
        if url.port:
            command.extend(["--port", str(url.port)])
        if url.username:
            command.extend(["--username", url.username])
        if url.database:
            command.extend(["--dbname", url.database])

        env = dict(os.environ)
        if url.password:
            env["PGPASSWORD"] = url.password
        sslmode = str(url.query.get("sslmode") or "").strip()
        if sslmode:
            env["PGSSLMODE"] = sslmode
        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise BackupError("当前环境缺少 pg_dump，无法备份 PostgreSQL") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise BackupError(f"PostgreSQL 备份失败：{detail or 'pg_dump 执行失败'}") from exc
        if not destination.is_file() or destination.stat().st_size == 0:
            raise BackupError("PostgreSQL 备份失败：pg_dump 未输出数据")

    def _add_bytes_to_archive(self, archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        info.mtime = int(_utc_now().timestamp())
        archive.addfile(info, io.BytesIO(payload))

    def _add_file_to_archive(self, archive: tarfile.TarFile, source: Path, arcname: str) -> None:
        if not source.exists() or not source.is_file():
            return
        archive.add(source, arcname=arcname)

    def _add_directory_to_archive(self, archive: tarfile.TarFile, source_dir: Path, arcname_root: str) -> None:
        if not source_dir.exists() or not source_dir.is_dir():
            return
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                relative = path.relative_to(source_dir).as_posix()
                archive.add(path, arcname=f"{arcname_root}/{relative}")


backup_service = BackupService()
