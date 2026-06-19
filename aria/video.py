# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""The right-hand pane: turn each scene into a REAL talking video.

Aria's world is shown with live text-to-video generation on Alibaba's
**HappyHorse-1.0** model (DashScope / Model Studio) — native 720p/1080p video with
integrated, lip-synced audio, so the guide actually *speaks* the line on screen.
The same ``DASHSCOPE_API_KEY`` that powers the Qwen brain powers the video too.

DashScope video is asynchronous: **create a task, then poll** until it's ready.
  • ``HappyHorseBackend`` — the real text-to-video, create + poll.
  • ``PlaceholderBackend`` — a soft scene card, used ONLY as a fallback if a call
    fails, never as the headline experience.

There is no advertised "run without a key" path: Aria needs a DashScope key.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

log = logging.getLogger("aria.video")


@dataclass(frozen=True, slots=True)
class VideoClip:
    """What the right pane shows for one beat."""

    turn: int
    speaker: str            # "guide"
    kind: str               # "video" | "placeholder"
    caption: str            # the guide's spoken line
    scene: str              # the video prompt / scene text
    url: str = ""           # generated video URL when kind == "video"
    audio_url: str = ""     # empty — HappyHorse bakes audio into the video file
    meta: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return asdict(self)


class VideoBackend:
    name = "base"

    async def render_scene(self, *, turn: int, prompt: str, caption: str, emotion: str) -> VideoClip:
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover
        pass


class PlaceholderBackend(VideoBackend):
    """Fallback only — a scene card if video generation is unavailable."""

    name = "placeholder"

    async def render_scene(self, *, turn: int, prompt: str, caption: str, emotion: str) -> VideoClip:
        return VideoClip(turn=turn, speaker="guide", kind="placeholder", caption=caption,
                         scene=prompt, meta={"emotion": emotion,
                                             "reason": "video generation unavailable — add a DashScope key"})


class HappyHorseBackend(VideoBackend):
    """Alibaba HappyHorse-1.0 text-to-video (DashScope Model Studio), async.

    Create:  POST {base}/api/v1/services/aigc/video-generation/video-synthesis
             header  X-DashScope-Async: enable
             {"model","input":{"prompt"},"parameters":{"resolution","duration","audio"}}
             → output.task_id
    Poll:    GET  {base}/api/v1/tasks/{task_id}  until task_status SUCCEEDED/FAILED
             → the video URL (a file with lip-synced audio baked in).
    """

    name = "happyhorse"

    def __init__(self, base_url: str, api_key: str, model: str, *,
                 resolution: str, duration: int, timeout_s: int, poll_s: float) -> None:
        self.model = model
        self.resolution = resolution
        self.duration = duration
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._fallback = PlaceholderBackend()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def render_scene(self, *, turn: int, prompt: str, caption: str, emotion: str) -> VideoClip:
        # The character performs the scene briefly, then turns to the viewer and
        # waits — looking right at you, gesturing, ready for your reply.
        full = (f"{prompt} The character performs this briefly, then turns and looks "
                f"directly at the viewer, attentive and waiting for a reply, with natural "
                f"gestures — pointing to show the way when relevant. They speak, lip-synced "
                f'with natural audio, saying: "{caption}". '
                f"Mood: {emotion}. About 5–6 seconds, cinematic, photoreal, smooth motion.")
        try:
            task_id = await self._create(full)
            url = await self._poll(task_id)
            if not url:
                raise ValueError("no video url in finished task")
            return VideoClip(turn=turn, speaker="guide", kind="video", caption=caption,
                             scene=prompt, url=url, meta={"model": self.model, "emotion": emotion})
        except Exception as e:  # noqa: BLE001 — never lose a beat to the video layer
            log.warning("HappyHorse render failed (beat %d), placeholder: %s", turn, e)
            clip = await self._fallback.render_scene(turn=turn, prompt=prompt, caption=caption, emotion=emotion)
            return VideoClip(**{**clip.public(), "meta": {**clip.meta, "happyhorse_error": str(e)}})

    async def _create(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "input": {"prompt": prompt},
            "parameters": {"resolution": self.resolution, "duration": self.duration, "audio": True},
        }
        resp = await self._client.post(
            "/api/v1/services/aigc/video-generation/video-synthesis",
            json=payload, headers={"X-DashScope-Async": "enable"})
        if resp.status_code >= 400:
            raise ValueError(f"create HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise ValueError(f"no task_id: {str(data)[:200]}")
        return task_id

    async def _poll(self, task_id: str) -> str:
        deadline = asyncio.get_event_loop().time() + self.timeout_s
        while asyncio.get_event_loop().time() < deadline:
            resp = await self._client.get(f"/api/v1/tasks/{task_id}")
            if resp.status_code >= 400:
                raise ValueError(f"poll HTTP {resp.status_code}: {resp.text[:200]}")
            out = resp.json().get("output") or {}
            status = (out.get("task_status") or "").upper()
            if status == "SUCCEEDED":
                return _extract_video(out)
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise ValueError(f"task {status}: {str(out)[:200]}")
            await asyncio.sleep(self.poll_s)
        raise ValueError(f"video generation timed out after {self.timeout_s}s")

    async def aclose(self) -> None:
        await self._client.aclose()


def _extract_video(out: dict[str, Any]) -> str:
    paths = (
        lambda o: o["video_url"],
        lambda o: o["results"][0]["url"],
        lambda o: o["results"][0]["video_url"],
        lambda o: o["results"]["video_url"],
    )
    for path in paths:
        try:
            url = path(out)
            if isinstance(url, str) and url.startswith("http"):
                return url
        except (KeyError, IndexError, TypeError):
            continue
    return ""


def build_video_backend() -> VideoBackend:
    """Build the video backend. Uses the DashScope key (shared with the brain).

    ``ARIA_VIDEO_MODE``: ``auto`` (HappyHorse if a key exists, else placeholder) |
    ``happyhorse`` (force) | ``placeholder`` (force the fallback).
    """
    mode = os.environ.get("ARIA_VIDEO_MODE", "auto").strip().lower()
    key = (os.environ.get("ARIA_VIDEO_KEY") or os.environ.get("QWEN_API_KEY")
           or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    base = os.environ.get("ARIA_VIDEO_BASE_URL", "https://dashscope-intl.aliyuncs.com").strip()
    model = os.environ.get("ARIA_VIDEO_MODEL", "happyhorse-1.0-t2v").strip()
    resolution = os.environ.get("ARIA_VIDEO_RESOLUTION", "1080P").strip()
    try:
        duration = int(os.environ.get("ARIA_VIDEO_DURATION", "5"))
    except ValueError:
        duration = 5
    try:
        timeout_s = int(os.environ.get("ARIA_VIDEO_TIMEOUT", "240"))
    except ValueError:
        timeout_s = 240

    if mode == "placeholder" or (mode == "auto" and not key):
        log.info("Aria scene backend: placeholder (no DashScope key yet — add one to generate video)")
        return PlaceholderBackend()
    log.info("Aria scene backend: HappyHorse video model=%s %s %ds", model, resolution, duration)
    return HappyHorseBackend(base, key, model, resolution=resolution, duration=duration,
                             timeout_s=timeout_s, poll_s=3.0)
