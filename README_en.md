# Apex Voice

[日本語](README.md) | [English](README_en.md)

A macOS menu-bar app that combines **voice typing + AI agent**. Speak into your mic, get transcribed by local Whisper, polished / formalized / translated, and **inserted at the cursor position in whatever app is in front**. Agent mode also runs actions from your voice: **add a reminder, create a calendar event, web search, web page summarization**.

- **Speech recognition**: local Whisper (`mlx-whisper`, `whisper-large-v3-turbo`)
- **AI post-processing**: Amazon Bedrock Claude Haiku 4.5 for polish / formal / translation / bullets
- **AI agent**: Strands Agents for multi-step execution
  - macOS integration: Reminders / Calendar / open URL
  - Web fetch + summarize (multiple tools chained from one utterance)
- **Vocabulary learning**: Amazon Bedrock AgentCore Memory accumulates proper nouns and domain terms, injected back into Whisper as prompt hints
- **Global hotkey**: ⌃⌥V toggles recording from anywhere
- Multi-language (11 languages selectable from the menu)

## Requirements
- **Apple Silicon Mac** (M1+). Intel Macs are not supported (mlx is Apple Silicon only)
- macOS (microphone required)
- Internet on first run only (downloads Whisper model ~1.5 GB; offline after that)
- AWS credentials for post-processing / agent / Memory features (`aws login`)

## Quick Start

### 1. Run from source
```bash
git clone https://github.com/yama3133/apex-voice.git
cd apex-voice
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python voicetype.py
```

### 2. Install as a LaunchAgent (recommended)
Runs on login, auto-restarts on crash. More stable than a `.app` bundle.

```bash
# The plist uses absolute paths — edit to match your clone location
cp com.yamashita.apexvoice.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
```

Stop / restart:
```bash
launchctl unload ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
launchctl load ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
```

Log: `/tmp/apexvoice.log`

## Permissions (one-time)
1. **Microphone** — granted on first recording
2. **Accessibility** — required to paste into other apps (sends Cmd+V)
3. **Input Monitoring** — required for the global hotkey (⌃⌥V)
4. **AWS credentials** — for post-processing / agent / Memory features (`aws login`)

## Menu

**Left-click** the 🎤 menu-bar icon to open the menu.

| Item | Behavior |
|---|---|
| 🎤 Start / ■ Stop recording | Toggle recording (or use hotkey ⌃⌥V) |
| Status | Recording / Idle |
| Language | 11 languages (Auto / 日本語 / English / 中文 / 한국어 / Español / Français / Deutsch / Italiano / Português / Русский) |
| Post-process | Raw / Polish / Formal / Translate (EN) / Bullets / **Agent** |
| Hotkey | View / change current combo |
| Sensitivity up/down | Adjust VAD threshold |
| Quit | Exit the app |

Settings persist in `~/.apexvoice/config.json` and restore on next launch.

## Post-Process Modes

| Mode | Behavior | External actions |
|---|---|---|
| Raw | Insert Whisper output as-is | None |
| Polish | Remove fillers, fix homophones, natural written style | None |
| Formal | Rewrite as business-formal Japanese | None |
| Translate | Translate to natural English | None |
| Bullets | Convert key points to bullets | None |
| **Agent** | Classify the utterance and run the right tool or insert text | **Yes** |

Only Agent mode triggers external actions. For day-to-day use, Raw or Polish is safe — no unintended events or browser windows.

### Agent actions (Strands Agents)
Multiple actions chain from a single utterance. e.g. "Remind me to send the email in 1 minute, and add a meeting at 10am tomorrow" → two actions in one go.

- **Add reminder** — "Remind me to send the email in 30 minutes"
- **Create calendar event** — "Add a meeting with Tanaka-san tomorrow at 3pm to my calendar"
- **Open URL / search** — "Search the Python documentation on the web"
- **Web fetch + summarize** — "Tell me the latest version from the Python official site" / "Look up Mt. Fuji on Wikipedia"
  - Fetches the page → summarizes with Claude Haiku 4.5 → inserts at cursor

## Vocabulary Memory (AgentCore Memory)

Extracts proper nouns and domain terms from the polished transcript and stores them locally (`~/.apexvoice/vocabulary.json`) and in the cloud (AgentCore Memory). On next recognition, those terms get injected into Whisper's `initial_prompt`, reducing homophone errors.

A personal dictionary that grows the more you use it.

## Configuration (env vars)
| Variable | Default | Description |
|---|---|---|
| `VOICETYPE_MODEL` | `mlx-community/whisper-large-v3-turbo` | Recognition model |
| `VOICETYPE_LANG` | `ja` | Recognition language (empty = auto) |
| `VOICETYPE_PROMPT` | (empty) | Extra term hints |
| `VOICETYPE_SENSITIVITY` | `2.5` | VAD threshold (smaller = more sensitive) |
| `VOICETYPE_MIC` | (empty = OS default) | Mic device (index or name substring) |
| `VOICETYPE_DEBUG` | (empty) | `1` to log RMS volume |
| `APEXVOICE_BEDROCK_MODEL` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Model for post-process / agent |
| `APEXVOICE_BEDROCK_REGION` | `us-east-1` | Bedrock region |
| `APEXVOICE_MEMORY_ID` | (built-in default) | AgentCore Memory store ID |
| `APEXVOICE_ACTOR_ID` | `default-user` | User identifier in Memory |

## `.app` build (reference, not recommended)
```bash
.venv/bin/python setup.py py2app
# → dist/Apex Voice.app
```
py2app tends to break `python312.zip`, causing `mlx-whisper` load failures. Use the **LaunchAgent** method above for daily use.

## How it works
```
Mic → simple VAD → utterance buffer
  → mlx-whisper (initial_prompt: learned vocab)
  → Post-process (Bedrock Claude Haiku 4.5)
    ├─ Raw / Polish / Formal / Translate / Bullets → insert text
    └─ Agent (Strands) → tool selection
        ├─ Reminders / Calendar / URL / Web summary, chained
        └─ Insert text (when no tool fires)
  → Write to AgentCore Memory (vocab accumulation)
```

## Version history
- **v0.2.1** — Switched to LaunchAgent (stable) / menu-bar icon to SF Symbols PNG / process name fixed via `setproctitle` / Apex Voice Web integration
- **v0.2.0** — Renamed to Apex Voice. Added Strands Agents multi-step, web fetch + summary, AgentCore Memory, global hotkey
- **v0.1.0** — Initial release (as WhisType)

## License
MIT
