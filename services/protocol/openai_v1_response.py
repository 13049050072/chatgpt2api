from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.protocol.chat_completion_cache import cache_key, chat_completion_cache, normalize_text_messages
from services.protocol.conversation import (
    ConversationRequest,
    MISSING_IMAGE_OPTION,
    ImageOutput,
    count_message_image_tokens,
    count_message_text_tokens,
    count_text_tokens,
    encode_images,
    normalize_messages,
    stream_image_outputs_with_pool,
    validate_image_output_request,
    stream_text_deltas,
    text_backend,
)
from services.protocol.reasoning import thinking_effort_from_body
from services.protocol.web_search_tool import (
    WEB_SEARCH_TOOL_TYPES,
    has_unsupported_tools,
    has_web_search_tool,
    normalized_sources,
    run_web_search,
    search_query_from_messages,
    text_with_url_citations,
)
from utils.helper import extract_image_from_message_content, extract_response_prompt, has_response_image_generation_tool
from utils.image_tokens import (
    count_image_content_tokens,
    count_image_output_items_tokens,
    image_usage,
    image_output_metadata,
    token_usage,
)

TOOL_UNAVAILABLE_SYSTEM_MESSAGE = (
    "This compatibility backend cannot execute local tools, shell commands, non-search tools, "
    "or file operations. Do not claim to have run tools or inspected external resources. "
    "If a user asks you to use a tool, say that tool execution is unavailable through this backend."
)

RESPONSE_CONTENT_PART_TYPES = {"text", "input_text", "output_text", "image_url", "input_image", "image"}

STORED_RESPONSE_TTL_SECONDS = 60 * 60
STORED_RESPONSE_LIMIT = 256
STORED_RESPONSE_MAX_CHARS = 4 * 1024 * 1024
STORED_RESPONSE_TOTAL_CHARS = 64 * 1024 * 1024
STORED_RESPONSE_OWNER_LIMIT = 64
STORED_RESPONSE_OWNER_CHARS = 16 * 1024 * 1024


