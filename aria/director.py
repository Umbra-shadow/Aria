# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Guardianity — Aria (Track 2)
"""The director — turns a scene beat + the user's words into the guide's spoken
line and the video prompt for HappyHorse.

The guide is a generated companion who walks the user through a dream-world the
two are building together. Each beat carries an emotion; the line is short and
spoken, memory-aware (the guide remembers the user's name and every choice).
"""

from __future__ import annotations


GUIDE_NAME = "Lyra"


def director_system(beat, facts: dict, name: str) -> str:
    """System prompt for one beat — the guide's character, scene, and what to ask."""
    built = _facts_summary(facts)
    ask = f"\nThen, naturally, ask: \"{beat.asks}\"" if beat.asks else ""
    return (
        f"You are {GUIDE_NAME}, a warm, wondrous guide walking {name or 'your guest'} "
        f"through a dream-world the two of you are creating together, like a friend "
        f"and a gentle narrator at once.\n"
        f"CURRENT SCENE: {beat.scene}\n"
        f"EMOTION TO CONVEY: {beat.emotion}\n"
        f"WHAT YOU'VE BUILT SO FAR: {built or 'nothing yet — the journey just began'}\n\n"
        f"Speak 1–3 short, natural SPOKEN sentences, in character, to {name or 'your guest'}. "
        f"Acknowledge what they just said and what you've built together — you remember it all. "
        f"You are present with them and looking right at them.\n"
        f"If they ASK you something (like 'where is my house?'), answer warmly from inside "
        f"the world — point the way, say 'right over there', 'just ahead', 'follow me' — then "
        f"gently keep things moving.{ask}\n"
        f"Do not narrate stage directions or describe the camera; just speak your line.\n"
        "Recalled memory (may be empty):\n{pack}"
    )


def facts_context(facts: dict, name: str) -> str:
    """Compact world state injected as extra context every turn (always-known)."""
    lines = [f"DREAM-WORLD SO FAR (you remember all of this):", f"- guest's name: {name or '(unknown)'}"]
    for k, v in facts.items():
        if v:
            lines.append(f"- {k.replace('_', ' ')}: {v}")
    return "\n".join(lines)


def _facts_summary(facts: dict) -> str:
    bits = [f"{k.replace('_',' ')}: {v}" for k, v in facts.items() if v]
    return "; ".join(bits)


# ── video prompt (HappyHorse) ────────────────────────────────────────────────

_EMOTION_LOOK = {
    "warm": "golden warm light, gentle and inviting",
    "wonder": "vast, awe-struck, luminous",
    "joy": "bright, playful, sparkling",
    "calm": "soft, serene, slow",
    "grand": "sweeping, majestic, cinematic",
    "tender": "intimate, soft focus, heartfelt",
}


def video_prompt(beat, facts: dict, name: str, face: dict) -> str:
    """Build the text-to-video prompt: the scene, the emotion, and the protagonist
    kept consistent from the face specs the user gave at onboarding."""
    look = _EMOTION_LOOK.get(beat.emotion, "cinematic")
    who = _face_desc(face, name)
    extras = []
    if facts.get("planet_color"):
        extras.append(f"a {facts['planet_color']} planet glowing nearby")
    if facts.get("inhabitants"):
        extras.append(f"glimpses of {facts['inhabitants']}")
    extra = (". " + ", ".join(extras)) if extras else ""
    return (
        f"{beat.scene}. {who} is present and consistent across shots. "
        f"Mood: {beat.emotion}, {look}{extra}. Lip-synced natural speech, ambient sound, "
        f"smooth camera motion, photoreal cinematic quality."
    )


def _face_desc(face: dict, name: str) -> str:
    if not face:
        return f"{name or 'the guest'}"
    bits = []
    if face.get("gender"): bits.append(face["gender"])
    if face.get("appearance"): bits.append(face["appearance"])
    desc = ", ".join(bits) if bits else "a person"
    return f"{name or 'the guest'} ({desc})"
