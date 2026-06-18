# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""The right-hand pane: turn dialogue into video.

A pluggable backend so Aria runs *today* without a key and lights up for real
when one is present:

  • ``HappyHorseBackend`` — Alibaba's HappyHorse-1.0 text-to-video (synced audio,
    character-consistent across turns). Best-effort adapter; downgrades to the
    placeholder on any failure so a turn is never lost.
  • ``PlaceholderBackend`` — returns a "scene card": the outdoor scene description
    + the spoken caption + a soft animated gradient. The split-screen UI stays
    whole even with no video key.

Selection (``ARIA_VIDEO_MODE``): ``auto`` (HappyHorse if a key exists, else
placeholder), ``happyhorse`` (force), or ``placeholder`` (force the demo mode).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

log = logging.getLogger("aria.video")

# The couple, fixed for character consistency across turns. Reference text now;
# swap to reference images when HappyHorse reference-to-video is wired.
COUPLE_SCENE = (
    "A young couple sitting close on a wooden bench in a sunlit park, late "
    "afternoon golden light, soft bokeh of trees behind them, occasional birds "
    "passing, gentle ambient sound. Natural, warm, cinematic. They are having a "
    "relaxed conversation."
)


@dataclass(frozen=True, slots=True)
class VideoClip:
    """What the right pane needs to show one turn."""

    turn: int
    speaker: str            # "a" | "b"
    kind: str               # "video" | "placeholder"
    caption: str            # the line being spoken
    scene: str              # the visual prompt / scene card text
    url: str = ""           # clip URL when kind == "video"
    audio_url: str = ""     # synced audio when provided separately
    meta: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return asdict(self)


class VideoBackend:
    """Interface every backend implements."""

    name = "base"

    async def render_turn(self, *, turn: int, speaker: str, line: str, topic: str) -> VideoClip:
        raise NotImplementedError

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        pass


class PlaceholderBackend(VideoBackend):
    """No key needed. A tasteful scene card so the UI is always whole."""

    name = "placeholder"

    async def render_turn(self, *, turn: int, speaker: str, line: str, topic: str) -> VideoClip:
        who = "She" if speaker == "b" else "He"
        scene = f"{COUPLE_SCENE} Topic: {topic}. {who} is speaking now."
        return VideoClip(
            turn=turn, speaker=speaker, kind="placeholder",
            caption=line, scene=scene,
            meta={"reason": "no video key — live captions over an animated scene"},
        )


class HappyHorseBackend(VideoBackend):
    """HappyHorse-1.0 text-to-video adapter (best-effort, downgrades cleanly).

    The exact request/response shape is pinned once a real key is available; the
    adapter is written against the OpenAI-compatible video surface and falls back
    to the placeholder on any error so a turn is never dropped.
    """

    name = "happyhorse"

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.model = model
        self._fallback = PlaceholderBackend()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def render_turn(self, *, turn: int, speaker: str, line: str, topic: str) -> VideoClip:
        who = "the woman" if speaker == "b" else "the man"
        prompt = (
            f"{COUPLE_SCENE} They are discussing: {topic}. In this shot, {who} "
            f"says: \"{line}\". Lip-synced speech, natural delivery, ambient park "
            f"audio. Keep both characters visually consistent with previous shots."
        )
        try:
            data = await self._post_video(prompt)
            url, audio = _extract_media(data)
            if not url:
                raise ValueError("no video url in response")
            return VideoClip(
                turn=turn, speaker=speaker, kind="video", caption=line,
                scene=prompt, url=url, audio_url=audio, meta={"model": self.model},
            )
        except Exception as e:  # noqa: BLE001 — never lose a turn to the video layer
            log.warning("HappyHorse render failed (turn %d), using placeholder: %s", turn, e)
            clip = await self._fallback.render_turn(turn=turn, speaker=speaker, line=line, topic=topic)
            return VideoClip(**{**clip.public(), "meta": {**clip.meta, "happyhorse_error": str(e)}})

    async def _post_video(self, prompt: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "input": {"prompt": prompt},
            "parameters": {"duration": 6, "resolution": "1080p", "audio": True},
        }
        resp = await self._client.post("/video/generations", json=payload)
        if resp.status_code >= 400:
            raise ValueError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _extract_media(data: dict[str, Any]) -> tuple[str, str]:
    """Tolerant extraction across plausible response shapes."""
    # OpenAI-style: {"data": [{"url": ...}]}; dashscope-style: {"output": {...}}
    for path in (
        lambda d: d["data"][0]["url"],
        lambda d: d["output"]["video_url"],
        lambda d: d["output"]["results"][0]["url"],
        lambda d: d["video_url"],
    ):
        try:
            url = path(data)
            if isinstance(url, str) and url:
                break
        except (KeyError, IndexError, TypeError):
            continue
    else:
        url = ""
    audio = ""
    for path in (lambda d: d["output"]["audio_url"], lambda d: d["audio_url"]):
        try:
            a = path(data)
            if isinstance(a, str) and a:
                audio = a
                break
        except (KeyError, IndexError, TypeError):
            continue
    return url, audio


def build_video_backend() -> VideoBackend:
    """Choose a backend from the environment. Safe with no key."""
    mode = os.environ.get("ARIA_VIDEO_MODE", "auto").strip().lower()
    key = os.environ.get("HAPPYHORSE_API_KEY", "").strip()
    base = os.environ.get("HAPPYHORSE_BASE_URL", "").strip()
    model = os.environ.get("HAPPYHORSE_MODEL", "happyhorse-1.0-t2v").strip()

    if mode == "placeholder" or (mode == "auto" and not key):
        log.info("Aria video backend: placeholder (mode=%s, key=%s)", mode, "set" if key else "none")
        return PlaceholderBackend()
    if not base:
        log.warning("HappyHorse requested but HAPPYHORSE_BASE_URL is empty; using placeholder")
        return PlaceholderBackend()
    log.info("Aria video backend: happyhorse model=%s", model)
    return HappyHorseBackend(base, key, model)
