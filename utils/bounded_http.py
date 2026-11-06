from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


REMOTE_JSON_MAX_BYTES = 32 * 1024 * 1024


class ResponseTooLargeError(RuntimeError):
    pass


def require_http_url(url: object) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must start with http:// or https://")
    return value


def response_succeeded(response: Any) -> bool:
    """curl_cffi counts 3xx as ok; with redirects disabled they are failures too."""
    try:
        return 200 <= int(getattr(response, "status_code", 0) or 0) < 300
    except (TypeError, ValueError):
        return False


def close_streamed_response(response: Any) -> None:
    # Synchronous on purpose: callers close the owning session next, and the
    # transfer thread must not outlive the curl handle it runs on.
    try:
        quit_now = getattr(response, "quit_now", None)
        if quit_now is not None:
            quit_now.set()
    except Exception:
        pass
    try:
        response.close()
    except Exception:
        pass


def read_response_limited(response: Any, *, max_bytes: int) -> bytes:
    """Read a ``stream=True`` curl_cffi response without buffering past ``max_bytes``."""
    try:
        content_length = str(response.headers.get("content-length") or "").strip()
        if content_length.isdigit() and int(content_length) > max_bytes:
            raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        received = 0
        for chunk in response.iter_content():
            if not chunk:
                continue
            received += len(chunk)
            if received > max_bytes:
                raise ResponseTooLargeError(f"response exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        close_streamed_response(response)


def read_json_limited(response: Any, *, max_bytes: int = REMOTE_JSON_MAX_BYTES) -> Any:
    return json.loads(read_response_limited(response, max_bytes=max_bytes))


def read_text_prefix(response: Any, limit: int = 200) -> str:
    """Return the start of a streamed error body for diagnostics, then close it."""
    try:
        buffer = b""
        for chunk in response.iter_content():
            buffer += chunk or b""
            if len(buffer) >= limit:
                break
        return buffer[:limit].decode("utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        close_streamed_response(response)
