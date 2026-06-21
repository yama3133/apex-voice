---
title: "Apex Voice v2 — Killing Hallucinations, Cutting Latency, and Landing on Caps Lock"
published: false
tags: aws, bedrock, python, macos
---

> Follow-up to my first post on **Apex Voice** — the macOS menu-bar
> voice-typing app I built with mlx-whisper and Amazon Bedrock.
> This is the "make it actually pleasant to use every day" pass.

## Where I Left Off

A few weeks ago I shipped v0.2 — local Whisper, Bedrock post-processing,
Strands Agents for multi-step tool calls, AgentCore Memory for
vocabulary learning. It worked. But day-to-day it had three rough edges:

1. **YouTube-style hallucinations** — "Thanks for watching, see you in
   the next video!" appearing mid-sentence when I hadn't said anything.
2. **Latency** — noticeable lag between speaking and seeing text.
3. **Hotkey ergonomics** — `⌃⌥V` worked, but it's a three-finger
   contortion I never got comfortable with.

v2 is about fixing all three plus adding two real Bedrock AgentCore
features I'd been deferring.

- Repo: [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
- Companion web: [apex-voice-web.vercel.app](https://apex-voice-web.vercel.app)

## 1. Killing the Hallucinations (the layered way)

Whisper hallucinations aren't a bug — they're a side effect of how
the model was trained. The Japanese training set is dominated by
YouTube captions, so when Whisper sees silence or noise, the highest-
probability output is a polite YouTube sign-off. There's no single fix.
You have to layer defenses.

**Layer 1: push-to-talk, not VAD-driven segmentation.**
The original recorder used RMS-based VAD to detect the start and end
of each utterance. That meant any time my mic picked up keyboard
noise or breath, a "phantom utterance" got passed to Whisper —
and Whisper helpfully invented words. I tore it out:

```python
# Listening mode = take everything, no judgment
if not self._speaking:
    self._speaking = True
    self._buf = list(self._pre)
self._buf.append(block)
```

The user explicitly toggles recording on/off (now via Caps Lock,
see §3), so VAD inside the recording window is redundant. The
gate is now at the boundary, not inside.

**Layer 2: Silero VAD as the final gate before Whisper.**
Once a recording ends, I run it through [Silero VAD]
(https://github.com/snakers4/silero-vad). If Silero says
"no speech here," the audio never touches Whisper.

```python
if not _silero_has_speech(audio):
    log("Silero VAD: no speech → drop")
    return ""
```

I also preload Silero at app startup on a background thread, so
the first utterance doesn't pay the model-load cost.

**Layer 3: tighter mlx-whisper decoding.**
Three knobs:

```python
kwargs = dict(
    no_speech_threshold=0.7,         # was 0.5 — bias toward silence
    logprob_threshold=-0.5,          # drop low-confidence outputs
    compression_ratio_threshold=2.2, # drop repetition spirals
    temperature=0.0,                 # no fallback ladder
)
```

Temperature fallback was a hidden latency cost too (more on that
in §2).

**Layer 4: STRONG vs WEAK phrase filters.**
My old filter rejected outputs that were *mostly* a known YouTube
phrase. But "次の動画でお会いしましょう" ("see you in the next video")
has the trigger phrase "次の動画" at the front, followed by 9
characters of plausible-looking text. The "mostly" check let it
through.

The fix was simple. Split the phrase list into two:

- **STRONG**: if this phrase appears *anywhere*, drop. ("see you in
  the next video", "channel registration", "thank you for watching")
- **WEAK**: only drop if the phrase is the dominant content.
  ("good night" — could be a real utterance)

```python
for p in STRONG_HALLUCINATION_PHRASES:
    if p in text:
        return True  # immediate kill
```

## 2. Cutting the Latency

I added timing logs to every stage to find the real bottleneck:

```
audio=4.8s vad=56ms whisper=1499ms → 19 chars
```

Whisper is the bottleneck. Always. So I attacked it three ways.

**Async clipboard restore.** The clipboard dance was blocking:

```python
self._pb_set(text)
ok = self._paste()  # this is what the user feels
# THEN sleep 150ms, THEN restore...
```

Moved restore to a background thread. The user-perceived insertion
time dropped from ~450ms (first time) to ~150ms steady-state.

**Quantized Whisper model.** Switched the default from
`mlx-community/whisper-large-v3-turbo` to its 4-bit quantized
sibling `whisper-large-v3-turbo-q4`. Honest result:

| | turbo (default) | turbo-q4 |
|---|---|---|
| Model size | ~800MB | ~350MB |
| Whisper time (4s audio) | ~1500ms | ~1500ms |
| Accuracy (subjective) | high | same or slightly better |

MLX is already heavily optimized, so the speed delta is ~0%.
But the memory and disk halve, accuracy didn't degrade in my
testing, and there's no reason not to take the win.

**The failed experiment.** I also tried
`mlx-community/distil-whisper-large-v3` — theoretically 2× faster.
Japanese accuracy collapsed. Distil-Whisper is English-tuned and
it shows. Reverted in 30 seconds. Worth saying out loud:
**always measure end-to-end, not just throughput.**

End state: ~1.5s for a 4-second utterance, on M-series Mac.
That's the floor with this model.

## 3. Caps Lock as the One-Key Hotkey

The original hotkey was `⌃⌥V` via `pynput.GlobalHotKeys`. Functional,
but I never warmed to the three-finger combo. I wanted **one key**
that I could mash without thinking — but every reasonable single-key
candidate has a problem:

- Letter keys (single `v`, `a`, etc.) — kills normal typing
- Right Cmd / Right Option — pynput can't distinguish left/right
- `fn` (globe key) — macOS hides this from userspace
- Caps Lock — taken by macOS for its toggle behavior
- F13–F19 — perfect, but most keyboards don't have them physically

The fix: rebind Caps Lock to F19 at the OS layer with
**Karabiner-Elements**, then make Apex Voice listen for F19. Karabiner
is a battle-tested OSS keyboard remapper for macOS. The config is
a one-rule JSON file:

```json
{
    "description": "Caps Lock -> F19 (for Apex Voice)",
    "manipulators": [{
        "type": "basic",
        "from": {
            "key_code": "caps_lock",
            "modifiers": { "optional": ["any"] }
        },
        "to": [{ "key_code": "f19" }]
    }]
}
```

Apex Voice ships this rule and a `karabiner://import?url=…` link
that adds it with one click. The user-facing result: **tap Caps
Lock once to start recording, tap again to stop**. Best UX upgrade
in the whole project.

There's a small subtlety here for cross-platform thinking. The
same `hotkey` config value (`<f19>`) works on Windows too, because
`pynput.GlobalHotKeys` parses identically. On Windows the user
would remap Caps Lock → F19 with **PowerToys Keyboard Manager**.
Different tool, same shape — no code branches.

## 4. AgentCore Browser for Real Web Search

The agent's "web search and summarize" tool was originally
`requests` + BeautifulSoup against Google's HTML results. It works
right up until Google's bot detection notices and blocks the
serverless IP. Predictable.

The fix: a two-stage fetch.

```
1) requests + BeautifulSoup (fast, free)
   ↓ fail / too short / Google blocked
2) AgentCore Browser (managed Chromium + Playwright over CDP)
```

AgentCore Browser is a managed headless Chromium environment on AWS,
addressed via boto3 + an authenticated WebSocket. You connect
Playwright with `connect_over_cdp` and drive it like any browser
session. Browsers with bot detection, JS-required pages, and Google's
search results all work, because it really *is* a real browser.

```python
client = BrowserClient(region=BEDROCK_REGION)
client.start()
ws_url, headers = client.generate_ws_headers()
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(ws_url, headers=headers)
    page = browser.contexts[0].new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    text = page.evaluate("() => document.body.innerText")
```

Sessions are started per-request and torn down — cost over comfort,
since web summarization is a low-frequency action.

## 5. AgentCore Payments (x402) — Letting the Agent Pay

This is the headline new capability. **The agent can pay for things.**

[AgentCore Payments](https://aws.amazon.com/bedrock/agentcore/) is
the Bedrock service for AI-agent-initiated payments via embedded
crypto wallets (Coinbase CDP or Stripe Privy). It speaks
**[x402](https://www.x402.org/)**, the "HTTP 402 Payment Required"
protocol revived for the agent era — a paid API responds with `402`
and a payment manifest, the agent generates a payment header and
retries.

The Apex Voice tool wires it into the existing approval flow:

```python
@tool
def pay_for_paid_resource(
    url: str, max_amount_usd: float, description: str = ""
) -> str:
    """Pay for a paid HTTP resource via x402.

    1) GET the URL → expect HTTP 402
    2) Create a PaymentSession with a spend cap
    3) generate_payment_header for the 402 body
    4) Re-GET with the payment header
    """
```

The flow:

```
voice: "pay this API up to 5 cents"
  → Whisper transcript
  → Strands Agent picks pay_for_paid_resource
  → Guardrail check (per-request / per-day caps)
  → Human approval dialog (or Slack approval via Aegis)
  → PaymentManager.create_payment_session
  → generate_payment_header
  → HTTP retry with header
  → result inserted at cursor
```

Setting it up isn't trivial — you need a PaymentManager + Connector
configured with Coinbase CDP or Stripe Privy credentials in the AWS
console, and an embedded wallet funded for the user. Not a quick demo,
but the **code path is real and the approval flow is structurally what
"giving an AI agent a wallet" should look like.**

## The New Architecture

Everything composes into something that genuinely runs all day.

(see architecture diagram below)

The pipeline is local-first: mic → recording → Silero VAD → mlx-whisper
→ insertion. Bedrock features layer on top per-utterance only when the
mode demands them.

## What I Learned (v2 Edition)

- **For hallucinations, layered defense beats any single fix.** Pre-
  filter (Silero VAD), in-flight params (logprob, no_speech), post-
  filter (phrase lists). No single layer is enough.
- **Measure before optimizing, even when "obvious."** I was sure
  the quantized model would be faster. It wasn't. MLX already had
  the win baked in.
- **The right primitive beats a clever hack.** Three-key combo vs.
  Caps Lock + Karabiner — same goal, but one is invisible to the
  user.
- **AgentCore is more than Memory.** Browser solves a real bot-
  detection problem; Payments turns "give the agent a wallet"
  from a thought experiment into a real code path with human
  approval baked in.

## What's Next

- **A real x402 endpoint to demo against.** Right now the Payments
  code path is functional but I'm exercising it with a synthetic
  402 responder. I want to wire it to a real paid API.
- **Windows port.** mlx-whisper → faster-whisper, rumps → pystray.
  The hotkey config (`<f19>`) is already portable; Windows users
  rebind via PowerToys.
- **Vocabulary UI.** AgentCore Memory has been quietly learning
  proper nouns for weeks. I have no UI to inspect or curate it.

---

If you tried v0.2 and got bitten by the hallucinations or hotkey,
v2 is the version to come back to. Issues and PRs:
[github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice).
