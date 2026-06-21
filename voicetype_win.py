# -*- coding: utf-8 -*-
"""
Apex Voice for Windows
  - faster-whisper (CPU/int8) で音声認識
  - pyperclip + Ctrl+V でテキスト挿入
  - pystray でタスクトレイ常駐
  - pynput でグローバルホットキー (F19 / Caps Lock)
"""
import os
import sys
import json
import time
import zlib
import queue
import threading
import subprocess
from pathlib import Path

try:
    import setproctitle
    setproctitle.setproctitle("Apex Voice")
except ImportError:
    pass

# ============================================================
# 設定
# ============================================================
CONFIG_PATH = Path.home() / ".apexvoice" / "config.json"
VOCAB_PATH  = Path.home() / ".apexvoice" / "vocabulary.json"

SAMPLE_RATE  = 16000
BLOCK_SEC    = 0.1
BLOCK        = int(SAMPLE_RATE * BLOCK_SEC)
MIN_SPEECH_SEC = 0.4
MAX_SPEECH_SEC = 120.0
PRE_PAD_SEC  = 0.5

MODEL_DEFAULT = os.environ.get(
    "VOICETYPE_MODEL", "large-v3-turbo"
)
LANGUAGE = os.environ.get("VOICETYPE_LANG", "ja")
DEFAULT_HOTKEY = "<f19>"

BEDROCK_MODEL_ID = os.environ.get(
    "APEXVOICE_BEDROCK_MODEL",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_REGION = os.environ.get("APEXVOICE_BEDROCK_REGION", "us-east-1")

STRONG_HALLUCINATION_PHRASES = (
    "ご視聴ありがとう", "ご清聴ありがとう", "最後までご視聴",
    "チャンネル登録", "高評価", "次の動画", "また次回",
    "お会いしましょう", "皆さんこんにちは",
)
WEAK_HALLUCINATION_PHRASES = ("おやすみなさい",)


# ============================================================
# ユーティリティ
# ============================================================
def now():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _compression_ratio(text: str) -> float:
    b = text.encode("utf-8")
    if not b:
        return 0.0
    return len(b) / max(1, len(zlib.compress(b)))


def looks_like_hallucination(text: str) -> bool:
    t = "".join(ch for ch in text if ch not in " 　、。.,!?！？")
    if len(t) < 6:
        return False
    if len(set(t)) <= 3:
        return True
    for n in (1, 2, 3, 4):
        unit = t[:n]
        if unit and t.startswith(unit * 6):
            return True
    if len(t) >= 12 and _compression_ratio(text) >= 3.0:
        return True
    for p in STRONG_HALLUCINATION_PHRASES:
        if p in text:
            return True
    stripped = text
    for p in WEAK_HALLUCINATION_PHRASES:
        stripped = stripped.replace(p, "")
    if stripped != text and len(stripped.strip(" 　、。.,!?！？")) < 6:
        return True
    return False


# ============================================================
# Silero VAD
# ============================================================
_SILERO_MODEL = None


def _silero_load():
    global _SILERO_MODEL
    if _SILERO_MODEL is not None:
        return _SILERO_MODEL or None
    try:
        from silero_vad import load_silero_vad
        _SILERO_MODEL = load_silero_vad()
        log("Silero VAD ロード完了")
    except Exception as e:
        log(f"Silero VAD ロード失敗: {e}")
        _SILERO_MODEL = False
    return _SILERO_MODEL or None


def _silero_has_speech(audio, sr: int = 16000) -> bool:
    import numpy as np
    model = _silero_load()
    if model is None:
        return True
    try:
        import torch
        from silero_vad import get_speech_timestamps
        a = audio if audio.dtype == np.float32 else audio.astype(np.float32)
        ts = get_speech_timestamps(
            torch.from_numpy(a), model,
            sampling_rate=sr, min_speech_duration_ms=150, threshold=0.5
        )
        return bool(ts)
    except Exception:
        return True


# ============================================================
# 録音 (push-to-talk)
# ============================================================
class Recorder:
    def __init__(self, on_segment):
        self.on_segment  = on_segment
        self.listening   = False
        self._buf        = []
        self._speaking   = False
        self._pre        = []
        self._pre_blocks = int(PRE_PAD_SEC / BLOCK_SEC)
        self._max_blocks = int(MAX_SPEECH_SEC / BLOCK_SEC)
        self._stream     = None
        self._dbg        = 0

    def start(self):
        import sounddevice as sd
        import numpy as np
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, blocksize=BLOCK,
            dtype="float32", callback=self._callback
        )
        self._stream.start()
        log("マイク入力ストリーム開始")

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def flush(self):
        if self._buf:
            self._finalize()
        else:
            self._speaking = False

    def _callback(self, indata, frames, time_info, status):
        import numpy as np
        block = indata[:, 0].copy()
        if not self.listening:
            self._pre.append(block)
            if len(self._pre) > self._pre_blocks:
                self._pre.pop(0)
            return
        if not self._speaking:
            self._speaking = True
            self._buf = list(self._pre)
        self._buf.append(block)
        if len(self._buf) >= self._max_blocks:
            log(f"最大録音長に到達 ({MAX_SPEECH_SEC:.0f}s) 強制確定")
            self._finalize()

    def _finalize(self):
        import numpy as np
        buf, self._buf = self._buf, []
        self._speaking = False
        if not buf:
            return
        duration = len(buf) * BLOCK_SEC
        if duration < MIN_SPEECH_SEC:
            log(f"録音破棄: 短すぎ ({duration:.2f}s)")
            return
        audio = np.concatenate(buf).astype(np.float32)
        self.on_segment(audio)


