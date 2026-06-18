# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""The conversation: two remembering minds, talking, turn by turn.

Two named Kioku minds (``aria:speaker-a`` / ``aria:speaker-b``) hold a
turn-limited dialogue about a topic. Each mind runs a full Kioku ``turn`` — it
recalls its own memory, replies to the other speaker's last line, and the
exchange is committed as an engram, so each speaker genuinely remembers within
the chat and across sessions.

Every turn is announced over an event bus (SSE): the reply text on the left, and
a video job for the right pane. Generation is live — the LLM's natural latency is
the couple's pause before they speak.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from engine.engram import new_ulid
from engine.qwen import LLMError, QwenClient
from engine.tenants import KiokuEngine

from aria.video import VideoBackend

log = logging.getLogger("aria.dialogue")

SPEAKER_A = "aria:speaker-a"
SPEAKER_B = "aria:speaker-b"

# Warm, distinct personas. Framed as a couple chatting on a bench so the video
# reads naturally. Each gets the recalled-memory pack appended by the engine.
PERSONA_A = """\
You are Theo, one half of a couple sitting on a park bench with your partner Mara,
talking about: {topic}. You are curious, grounded, a little playful. Speak in 1–3
natural spoken sentences — this is talking, not an essay. Build on what Mara just
said, bring your own view, and remember earlier points in the chat. Do not narrate
stage directions; just speak your line.

What you remember:
{pack}"""

PERSONA_B = """\
You are Mara, one half of a couple sitting on a park bench with your partner Theo,
talking about: {topic}. You are warm, sharp, ask good questions. Speak in 1–3
natural spoken sentences — this is talking, not an essay. Respond to what Theo just
said, add your own angle, and remember earlier points in the chat. Do not narrate
stage directions; just speak your line.

What you remember:
{pack}"""


@dataclass
class Conversation:
    """One live two-agent dialogue with its own event bus."""

    conv_id: str
    topic: str
    max_turns: int
    session_id: str
    status: str = "idle"            # idle | talking | done | error
    turns: list[dict] = field(default_factory=list)
    error: str = ""
    created_ts: float = field(default_factory=time.time)
    _subs: set[asyncio.Queue] = field(default_factory=set)
    _recent: list[dict] = field(default_factory=list)

    # -- event bus --------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def emit(self, event: dict) -> None:
        self._recent.append(event)
        del self._recent[:-64]
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    @property
    def recent_events(self) -> list[dict]:
        return list(self._recent)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "error")

    def public(self) -> dict[str, Any]:
        return {
            "conv_id": self.conv_id, "topic": self.topic, "max_turns": self.max_turns,
            "status": self.status, "turns": self.turns, "error": self.error,
            "created_ts": self.created_ts,
        }


class DialogueManager:
    """Owns conversations and drives the turn loop over the copied Kioku engine."""

    def __init__(self, engine: KiokuEngine, video: VideoBackend) -> None:
        self.engine = engine
        self.video = video
        self._convs: dict[str, Conversation] = {}
        self._tasks: set[asyncio.Task] = set()

    def get(self, conv_id: str) -> Conversation | None:
        return self._convs.get(conv_id)

    def start(self, topic: str, max_turns: int, qwen: QwenClient | None) -> Conversation:
        conv = Conversation(
            conv_id="c_" + new_ulid()[:12],
            topic=topic.strip(),
            max_turns=max(2, min(12, max_turns)),
            session_id="s_" + new_ulid()[:12],
        )
        self._convs[conv.conv_id] = conv
        task = asyncio.create_task(self._run(conv, qwen))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return conv

    async def _run(self, conv: Conversation, qwen: QwenClient | None) -> None:
        conv.status = "talking"
        await conv.emit({"stage": "start", "topic": conv.topic, "max_turns": conv.max_turns})
        registry = self.engine.registry
        mind_a = registry.named_mind(SPEAKER_A)
        mind_b = registry.named_mind(SPEAKER_B)

        last_line = (
            f"Let's talk about {conv.topic}. I'll start — what's your first reaction?"
        )
        try:
            for turn in range(1, conv.max_turns * 2 + 1):
                is_a = turn % 2 == 1
                mind = mind_a if is_a else mind_b
                speaker = "a" if is_a else "b"
                persona = (PERSONA_A if is_a else PERSONA_B)
                system = persona.format(topic=conv.topic, pack="{pack}")  # engine fills {pack}

                await conv.emit({"stage": "thinking", "turn": turn, "speaker": speaker})
                result = await self.engine.turn(
                    mind, last_line,
                    session_id=conv.session_id,
                    send_to_both=False,
                    qwen=qwen,
                    system_override=system,
                )
                line = result.kioku_reply.strip()
                conv.turns.append({"turn": turn, "speaker": speaker, "line": line})
                await conv.emit({
                    "stage": "spoke", "turn": turn, "speaker": speaker, "line": line,
                    "recalled": len(result.pack.hit_list()),
                })

                # Right pane: render (or placeholder) this line as the couple talking.
                clip = await self.video.render_turn(
                    turn=turn, speaker=speaker, line=line, topic=conv.topic
                )
                await conv.emit({"stage": "video", **clip.public()})

                last_line = line
                await asyncio.sleep(0)  # cooperative; lets subscribers drain

            conv.status = "done"
            await conv.emit({"stage": "done", "turns": len(conv.turns)})
        except LLMError as e:
            conv.status = "error"; conv.error = str(e)
            await conv.emit({"stage": "error", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            conv.status = "error"; conv.error = str(e)
            log.exception("dialogue %s failed", conv.conv_id)
            await conv.emit({"stage": "error", "error": str(e)})
