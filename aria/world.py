# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""The interactive world — a guided, generated dream the user walks through.

A scripted arc of **beats** (greeting → through the door → the universe → choose a
planet colour → choose its inhabitants → the kingdom → the mansion & the mirror →
the dinner → invite the staff → farewell). At every beat the guide speaks a line
(authored live by the LLM, memory-aware) and the right pane renders that scene as
video. The user answers in the chat; their words set the world's facts and advance
the story — and they can ask anything along the way, the guide replies in
character. Each run differs: the LLM colours the scene and its emotion.

Turn-based (request/response): ``start`` opens the world; each ``say`` advances it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from engine.engram import new_ulid
from engine.qwen import QwenClient
from engine.tenants import KiokuEngine, Mind

from aria.director import GUIDE_NAME, director_system, facts_context, video_prompt
from aria.video import VideoBackend

log = logging.getLogger("aria.world")


@dataclass(frozen=True, slots=True)
class Beat:
    key: str
    scene: str          # what the video shows
    emotion: str        # drives the look + the guide's tone
    asks: str = ""      # the question the guide poses to the user at this beat
    fact: str = ""      # the world fact the user's answer to THIS beat sets
    special: str = ""   # client hint, e.g. "mirror" or "end"


# The reference arc. The guide authors the actual words; this is the skeleton.
# Identity (name, who you are, how you look) is gathered IN the conversation by
# the guide — never a form. Each of those is just an early beat.
BEATS: list[Beat] = [
    Beat("greeting", "A warm guide stands before a softly glowing door in a field of light, turning to welcome you",
         "warm", asks="Before we step through — what should I call you?", fact="name", special="door"),
    Beat("kindle", "The guide smiles warmly as the glowing door brightens, ready to open",
         "warm", asks="It's good to meet you. In this world, are you a he, a she, or something else?", fact="gender"),
    Beat("universe", "Stepping through the door into outer space — nebulae, drifting stars, deep silence",
         "wonder", asks="What colour do you want your planets to be?", fact="planet_color"),
    Beat("inhabitants", "A planet forms and glows in the chosen colour, oceans and continents swirling into being",
         "wonder", asks="What should live on it — humans, aliens, beast-men, or all of them?", fact="inhabitants"),
    Beat("kingdom", "Down on the planet a kingdom rises — towers, banners, the chosen creatures gathering",
         "grand", asks="Shall we walk to your house now?"),
    Beat("mansion", "Walking up to a beautiful futuristic mansion with old-world warmth; a butler and maids greet you; a tall mirror waits in the hall",
         "joy", asks="A mirror waits in the hall. Picture yourself — your eyes, your skin, your hair. How do you look?", fact="appearance", special="arrive"),
    Beat("mirror", "A tall ornate mirror in the grand hall reflects the guest's own face, exactly as they described",
         "tender", asks="Shall we head to the dining room?", special="mirror"),
    Beat("dinner", "A long table set with delicious food in a futuristic-yet-cozy dining room",
         "joy", asks="Would you like to invite your butler and maids to eat with you?", fact="invited"),
    Beat("farewell", "Everyone sits together at the warm table; the guide turns to you with a smile",
         "tender", asks="", special="end"),
]


@dataclass
class WorldRun:
    run_id: str
    session_id: str
    tenant: str
    beat_index: int = 0
    turn_no: int = 0
    facts: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)
    clips: dict[int, dict] = field(default_factory=dict)  # turn_no -> video clip
    created_ts: float = field(default_factory=time.time)

    @property
    def name(self) -> str:
        return self.facts.get("name", "")

    @property
    def face(self) -> dict:
        return {"gender": self.facts.get("gender", ""), "appearance": self.facts.get("appearance", "")}

    @property
    def beat(self) -> Beat:
        return BEATS[min(self.beat_index, len(BEATS) - 1)]

    @property
    def done(self) -> bool:
        return self.beat_index >= len(BEATS) - 1

    def public(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "name": self.name, "beat_index": self.beat_index,
                "beat": self.beat.key, "facts": self.facts, "done": self.done}