# ============================================================
# 文字起こし (faster-whisper)
# ============================================================
class Transcriber:
    def __init__(self, model=MODEL_DEFAULT, language=LANGUAGE):
        self.model    = model
        self.language = language
        self._model   = None

    def _ensure(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log(f"Whisperモデルロード中: {self.model}")
            self._model = WhisperModel(
                self.model, device="cpu", compute_type="int8"
            )
            log("Whisperモデルロード完了")

    def transcribe(self, audio) -> str:
        import numpy as np
        self._ensure()
        duration = len(audio) / SAMPLE_RATE
        if duration < MIN_SPEECH_SEC:
            return ""
        t_vad = time.time()
        if not _silero_has_speech(audio):
            log("Silero VAD: 音声区間なし → 破棄")
            return ""
        vad_ms = (time.time() - t_vad) * 1000
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio = (audio / peak * 0.95).astype(np.float32)
        t_w = time.time()
        segments, _ = self._model.transcribe(
            audio,
            language=self.language or None,
            condition_on_previous_text=False,
            no_speech_threshold=0.7,
            log_prob_threshold=-0.5,
            compression_ratio_threshold=2.2,
            temperature=0.0,
            beam_size=5,
        )
        text = "".join(s.text for s in segments).strip()
        whisper_ms = (time.time() - t_w) * 1000
        log(f"認識所要 audio={duration:.2f}s vad={vad_ms:.0f}ms "
            f"whisper={whisper_ms:.0f}ms → {len(text)}文字")
        return text


# ============================================================
# テキスト挿入 (pyperclip + Ctrl+V)
# ============================================================
class Inserter:
    def insert(self, text: str, on_perm_error=None):
        text = text.strip()
        if not text:
            return
        t0 = time.time()
        try:
            import pyperclip
            from pynput.keyboard import Controller, Key
            prev = pyperclip.paste()
            pyperclip.copy(text)
            time.sleep(0.05)
            kb = Controller()
            with kb.pressed(Key.ctrl):
                kb.press('v')
                kb.release('v')
            elapsed = (time.time() - t0) * 1000
            log(f"挿入完了 ({elapsed:.0f}ms): {text[:30]}")
            def _restore():
                time.sleep(0.1)
                pyperclip.copy(prev)
            threading.Thread(target=_restore, daemon=True).start()
        except Exception as e:
            log(f"挿入エラー: {e}")


# ============================================================
# ホットキー (pynput)
# ============================================================
class HotkeyManager:
    def __init__(self, hotkey: str, callback):
        self.hotkey   = hotkey
        self.callback = callback
        self._listener = None

    def start(self):
        if not self.hotkey:
            return
        try:
            from pynput import keyboard
            self._listener = keyboard.GlobalHotKeys(
                {self.hotkey: self._on_activate}
            )
            self._listener.start()
            log(f"ホットキー登録: {self.hotkey}")
        except Exception as e:
            log(f"ホットキー登録失敗: {e}")

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass

    def _on_activate(self):
        try:
            self.callback()
        except Exception as e:
            log(f"ホットキー処理エラー: {e}")


# ============================================================
# Bedrock後処理
# ============================================================
class Postprocessor:
    def _client(self):
        import boto3
        return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    def apply(self, text: str, mode: str) -> str:
        prompts = {
            "polish": (
                "以下の音声認識テキストを自然な書き言葉に整文してください。"
                "テキストのみ返してください。\n\n" + text
            ),
            "formal": (
                "以下のテキストをビジネス敬語に変換してください。"
                "テキストのみ返してください。\n\n" + text
            ),
            "translate": (
                "以下の日本語を自然な英語に翻訳してください。"
                "翻訳文のみ返してください。\n\n" + text
            ),
            "bullets": (
                "以下の内容を箇条書きにまとめてください。"
                "箇条書きのみ返してください。\n\n" + text
            ),
        }
        prompt = prompts.get(mode)
        if not prompt:
            return text
        try:
            c = self._client()
            r = c.converse(
                modelId=BEDROCK_MODEL_ID,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 1000, "temperature": 0.3},
            )
            out = r["output"]["message"]["content"][0]["text"].strip()
            log(f"後処理({mode}): {out[:50]}")
            return out
        except Exception as e:
            log(f"後処理エラー({mode}): {e}")
            return text


