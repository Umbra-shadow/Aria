# Aria

**Two remembering minds in live dialogue, made cinematic.** — Qwen hackathon, Track 2.

Type a topic. Two AI agents — visualised as a couple talking on a park bench —
hold a live, turn-limited conversation about it. **Both have memory** (the Kioku
engine), so they remember earlier points and across sessions. The screen splits:

- **Left** — the machinery in the open: the topic + Qwen key field, then each
  agent's reply streaming token-by-token before it's "spoken", plus a memory strip.
- **Right** — **HappyHorse-1.0** renders the couple having that exact exchange,
  with synced audio and character consistency. No video key? A graceful scene-card
  placeholder keeps the split-screen whole.

Nothing is pre-rendered — text and video are produced in the same moment; the
LLM's thinking latency reads as the couple's natural pause.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r engine/requirements.txt
make demo            # http://localhost:8001
```

Qwen is the main brain. Leave `QWEN_API_KEY` blank in `.env` and enter your key in
the web UI (sent per-request as `X-Qwen-Key`, lives only in that tab, never
persisted). For video, set `HAPPYHORSE_API_KEY`; otherwise Aria runs in
placeholder mode.

## Standalone

Aria is self-contained. The memory substrate under `engine/` is **copied** from
the Kioku project, not imported — Aria has no dependency on its sibling projects.

## Layout

```
engine/    copied Kioku memory substrate (no app code)
aria/      app layer — dialogue (two minds), video (HappyHorse + placeholder), app
web/       split-screen UI
```
