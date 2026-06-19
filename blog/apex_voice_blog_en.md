---
title: "Voice Typing Anywhere on macOS — I Built Apex Voice with mlx-whisper, Amazon Bedrock, and Strands Agents"
published: false
tags: aws, bedrock, python, macos
---

> A weekend project that turned into a daily-driver: a macOS menu-bar
> app that lets you talk into **any** input field — Slack, browser,
> Notes, anywhere — powered by local Whisper and Amazon Bedrock.

## TL;DR

I built **Apex Voice**, an open-source macOS voice typing tool.
It listens through your microphone, transcribes speech offline with
[mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
and inserts the result wherever your cursor is. With Amazon Bedrock
layered on top, it can polish the text, translate it, or execute
agent actions like "add a reminder" or "summarize this page."

- Repo: [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
- Companion web app: [apex-voice-web.vercel.app](https://apex-voice-web.vercel.app)

## Why I Built It

macOS already has built-in dictation, and there are great commercial
tools like Aqua Voice. So why bother?

1. **Built-in dictation** doesn't reliably work in every app, and
   Japanese accuracy is uneven.
2. **Aqua Voice** is polished but closed and paid.
3. I wanted to **own the stack**: pick my model, my post-processing,
   my agent tools. And to learn by building.

## What It Does

The core loop:

```
Mic
  → VAD
  → mlx-whisper
  → (optional Bedrock post-process)
  → Clipboard
  → ⌘V into any app
```

On top of that:

- **Post-process modes** powered by Claude Haiku 4.5 on Bedrock:
  `polish`, `formal`, `translate`, `bullets`.
- **Agent mode** via Strands Agents: one utterance can trigger
  multiple tool calls — add a reminder, open a calendar event,
  fetch a webpage and summarize it.
- **Vocabulary learning** with Amazon Bedrock AgentCore Memory:
  proper nouns and domain terms accumulate over time and get
  injected as Whisper's `initial_prompt`, so accuracy improves
  with use.

## Architecture

The whole thing runs as a Python process managed by `launchd`.
Local-only by default; Bedrock features kick in when you enable them.

The main pipeline (mic → whisper → insertion) is fully offline.
Bedrock and AgentCore Memory are auxiliary — they make the experience
richer but the app works without them.

## The AWS Side

Three Bedrock-shaped pieces:

| Piece | Role |
|---|---|
| **Amazon Bedrock (Claude Haiku 4.5)** | Post-processing, agent classification, summarization |
| **Strands Agents** | Tool schemas + multi-step orchestration |
| **AgentCore Memory** | Persists vocabulary across sessions |

I picked **Haiku 4.5** because the post-processing happens *every
utterance*. Sub-second latency matters more than top-tier reasoning
here. For agent mode, Haiku is still strong enough to pick the right
tool from ~10 options reliably.

The companion web app (`apex-voice-web`) runs on **Vercel** with a
Python serverless function that calls Bedrock for URL classification
and summarization. It uses **Upstash Redis** for a live history
feed. The macOS app fires history entries to it via a non-blocking
POST.

## The Hard Part Wasn't AI. It Was Packaging.

I lost almost a full day to `py2app`.

The plan was obvious:

```
setup.py py2app → dist/Apex Voice.app
→ drop in /Applications → done
```

Reality:

```
[12:12:22] 認識エラー: bad local file header:
  '.../Apex Voice.app/Contents/Resources/lib/python312.zip'
```

`py2app` bundles your dependencies into a `python312.zip`, and
`mlx-whisper`'s native extensions don't survive being zipped.
I tried `zip_include_packages: []` — not a valid py2app option.
I tried a shell-script launcher inside the `.app` — macOS warned
about needing Rosetta. I tried a hand-compiled arm64 launcher
binary — that worked, but every iteration meant re-granting
Accessibility permission because the bundle signature changed.

**The fix: stop building a `.app` entirely.**

I switched to a **LaunchAgent** plist that points directly at the
venv's Python and the script. Drop it in `~/Library/LaunchAgents/`,
`launchctl load`, done. With `KeepAlive: true`, it auto-restarts on
crash. The "restart" menu item just calls `rumps.quit_application()`
and lets launchd bring it back.

Two small touches kept it feeling like a proper app:

```python
import setproctitle
setproctitle.setproctitle("Apex Voice")
```

So Activity Monitor shows "Apex Voice" instead of "python3.12".
And for the menu-bar icon, I rendered SF Symbols
(`waveform.and.mic` / `mic.fill`) to PNG once and loaded them as
a template image — they auto-adapt to light/dark mode.

## What I Learned

- **Don't fight the OS packaging story when you have a better path.**
  A LaunchAgent + venv beat py2app on every axis: simpler, more
  stable, easier to update.
- **Pick the model for the latency profile, not the leaderboard.**
  Haiku 4.5 wins here because users feel every 500 ms in a voice
  typing loop.
- **Bluetooth mic quality is an OS problem, not an app problem.**
  When the mic engages HFP mode, sample rate drops to 8 kHz. No
  app-level fix exists on macOS.
- **Accessibility-driven keystroke injection (`osascript`-Cmd-V)
  is the right primitive.** It works in every app I've tried —
  Slack, browsers, native editors. No per-app integration needed.

## What's Next

- **Vocabulary learning UI** — show the user what AgentCore Memory
  has learned.
- **Windows port** — swap `mlx-whisper` for `faster-whisper`,
  `rumps` for `pystray`. The core loop is OS-agnostic.
- **Approval-gated agent actions** — wire it into Aegis, a
  Slack-based approval plane I'm building for AI agents.

## Try It

```bash
git clone https://github.com/yama3133/apex-voice.git
cd apex-voice
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run directly
.venv/bin/python voicetype.py

# Or install as a LaunchAgent (auto-start, auto-restart)
# Edit the plist to point at your clone path first
cp com.yamashita.apexvoice.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
```

You'll need an Apple Silicon Mac (mlx is Apple Silicon only) and
AWS credentials if you want post-processing and agent features.

---

If you build something similar, or hit the same py2app wall I did,
I'd love to hear about it. Code, issues, and PRs welcome on
[GitHub](https://github.com/yama3133/apex-voice).
