---
title: "Porting Apex Voice to Windows broke at every single layer (faster-whisper + pystray + pywin32)"
published: false
tags: aws, bedrock, python, windows
---

> I ported my macOS voice-typing app **Apex Voice** to Windows,
> aiming for feature parity. Every layer of the stack tripped me up.
> Here's the honest log.

## Where I started

[Apex Voice](https://github.com/yama3133/apex-voice) is the macOS
menu-bar voice-typing app I built on mlx-whisper + rumps + LaunchAgent.
Apple Silicon-only by design, so it doesn't run on Windows as-is.

The plan was a port for parity. The only test environment I had on
hand was **Windows 11 ARM64 (evaluation) on UTM**. I did not expect
this to be a months-of-effort kind of project. It wasn't months, but
it was every layer.

- Repo: [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
- Windows README: [README_win.md](https://github.com/yama3133/apex-voice/blob/main/README_win.md)

## The stack I picked

The substitutions I started with:

| | macOS | Windows |
|---|---|---|
| Speech recognition | mlx-whisper | faster-whisper (CPU/int8) |
| Tray | rumps | pystray |
| Text insertion | NSPasteboard + osascript Cmd+V | pyperclip + pynput Ctrl+V |
| Auto-launch | LaunchAgent | Task Scheduler |
| Caps Lock one-key | Karabiner-Elements | AutoHotkey v2 |

In my head this was "swap the libraries." In reality, **every swapped
layer** broke in its own way.

## Pain #1: sounddevice doesn't load on ARM64

First blocker. I wanted to use sounddevice for mic input, but on
ARM64 Windows it fails with `libportaudioarm64.dll: error 0x7e`.
PortAudio doesn't ship an ARM64 binary.

→ Fall back to pyaudio. pyaudio has an ARM64 wheel and just works.
Rewrote the Recorder class around it.

```python
class Recorder:
    """sounddevice doesn't work on ARM64 Windows; use pyaudio."""

    def _start_pyaudio(self):
        import pyaudio
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            rate=SAMPLE_RATE, channels=1, format=pyaudio.paFloat32,
            input=True, frames_per_buffer=BLOCK
        )
```

## Pain #2: zero output on startup

Ran `python voicetype_win.py` and the console showed nothing. The
process was running but looked frozen.

My first guess: pystray is blocking the main thread. Added print
statements everywhere. Still nothing.

The actual cause: **the Windows console was choking on bytes written
to stderr**. pyaudio and pystray write something to stderr at startup
that the console's code page can't render, and the output buffer
stalls.

Workaround:

```cmd
python -u voicetype_win.py 2>err.txt
```

`-u` unbuffers stdout, `2>err.txt` redirects stderr to a file. After
that I could finally see my `STEP1` `STEP2` ... diagnostic prints.

I spent over an hour debugging "why aren't my `flush=True` prints
appearing."

## Pain #3: pynput can't catch F19

On macOS I use Karabiner to remap Caps Lock → F19, and pynput
listens for F19. I tried to replicate this with PowerToys on Windows.

The PowerToys remap itself worked (the Caps Lock LED no longer
toggled, meaning the original Caps Lock behavior was suppressed).
But pynput didn't catch F19. The log said `hotkey registered: <f19>`,
but pressing the key did nothing.

I later found that pynput on Windows has known issues with F13 and
above. Switched the hotkey to `<ctrl>+<alt>+r` and it worked
immediately.

## Pain #4: PowerToys ate Ctrl+V

Mid-debug, PowerToys Keyboard Manager somehow **hijacked Ctrl+V
itself**. Even after stopping Apex Voice, Ctrl+V no longer pasted.
PowerToys' keyboard hook had stacked some leftover state.

Eventually I lost **all** keyboard input to Notepad and had to
reboot the VM. After that I dropped PowerToys (it felt brittle) and
switched to **AutoHotkey v2** for the remap.

```ahk
#Requires AutoHotkey v2.0
SetCapsLockState("AlwaysOff")
CapsLock::Send("^!r")
```

`SetCapsLockState("AlwaysOff")` suppresses the Caps Lock toggle
behavior (the uppercase mode).

## Pain #5: Tray clicks steal focus

Click tray → stop recording → Whisper transcribes → paste into
Notepad. The "inserted" log appeared, but **nothing showed up in
Notepad**. The moment I clicked the tray, focus had moved to the
Command Prompt, and Ctrl+V was pasting there.

Fix: use pywin32 to save the foreground window handle when recording
starts, and restore it right before the paste:

```python
def _toggle(self, *_):
    if not self.recording:
        try:
            import win32gui
            self.inserter._prev_hwnd = win32gui.GetForegroundWindow()
        except Exception:
            pass
        ...

def insert(self, text: str):
    import win32gui
    prev_hwnd = getattr(self, '_prev_hwnd', None)
    if prev_hwnd:
        try:
            win32gui.SetForegroundWindow(prev_hwnd)
            time.sleep(0.15)
        except Exception:
            pass
    # then pyperclip + Ctrl+V
```

On macOS, osascript handled active-app restoration implicitly, so
this code didn't exist. OS-layer differences exposed.

## Pain #6: Bedrock — MissingDependencyException

`aws login` works, `aws sts get-caller-identity` returns my user, but
calling Bedrock via boto3 from Python:

```
botocore.exceptions.MissingDependencyException: Missing Dependency:
Using the login credentials provider requires an additional dependency.
You will need to pip install "botocore[crt]"
```

Credentials produced by `aws login` (AWS CLI 2.32.0+) need botocore's
optional `crt` extra:

```cmd
pip install "botocore[crt]"
```

After that, polish/formal/translate/bullets all worked.

## Pain #7: Caps Lock doesn't reach the VM in UTM

Running the AutoHotkey script as admin, pressing Caps Lock did
nothing. AHK was never triggered.

Cut to the chase: **the Mac → UTM → Windows key path drops Caps
Lock somewhere**. Either UTM's keyboard pass-through doesn't relay
it, or macOS absorbs it at the OS level before UTM sees it. Hard to
pin down.

This wouldn't happen on bare-metal Windows. I gave up on Caps Lock
1-key and shipped with **`<ctrl>+<alt>+r` pressed directly** as the
hotkey. Control+Option+R on the Mac keyboard arrives in Windows as
Ctrl+Alt+R through UTM, so functionally it works.

## Auto-launch on login

Task Scheduler with `/sc onlogon /rl highest`:

```cmd
schtasks /create /tn "ApexVoice" /tr "%USERPROFILE%\apex_voice_start.bat" /sc onlogon /rl highest /f
schtasks /create /tn "ApexCapsAHK" /tr "%USERPROFILE%\apex_caps.ahk" /sc onlogon /rl highest /f
```

Important: if Apex Voice runs as administrator, AutoHotkey must also
run as administrator (Windows blocks key injection from a lower-
privilege process to a higher-privilege one).

## End state and performance

Measured on UTM (no bare metal at hand):

| Metric | UTM (ARM64) | Estimated (bare-metal Windows) |
|---|---|---|
| 4s audio recognition | ~37s (CPU/int8, large-v3-turbo) | a few seconds |
| Insertion latency | ~250ms | similar |
| Bedrock post-processing | works | works |
| Caps Lock 1-key | impossible (UTM limitation) | should work |

UTM's virtual CPU is too slow for Whisper at this model size. On
real Windows hardware `large-v3-turbo` should be practical. Need
bare-metal testers for that.

## What I learned

- **"Just swap the libraries" is a lie.** OS permission models, key
  codes, console code pages, focus management — even Python apps
  hit all of these at the lower layers.
- **When console output disappears, suspect stderr.** Splitting it
  off with `2>err.txt` made everything visible.
- **Key remappers that grab modifiers are dangerous.** PowerToys
  Keyboard Manager left leftover state that killed Ctrl+V. AHK v2
  has been more stable.
- **The real cost of cross-platform isn't code volume**, it's the
  number of OS-specific traps you have to step into.

## What's next

- **Verification on bare-metal Windows.** UTM is enough for validation
  but I want to confirm performance and the Caps Lock path on real
  hardware.
- **GPU inference.** faster-whisper supports CUDA, so an NVIDIA-GPU
  Windows machine should be dramatically faster.
- **AgentCore Memory / Browser / Payments on Windows.** The code path
  is shared with macOS but I haven't verified it on Windows yet.

---

The Windows port is functional on UTM, published, and documented in
README_win.md. If you try it on bare metal, issues/PRs welcome:
[github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice).