# ============================================================
# pystray タスクトレイ UI
# ============================================================
ICON_SIZE = 64


def _make_icon(recording: bool = False):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (ICON_SIZE, ICON_SIZE), (30, 30, 30))
    d = ImageDraw.Draw(img)
    color = (255, 80, 50) if recording else (180, 180, 180)
    cx, cy, r = ICON_SIZE // 2, ICON_SIZE // 2, ICON_SIZE // 3
    d.ellipse(
        [(cx - r // 2, cy - r), (cx + r // 2, cy + r // 3)],
        fill=color
    )
    d.arc(
        [(cx - r, cy - r // 2), (cx + r, cy + r)],
        start=0, end=180, fill=color, width=3
    )
    d.line([(cx, cy + r // 2), (cx, cy + r)], fill=color, width=3)
    return img


class ApexVoiceApp:
    def __init__(self):
        self.config       = load_config()
        self.postprocess  = self.config.get("postprocess", "raw")
        self.recording    = False
        self.jobs         = queue.Queue()

        self.transcriber  = Transcriber(
            language=self.config.get("language", LANGUAGE)
        )
        self.postprocessor = Postprocessor()
        self.inserter      = Inserter()
        self.recorder      = Recorder(on_segment=self._enqueue)

        hotkey = self.config.get("hotkey", DEFAULT_HOTKEY)
        self.hotkey_mgr = HotkeyManager(hotkey, self._toggle)

        import pystray
        self._icon = pystray.Icon(
            "ApexVoice",
            _make_icon(False),
            "Apex Voice",
            self._build_menu()
        )

    def _build_menu(self):
        import pystray
        items = [
            pystray.MenuItem(
                lambda _: "■ 録音停止" if self.recording else "🎤 録音開始",
                self._toggle, default=True
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("後処理: raw", lambda _: self._set_pp("raw")),
            pystray.MenuItem("後処理: 整文", lambda _: self._set_pp("polish")),
            pystray.MenuItem("後処理: 敬語", lambda _: self._set_pp("formal")),
            pystray.MenuItem("後処理: 英訳", lambda _: self._set_pp("translate")),
            pystray.MenuItem("後処理: 箇条書き", lambda _: self._set_pp("bullets")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", self._quit),
        ]
        return pystray.Menu(*items)

    def _set_pp(self, mode: str):
        self.postprocess = mode
        self.config["postprocess"] = mode
        save_config(self.config)
        log(f"後処理モード: {mode}")

    def _toggle(self, *_):
        if not self.recording:
            self.recording = True
            self.recorder.listening = True
            self._icon.icon = _make_icon(True)
            self._icon.title = "Apex Voice — 録音中"
            log("録音開始")
        else:
            self.recording = False
            self.recorder.listening = False
            self.recorder.flush()
            self._icon.icon = _make_icon(False)
            self._icon.title = "Apex Voice"
            log("録音停止")

    def _quit(self, *_):
        log("終了")
        self.recorder.stop()
        self.hotkey_mgr.stop()
        self._icon.stop()

    def _enqueue(self, audio):
        self.jobs.put(audio)

    def _worker(self):
        while True:
            audio = self.jobs.get()
            if audio is None:
                break
            try:
                log("認識処理開始")
                text = self.transcriber.transcribe(audio)
                if not text:
                    continue
                if looks_like_hallucination(text):
                    log(f"幻聴として破棄: {text[:30]}")
                    continue
                log(f"認識: {text}")
                if self.postprocess != "raw":
                    text = self.postprocessor.apply(text, self.postprocess)
                self.inserter.insert(text)
            except Exception as e:
                log(f"認識エラー: {e}")

    def run(self):
        log("Apex Voice for Windows 起動")
        self.recorder.start()
        self.hotkey_mgr.start()
        threading.Thread(target=_silero_load, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        self._icon.run()


# ============================================================
# エントリポイント
# ============================================================
if __name__ == "__main__":
    ApexVoiceApp().run()