class _StoredResponses:
    """Owner-scoped, in-process conversation history for previous_response_id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, str, list[dict[str, Any]], int]] = OrderedDict()
        self._total_chars = 0
        self._owner_items: dict[str, OrderedDict[str, None]] = {}
        self._owner_chars: dict[str, int] = {}

    def _pop_locked(self, response_id: str) -> None:
        item = self._items.pop(response_id, None)
        if item is None:
            return
        _expires_at, owner, _messages, size = item
        self._total_chars -= size
        owned = self._owner_items.get(owner)
        if owned is not None:
            owned.pop(response_id, None)
            self._owner_chars[owner] = self._owner_chars.get(owner, 0) - size
            if not owned:
                del self._owner_items[owner]
                self._owner_chars.pop(owner, None)

    def put(self, response_id: str, owner: str, messages: list[dict[str, Any]]) -> None:
        try:
            size = len(json.dumps(messages, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            return
        if size > STORED_RESPONSE_MAX_CHARS:
            return
        expires_at = time.monotonic() + STORED_RESPONSE_TTL_SECONDS
        with self._lock:
            self._pop_locked(response_id)
            self._items[response_id] = (expires_at, owner, messages, size)
            self._total_chars += size
            owned = self._owner_items.setdefault(owner, OrderedDict())
            owned[response_id] = None
            self._owner_chars[owner] = self._owner_chars.get(owner, 0) + size
            # Each caller is trimmed to its own share first, so a single key cannot
            # push other callers' history out of the shared store.
            while owned and (
                len(owned) > STORED_RESPONSE_OWNER_LIMIT
                or self._owner_chars.get(owner, 0) > STORED_RESPONSE_OWNER_CHARS
            ):
                self._pop_locked(next(iter(owned)))
            while self._items and (
                len(self._items) > STORED_RESPONSE_LIMIT
                or self._total_chars > STORED_RESPONSE_TOTAL_CHARS
            ):
                self._pop_locked(next(iter(self._items)))

    def get(self, response_id: str, owner: str) -> list[dict[str, Any]] | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(response_id)
            if item is None:
                return None
            expires_at, stored_owner, messages, _size = item
            if expires_at <= now:
                self._pop_locked(response_id)
                return None
            if stored_owner != owner:
                return None
            return list(messages)


stored_responses = _StoredResponses()


def _response_owner(body: dict[str, Any]) -> str:
    return str(body.get("_caller_key_id") or "")


def _previous_response_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    previous_id = str(body.get("previous_response_id") or "").strip()
    if not previous_id:
        return []
    messages = stored_responses.get(previous_id, _response_owner(body))
    if messages is None:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Previous response with id '{previous_id}' not found."},
        )
    return messages


def _remember_response(body: dict[str, Any], response_id: str, reply_text: str) -> None:
    if body.get("store") is False:
        return
    conversation = body.get("_conversation_messages")
    if not isinstance(conversation, list):
        return
    stored_responses.put(
        response_id,
        _response_owner(body),
        [*conversation, {"role": "assistant", "content": reply_text}],
    )


def is_text_response_request(body: dict[str, Any]) -> bool:
    return not has_response_image_generation_tool(body)


def has_unsupported_response_tools(body: dict[str, Any]) -> bool:
    return has_unsupported_tools(body, {"image_generation", *WEB_SEARCH_TOOL_TYPES})


def response_image_tool(body: dict[str, Any]) -> dict[str, object]:
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "image_generation":
            return tool
    return {}


def extract_response_image(input_value: object, *, image_cache_scope: str = "") -> tuple[bytes, str] | None:
    if isinstance(input_value, dict):
        if str(input_value.get("type") or "").strip() == "input_image":
            images = extract_image_from_message_content([input_value], image_cache_scope=image_cache_scope)
            return images[0] if images else None
        images = extract_image_from_message_content(input_value.get("content"), image_cache_scope=image_cache_scope)
        return images[0] if images else None
    if not isinstance(input_value, list):
        return None
    for item in reversed(input_value):
        if isinstance(item, dict):
            if str(item.get("type") or "").strip() == "input_image":
                images = extract_image_from_message_content([item], image_cache_scope=image_cache_scope)
                if images:
                    return images[0]
            images = extract_image_from_message_content(item.get("content"), image_cache_scope=image_cache_scope)
            if images:
                return images[0]
    return None


def _input_image_parts(input_value: object) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if isinstance(input_value, dict):
        content = input_value.get("content")
        if isinstance(content, list):
            parts.extend(item for item in content if isinstance(item, dict))
        return parts
    if not isinstance(input_value, list):
        return parts
    if all(isinstance(item, dict) and item.get("type") for item in input_value):
        return [item for item in input_value if isinstance(item, dict)]
    for item in input_value:
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, list):
                parts.extend(part for part in content if isinstance(part, dict))
    return parts


def _is_response_content_part(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    part_type = str(value.get("type") or "").strip()
    return part_type in RESPONSE_CONTENT_PART_TYPES or ("image_url" in value and part_type != "message")


def _message_content_from_response_item(item: dict[str, Any]) -> object:
    content = item.get("content")
    if isinstance(content, list):
        return [dict(part) if isinstance(part, dict) else part for part in content]
    if isinstance(content, str):
        return content
    return extract_response_prompt([item]) or content or ""


def _append_response_message(messages: list[dict[str, Any]], role: object, content: object) -> None:
    if isinstance(content, str):
        if content.strip():
            messages.append({"role": str(role or "user"), "content": content.strip()})
        return
    if isinstance(content, list) and content:
        messages.append({"role": str(role or "user"), "content": content})


def messages_from_input(input_value: object, instructions: object = None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system_text = str(instructions or "").strip()
    if system_text:
        messages.append({"role": "system", "content": system_text})
    if isinstance(input_value, str):
        if input_value.strip():
            messages.append({"role": "user", "content": input_value.strip()})
        return messages
    if isinstance(input_value, dict):
        if _is_response_content_part(input_value):
            _append_response_message(messages, "user", [dict(input_value)])
            return messages
        _append_response_message(
            messages,
            input_value.get("role") or "user",
            _message_content_from_response_item(input_value),
        )
        return messages
    if isinstance(input_value, list):
        if all(_is_response_content_part(item) for item in input_value):
            _append_response_message(messages, "user", [dict(item) for item in input_value if isinstance(item, dict)])
            return messages
        pending_parts: list[dict[str, Any]] = []
        for item in input_value:
            if _is_response_content_part(item):
                pending_parts.append(dict(item))
                continue
            if pending_parts:
                _append_response_message(messages, "user", pending_parts)
                pending_parts = []
            if not isinstance(item, dict):
                continue
            _append_response_message(
                messages,
                item.get("role") or "user",
                _message_content_from_response_item(item),
            )
        if pending_parts:
            _append_response_message(messages, "user", pending_parts)
    return messages


def text_output_item(
    text: str,
    item_id: str | None = None,
    status: str = "completed",
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": item_id or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": annotations or []}],
    }


def web_search_call_item(
    query: str,
    item_id: str | None = None,
    status: str = "completed",
    sources: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "type": "search",
        "query": query,
        "queries": [query],
    }
    if sources:
        action["sources"] = [
            {"type": "url", "url": source["url"]}
            for source in sources
            if source.get("url")
        ]
    return {
        "id": item_id or f"ws_{uuid.uuid4().hex}",
        "type": "web_search_call",
        "status": status,
        "action": action,
    }


def image_output_items(prompt: str, data: list[dict[str, Any]], item_id: str | None = None) -> list[dict[str, Any]]:
    output = []
    for item in data:
        b64_json = str(item.get("b64_json") or "").strip()
        if b64_json:
            output.append({
                "id": item_id or f"ig_{len(output) + 1}",
                "type": "image_generation_call",
                "status": "completed",
                "result": b64_json,
                "revised_prompt": str(item.get("revised_prompt") or prompt).strip() or prompt,
            })
    return output


def response_created(response_id: str, model: str, created: int) -> dict[str, Any]:
    return {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "in_progress",
            "error": None,
            "incomplete_details": None,
            "model": model,
            "output": [],
            "parallel_tool_calls": False,
        },
    }


def response_completed(
    response_id: str,
    model: str,
    created: int,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "model": model,
            "output": output,
            "parallel_tool_calls": False,
        },
    }
    if usage:
        response["response"]["usage"] = usage
    return response


def _with_log_metadata(
    payload: dict[str, Any],
    account_email: str = "",
    conversation_id: str = "",
    image_urls: Iterable[str] | None = None,
    image_attempts: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if account_email:
        payload["_account_email"] = account_email
    if conversation_id:
        payload["_conversation_id"] = conversation_id
    urls = [str(url).strip() for url in image_urls or [] if str(url).strip()]
    if urls:
        payload["_image_urls"] = list(dict.fromkeys(urls))
    attempts = [dict(item) for item in image_attempts or [] if isinstance(item, dict)]
    if attempts:
        payload["_image_attempts"] = attempts
    return payload


def _backend_account_email(backend: object) -> str:
    return str(getattr(backend, "account_email", "") or "").strip()


def text_response_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    history = _previous_response_messages(body)
    current = messages_from_input(body.get("input"))
    # Instructions are not carried into later turns; each request supplies its own.
    body["_conversation_messages"] = [*history, *current]
    conversation = [*messages_from_input(None, body.get("instructions")), *history, *current]
    messages = normalize_text_messages(normalize_messages(conversation, image_cache_scope=_response_owner(body)))
    if has_unsupported_response_tools(body):
        messages.insert(0, {"role": "system", "content": TOOL_UNAVAILABLE_SYSTEM_MESSAGE})
    return model, messages


def stream_text_response(backend, body: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    messages = messages if messages is not None else messages_from_input(body.get("input"), body.get("instructions"))
    thinking_effort = thinking_effort_from_body(body)
    response_id = f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())
    full_text = ""
    yield response_created(response_id, model, created)
    yield {"type": "response.output_item.added", "output_index": 0, "item": text_output_item("", item_id, "in_progress")}
    request = ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort)
    for delta in stream_text_deltas(backend, request):
        full_text += delta
        yield _with_log_metadata(
            {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": delta},
            _backend_account_email(backend),
        )
    yield _with_log_metadata(
        {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": full_text},
        _backend_account_email(backend),
    )
    item = text_output_item(full_text, item_id, "completed")
    yield _with_log_metadata({"type": "response.output_item.done", "output_index": 0, "item": item}, _backend_account_email(backend))
    usage = token_usage(
        input_text_tokens=count_message_text_tokens(messages, model),
        input_image_tokens=count_message_image_tokens(messages, model),
        output_text_tokens=count_text_tokens(full_text, model),
    )
    completed = response_completed(response_id, model, created, [item], usage)
    _with_log_metadata(completed, _backend_account_email(backend))
    _with_log_metadata(completed["response"], _backend_account_email(backend))
    _remember_response(body, response_id, full_text)
    yield completed


def stream_web_search_response(body: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    messages = messages if messages is not None else messages_from_input(body.get("input"), body.get("instructions"))
    query = search_query_from_messages(messages) or extract_response_prompt(body.get("input"))
    if not query:
        raise HTTPException(status_code=400, detail={"error": "input text is required for web_search"})

    response_id = f"resp_{uuid.uuid4().hex}"
    search_id = f"ws_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())
    yield response_created(response_id, model, created)

    searching_item = web_search_call_item(query, search_id, "in_progress")
    yield {"type": "response.output_item.added", "output_index": 0, "item": searching_item}
    yield {"type": "response.web_search_call.in_progress", "output_index": 0, "item_id": search_id}
    yield {"type": "response.web_search_call.searching", "output_index": 0, "item_id": search_id}
    result = run_web_search(query)
    search_item = web_search_call_item(query, search_id, "completed", normalized_sources(result))
    yield {"type": "response.web_search_call.completed", "output_index": 0, "item_id": search_id}
    yield {"type": "response.output_item.done", "output_index": 0, "item": search_item}

    text, annotations = text_with_url_citations(result)
    message_item = text_output_item("", item_id, "in_progress", annotations)
    yield {"type": "response.output_item.added", "output_index": 1, "item": message_item}
    if text:
        yield {"type": "response.output_text.delta", "item_id": item_id, "output_index": 1, "content_index": 0, "delta": text}
    yield {"type": "response.output_text.done", "item_id": item_id, "output_index": 1, "content_index": 0, "text": text}
    message_item = text_output_item(text, item_id, "completed", annotations)
    yield {"type": "response.output_item.done", "output_index": 1, "item": message_item}
    usage = token_usage(
        input_text_tokens=count_message_text_tokens(messages, model),
        input_image_tokens=count_message_image_tokens(messages, model),
        output_text_tokens=count_text_tokens(text, model),
    )
    completed = response_completed(response_id, model, created, [search_item, message_item], usage)
    _remember_response(body, response_id, text)
    yield completed


def stream_image_response(
    image_outputs: Iterable[ImageOutput],
    prompt: str,
    model: str,
    input_image_tokens: int = 0,
    size: object = None,
    quality: str = "auto",
) -> Iterator[dict[str, Any]]:
    response_id = f"resp_{uuid.uuid4().hex}"
    created = int(time.time())
    yield response_created(response_id, model, created)
    for output in image_outputs:
        if output.kind == "message":
            text = output.text
            item = text_output_item(text)
            usage = token_usage(
                input_text_tokens=count_text_tokens(prompt, model),
                input_image_tokens=input_image_tokens,
                output_text_tokens=count_text_tokens(text, model),
            )
            yield _with_log_metadata(
                {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text},
                output.account_email,
                output.conversation_id,
                image_attempts=output.image_attempts,
            )
            yield _with_log_metadata(
                {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "text": text},
                output.account_email,
                output.conversation_id,
                image_attempts=output.image_attempts,
            )
            yield _with_log_metadata(
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                output.account_email,
                output.conversation_id,
                image_attempts=output.image_attempts,
            )
            completed = response_completed(response_id, model, created, [item], usage)
            _with_log_metadata(completed, output.account_email, output.conversation_id, image_attempts=output.image_attempts)
            _with_log_metadata(completed["response"], output.account_email, output.conversation_id, image_attempts=output.image_attempts)
            yield completed
            return
        if output.kind != "result":
            continue
        items = image_output_items(prompt, output.data)
        if items:
            usage = image_usage(
                input_text_tokens=count_text_tokens(prompt, model),
                input_image_tokens=input_image_tokens,
                output_tokens=count_image_output_items_tokens(output.data, size, quality),
            )
            for output_index, item in enumerate(items):
                yield _with_log_metadata(
                    {"type": "response.output_item.done", "output_index": output_index, "item": item},
                    output.account_email,
                    output.conversation_id,
                    output.image_urls,
                    output.image_attempts,
                )
            completed = response_completed(response_id, model, created, items, usage)
            _with_log_metadata(completed, output.account_email, output.conversation_id, output.image_urls, output.image_attempts)
            _with_log_metadata(completed["response"], output.account_email, output.conversation_id, output.image_urls, output.image_attempts)
            completed["response"]["_image_metadata"] = image_output_metadata(output.data)
            yield completed
            return
    raise RuntimeError("image generation failed")


def collect_response(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    completed = {}
    for event in events:
        if event.get("type") == "response.completed":
            completed = event.get("response") if isinstance(event.get("response"), dict) else {}
    if not completed:
        raise RuntimeError("response generation failed")
    return completed


def response_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if is_text_response_request(body):
        model, messages = text_response_parts(body)
        if has_web_search_tool(body) and not has_unsupported_response_tools(body):
            yield from stream_web_search_response(body, messages)
            return
        key = cache_key(body, messages, stream=bool(body.get("stream")))
        yield from chat_completion_cache.get_or_compute_stream(
            key,
            lambda: stream_text_response(text_backend(), body, messages),
        )
        return

    if str(body.get("previous_response_id") or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "previous_response_id is not supported with the image_generation tool"},
        )
    prompt = extract_response_prompt(body.get("input"))
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "input text is required"})
    model = str(body.get("model") or "gpt-image-2").strip() or "gpt-image-2"
    image_info = extract_response_image(body.get("input"), image_cache_scope=_response_owner(body))
    if image_info:
        image_data, mime_type = image_info
        images = encode_images([(image_data, "image.png", mime_type)])
    else:
        images = None
    input_image_tokens = count_image_content_tokens(_input_image_parts(body.get("input")), model)
    tool = response_image_tool(body)
    output_format = tool.get("output_format")
    frame_count = tool.get("frame_count", MISSING_IMAGE_OPTION)
    frame_duration_ms = tool.get("frame_duration_ms", MISSING_IMAGE_OPTION)
    validate_image_output_request(
        model,
        output_format,
        stream=bool(body.get("stream")),
        has_references=bool(images),
    )
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        size=tool.get("size"),
        quality=str(tool.get("quality") or "auto"),
        response_format="b64_json",
        output_format=str(output_format) if output_format is not None else None,
        frame_count=frame_count,
        frame_duration_ms=frame_duration_ms,
        images=images,
        message_as_error=True,
        call_id=str(body.get("_call_id") or ""),
        trace_image_perf=bool(body.get("_trace_image_perf")),
    ))
    yield from stream_image_response(image_outputs, prompt, model, input_image_tokens, tool.get("size"), str(tool.get("quality") or "auto"))


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    if body.get("stream") and has_response_image_generation_tool(body):
        tool = response_image_tool(body)
        validate_image_output_request(
            str(body.get("model") or "gpt-image-2").strip() or "gpt-image-2",
            tool.get("output_format"),
            stream=True,
        )
    events = response_events(body)
    if body.get("stream"):
        return events
    return collect_response(events)