class WorldManager:
    """Owns worlds and drives the beat loop over the copied Kioku engine."""

    def __init__(self, engine: KiokuEngine, video: VideoBackend) -> None:
        self.engine = engine
        self.video = video
        self._runs: dict[str, WorldRun] = {}
        self._tasks: set[asyncio.Task] = set()

    def get(self, run_id: str) -> WorldRun | None:
        return self._runs.get(run_id)

    def get_clip(self, run: WorldRun, turn_no: int) -> dict:
        """The video for one exchange — {"status":"pending"} until it's rendered."""
        return run.clips.get(turn_no) or {"status": "pending"}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self, qwen: QwenClient | None) -> dict:
        rid = "w_" + new_ulid()[:12]
        run = WorldRun(run_id=rid, session_id="s_" + new_ulid()[:12], tenant="aria:" + rid)
        self._runs[run.run_id] = run
        # The guide greets at the door and asks your name — no form, just the scene.
        beat = await self._speak(run, qwen, user_text="")
        return {"run_id": run.run_id, **beat, "guide": GUIDE_NAME}

    async def say(self, run: WorldRun, text: str, qwen: QwenClient | None) -> dict:
        # Free conversation: a real ANSWER advances the story; a side QUESTION
        # ("where is my house?") is answered in-scene without moving on. Either way
        # the guide turns to you and replies with a fresh look-at-you clip.
        cur = run.beat
        advance = bool(text.strip()) and not _is_question(text)
        if advance:
            if cur.fact:
                run.facts[cur.fact] = text.strip()
            if not run.done:
                run.beat_index += 1
        beat = await self._speak(run, qwen, user_text=text)
        beat["advanced"] = advance
        return beat

    async def _speak(self, run: WorldRun, qwen: QwenClient | None, *, user_text: str) -> dict:
        beat = run.beat
        run.turn_no += 1
        tno = run.turn_no  # every exchange gets its own clip, even side-questions
        mind: Mind = self.engine.registry.named_mind(run.tenant)
        message = user_text.strip() or "(I arrive, ready to begin.)"
        # The guide's line — fast, returned immediately so the text appears at once.
        result = await self.engine.turn(
            mind, message,
            session_id=run.session_id, send_to_both=False, qwen=qwen,
            system_override=director_system(beat, run.facts, run.name),
            extra_context=facts_context(run.facts, run.name),
            history=run.history[-12:],
        )
        line = result.kioku_reply.strip()
        run.history.append({"role": "user", "content": message})
        run.history.append({"role": "assistant", "content": line})

        # The talking video renders in the BACKGROUND (it's slow) and is polled
        # by the client — so the text shows now and the video arrives when ready.
        run.clips[tno] = {"status": "pending"}
        self._spawn(self._render(run, tno, beat, line))
        return {
            "beat": beat.key, "beat_index": run.beat_index, "turn_no": tno,
            "emotion": beat.emotion, "asks": beat.asks, "special": beat.special,
            "reply": line, "video": {"status": "pending"}, "facts": run.facts,
            "done": run.done, "recalled": len(result.pack.hit_list()), "face": run.face,
        }

    async def _render(self, run: WorldRun, tno: int, beat: Beat, line: str) -> None:
        try:
            clip = await self.video.render_scene(
                turn=tno, prompt=video_prompt(beat, run.facts, run.name, run.face),
                caption=line, emotion=beat.emotion,
            )
            run.clips[tno] = clip.public()
        except Exception as e:  # noqa: BLE001 — a failed render must not break the world
            log.warning("scene render failed for turn %d: %s", tno, e)
            run.clips[tno] = {"status": "error", "reason": str(e)}


_QWORDS = ("where", "what", "who", "why", "how", "when", "which", "can", "could",
           "is", "are", "do", "does", "will", "would", "should", "may", "tell")


def _is_question(text: str) -> bool:
    """A light heuristic: questions are side-chat (no story advance); plain answers
    ('blue', 'both', 'yes') advance the story."""
    t = text.strip().lower()
    if not t:
        return False
    if t.endswith("?"):
        return True
    first = t.split()[0]
    return first in _QWORDS


def _slug(text: str) -> str:
    import re
    return (re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "guest")
