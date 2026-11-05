from __future__ import annotations

from io import BytesIO
from PIL import Image

# Bound decompressed work even when upstream returns an unexpectedly large GIF.
MAX_ANIMATION_PIXELS = 64_000_000
MAX_SOURCE_FRAMES = 256


def prepare_gif(data: bytes, frame_count: int | None = None, duration_ms: int | None = None) -> bytes | None:
    if not data.startswith((b"GIF87a", b"GIF89a")):
        return None
    with Image.open(BytesIO(data)) as image:
        count = getattr(image, "n_frames", 1)
        if count < 2:
            return None
        target_count = frame_count or count
        if count > MAX_SOURCE_FRAMES or image.width * image.height * max(count, target_count) > MAX_ANIMATION_PIXELS:
            raise ValueError("animation exceeds processing limits")
        if frame_count is None and duration_ms is None:
            return data
        indices = [round(i * (count - 1) / (target_count - 1)) for i in range(target_count)] if target_count <= count else [i % count for i in range(target_count)]
        frames = []
        durations = []
        for index in indices:
            image.seek(index)
            frames.append(image.convert("RGBA"))
            source_duration = image.info.get("duration", 100)
            durations.append(duration_ms if duration_ms is not None else max(10, int(source_duration or 100)))
        result = BytesIO()
        frames[0].save(result, format="GIF", save_all=True, append_images=frames[1:], duration=durations, loop=image.info.get("loop", 0), optimize=False)
        return result.getvalue()


def prompt_prefix(frame_count: int | None, duration_ms: int | None) -> str:
    parts = ["GIF动图"]
    if frame_count is not None:
        parts.append(f"{frame_count}帧")
    if duration_ms is not None:
        parts.append(f"每帧{duration_ms}毫秒")
    return "【制作要求：" + "，".join(parts) + "。】"
