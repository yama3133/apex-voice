#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VoiceType - macOS 常駐の音声タイピングツール

仕組み:
  メニューバーのマイクで録音ON → 音量(RMS)ベースの簡易VADで発話区間を切り出し
  → ローカルWhisper(mlx-whisper)で文字起こし
  → クリップボード経由でアクティブなテキスト欄に挿入(Cmd+V)
  → もう一度クリックで録音OFF（押している間だけ認識するので誤入力・幻聴を抑制）

メニューバーから 録音ON/OFF・感度調整・終了 が可能。
"""

import os
import sys
import time
import zlib
import queue
import threading
import subprocess

import objc
import numpy as np
import sounddevice as sd
import rumps
from Foundation import NSObject
from AppKit import (
    NSApp as _AKNSApp,
    NSEventMaskLeftMouseUp,
    NSEventMaskRightMouseUp,
    NSEventTypeRightMouseUp,
    NSEventModifierFlagControl,
    NSPasteboard,
    NSPasteboardTypeString,
)

# ============================================================
# 設定（環境変数で上書き可）
# ============================================================
# 認識モデル（HuggingFace上のmlx-community形式）。初回起動時に自動DLされる。
MODEL = os.environ.get("VOICETYPE_MODEL", "mlx-community/whisper-large-v3-turbo")
# 認識言語（自動判定にしたい場合は空文字 "" にする）
LANGUAGE = os.environ.get("VOICETYPE_LANG", "ja")
# 用語ヒント(initial_prompt)。既定は空（定型句の幻聴を招くため入れない）。
# 専門用語の誤変換が気になる時だけ "Bedrock。AgentCore。mlx。" のように設定する。
PROMPT = os.environ.get("VOICETYPE_PROMPT", "")

SAMPLE_RATE = 16000          # Whisper想定の16kHz
BLOCK_SEC = 0.1              # 1ブロック=100ms
BLOCK = int(SAMPLE_RATE * BLOCK_SEC)

# VAD（発話区間検出）パラメータ
SILENCE_SEC = 0.8            # この長さ無音が続いたら1発話の区切りとみなす
MIN_SPEECH_SEC = 0.3         # これより短い音は雑音として破棄
MAX_SPEECH_SEC = 30.0        # 安全のため1発話の上限
PRE_PAD_SEC = 0.2            # 発話開始直前を少し含める（頭切れ防止）
START_SEC = 0.2             # この長さ連続で閾値超えしたら発話開始（単発ノイズ無視）
AUTO_STOP_SEC = 8.0         # 録音ONのまま無音がこの長さ続いたら自動停止

# 感度（環境ノイズに対する倍率）。大きいほど鈍感（拾いにくい）。
SENSITIVITY = float(os.environ.get("VOICETYPE_SENSITIVITY", "2.5"))
# デバッグ表示（VOICETYPE_DEBUG=1 で、マイク音量(RMS)を約1秒ごとにログ表示）
DEBUG = bool(os.environ.get("VOICETYPE_DEBUG"))
# 使用マイク（空=OS既定）。index番号か名前の一部を指定可。例: "5" や "MacBook"
MIC = os.environ.get("VOICETYPE_MIC", "")


def now():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def resolve_device():
    """VOICETYPE_MIC（index番号 or 名前の一部）から入力デバイスを解決。空ならOS既定(None)。"""
    if not MIC:
        return None
    if MIC.isdigit():
        return int(MIC)
    for i, d in enumerate(sd.query_devices()):
        if MIC.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    log(f"警告: マイク '{MIC}' が見つからずOS既定を使用")
    return None


# Whisperが無音/不明瞭音声で吐きがちなYouTube字幕系の定型句
HALLUCINATION_PHRASES = (
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "最後までご視聴",
    "チャンネル登録",
    "高評価",
    "次の動画",
    "また次回",
    "おやすみなさい",
)


def _compression_ratio(text: str) -> float:
    """反復だらけのテキストほど高くなる圧縮率。"""
    b = text.encode("utf-8")
    if not b:
        return 0.0
    return len(b) / max(1, len(zlib.compress(b)))


def looks_like_hallucination(text: str) -> bool:
    """反復・定型句などWhisper特有の幻聴らしさを判定（完全ではない）。"""
    t = "".join(ch for ch in text if ch not in " 　、。.,!?！？")
    if len(t) < 6:
        return False
    if len(set(t)) <= 3:                 # 文字種が極端に少ない
        return True
    for n in (1, 2, 3, 4):               # 先頭からの短い単位の連続反復
        unit = t[:n]
        if unit and t.startswith(unit * 6):
            return True
    if len(t) >= 12 and _compression_ratio(text) >= 3.0:  # 反復が支配的
        return True
    # YouTube字幕系の定型句が主成分
    stripped = text
    for p in HALLUCINATION_PHRASES:
        stripped = stripped.replace(p, "")
    if stripped != text and len(stripped.strip(" 　、。.,!?！？")) < 6:
        return True
    return False


# ============================================================
# テキスト挿入（クリップボード退避→Cmd+V→復元）
# ============================================================
class Inserter:
    def __init__(self):
        self.accessibility_warned = False

    def _pb_get(self) -> str:
        pb = NSPasteboard.generalPasteboard()
        s = pb.stringForType_(NSPasteboardTypeString)
        return str(s) if s is not None else ""

    def _pb_set(self, text: str):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    def insert(self, text: str, on_perm_error=None):
        text = text.strip()
        if not text:
            return
        prev = self._pb_get()                       # 既存クリップボードを退避
        self._pb_set(text)                          # 認識結果をコピー
        ok = self._paste()                          # Cmd+V を送出
        time.sleep(0.15)
        self._pb_set(prev)                          # クリップボードを復元
        if not ok and on_perm_error and not self.accessibility_warned:
            self.accessibility_warned = True
            on_perm_error()

    def _paste(self) -> bool:
        # System Events 経由で Cmd+V。アクセシビリティ権限が必要。
        script = 'tell application "System Events" to keystroke "v" using command down'
        r = subprocess.run(["osascript", "-e", script], capture_output=True)
        return r.returncode == 0


# ============================================================
# 文字起こし（mlx-whisper）
# ============================================================
class Transcriber:
    def __init__(self, model=MODEL, language=LANGUAGE, prompt=PROMPT):
        self.model = model
        self.language = language
        self.prompt = prompt
        self._mlx = None  # 遅延import（起動を速く）

    def _ensure(self):
        if self._mlx is None:
            import mlx_whisper
            self._mlx = mlx_whisper

    def transcribe(self, audio: np.ndarray) -> str:
        self._ensure()
        # 音量が小さいと幻聴が増えるためピーク正規化で底上げ
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0:
            audio = (audio / peak * 0.95).astype(np.float32)
        kwargs = dict(
            path_or_hf_repo=self.model,
            condition_on_previous_text=False,    # 直前テキストへの引きずられ(反復幻聴)を防ぐ
            no_speech_threshold=0.5,             # 無音をより無音と判定しやすく
            compression_ratio_threshold=2.2,     # 反復だらけの結果を弾く
        )
        if self.language:
            kwargs["language"] = self.language
        if self.prompt:
            kwargs["initial_prompt"] = self.prompt
        result = self._mlx.transcribe(audio, **kwargs)
        return (result.get("text") or "").strip()


# ============================================================
# 録音 + 簡易VAD
# ============================================================
class Recorder:
    def __init__(self, on_segment):
        self.on_segment = on_segment          # 区切れた発話(np.ndarray)を渡すコールバック
        self.listening = False
        self.stream = None

        # 状態
        self._speaking = False
        self._buf = []                        # 発話バッファ（ブロックのリスト）
        self._pre = []                        # 直前の数ブロック（頭切れ防止用リングバッファ）
        self._silence_blocks = 0
        self._noise_rms = 0.01                # 環境ノイズ推定（適応）
        self._dbg = 0                         # デバッグ表示用カウンタ
        self._hot_blocks = 0                  # 連続して閾値を超えたブロック数
        self._idle_blocks = 0                 # 録音ON中に発話がない無音ブロック数

        self._silence_limit = int(SILENCE_SEC / BLOCK_SEC)
        self._min_blocks = int(MIN_SPEECH_SEC / BLOCK_SEC)
        self._max_blocks = int(MAX_SPEECH_SEC / BLOCK_SEC)
        self._pre_blocks = int(PRE_PAD_SEC / BLOCK_SEC)
        self._start_blocks = max(1, int(START_SEC / BLOCK_SEC))
        self._auto_stop_blocks = int(AUTO_STOP_SEC / BLOCK_SEC)

    def start(self):
        dev = resolve_device()
        try:
            shown = dev if dev is not None else sd.default.device[0]
            name = sd.query_devices(shown)["name"]
        except Exception:
            name = "(デフォルト)"
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK, device=dev, callback=self._callback,
        )
        self.stream.start()
        log(f"マイク入力ストリーム開始: [{dev if dev is not None else 'OS既定'}] {name}")

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def flush(self):
        """録音停止時に、録音中の残りバッファを確定して認識へ回す。"""
        if self._speaking and self._buf:
            self._finalize()
        else:
            self._buf = []
            self._speaking = False
            self._silence_blocks = 0

    def _threshold(self):
        # 環境ノイズ × 感度 を発話判定の閾値にする（最低ラインあり）
        return max(self._noise_rms * SENSITIVITY, 0.006)

    def _callback(self, indata, frames, time_info, status):
        if status:
            # オーバーフロー等。致命的でないのでログのみ。
            pass
        block = indata[:, 0].copy()
        rms = float(np.sqrt(np.mean(block * block)) + 1e-12)

        if not self.listening:
            # 待機中も環境ノイズだけは緩く追従させておく
            self._noise_rms = 0.95 * self._noise_rms + 0.05 * rms
            return

        thresh = self._threshold()

        if DEBUG:
            self._dbg += 1
            if self._dbg % 10 == 0:  # 約1秒ごと
                bar = "#" * min(40, int(rms * 400))
                log(f"RMS={rms:.4f} 閾値={thresh:.4f} {'[発話中]' if self._speaking else ''} {bar}")

        if rms > thresh:
            self._hot_blocks += 1
            self._idle_blocks = 0
            if not self._speaking and self._hot_blocks >= self._start_blocks:
                self._speaking = True
                self._buf = list(self._pre)   # 頭の数ブロックを先頭に付ける
            if self._speaking:
                self._buf.append(block)
                self._silence_blocks = 0
        else:
            self._hot_blocks = 0
            if self._speaking:
                self._buf.append(block)       # 末尾の無音も少し含める
                self._silence_blocks += 1
                if self._silence_blocks >= self._silence_limit:
                    self._finalize()
            else:
                # 無音中は環境ノイズを推定更新し、長く続いたら自動停止
                self._noise_rms = 0.9 * self._noise_rms + 0.1 * rms
                self._idle_blocks += 1
                if self._idle_blocks >= self._auto_stop_blocks:
                    self._idle_blocks = 0
                    self.listening = False
                    log("無音が続いたため録音を自動停止しました")

        # 頭切れ防止用リングバッファ更新
        self._pre.append(block)
        if len(self._pre) > self._pre_blocks:
            self._pre.pop(0)

        # 長すぎる発話は強制確定
        if self._speaking and len(self._buf) >= self._max_blocks:
            self._finalize()

    def _finalize(self):
        buf, self._buf = self._buf, []
        self._speaking = False
        self._silence_blocks = 0
        if len(buf) >= self._min_blocks:
            audio = np.concatenate(buf).astype(np.float32)
            thresh = self._threshold()
            # ピークが閾値を十分超えない区間はノイズ(幻聴の元)として破棄
            if float(np.max(np.abs(audio))) < thresh * 1.5:
                return
            # 実際に声があったブロックが少なすぎる区間も破棄
            active = sum(1 for b in buf if float(np.sqrt(np.mean(b * b))) > thresh)
            if active < self._min_blocks:
                return
            self.on_segment(audio)


class _IconClickTarget(NSObject):
    """メニューバーアイコンの直接クリックを受けるハンドラ（PyObjC）。"""
    def initWithApp_(self, app):
        self = objc.super(_IconClickTarget, self).init()
        if self is None:
            return None
        self._app = app
        return self

    def onClick_(self, sender):
        self._app.on_icon_click()


# ============================================================
# メニューバー常駐アプリ
# ============================================================
ICON_IDLE = "🎤"     # 待機中（クリックで録音開始）
ICON_REC = "🔴"      # 録音中（クリックで停止）
ICON_WORK = "✍️"     # 認識処理中


class VoiceTypeApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_IDLE, quit_button=None)
        self.transcriber = Transcriber()
        self.inserter = Inserter()
        self.recorder = Recorder(on_segment=self._enqueue)
        self.jobs = queue.Queue()

        self.item_toggle = rumps.MenuItem("🎤 録音開始", callback=self.toggle)
        self.item_status = rumps.MenuItem("状態: 停止中", callback=None)
        self.menu = [
            self.item_toggle,
            self.item_status,
            None,
            rumps.MenuItem("感度を上げる (拾いやすく)", callback=self.sens_up),
            rumps.MenuItem("感度を下げる (拾いにくく)", callback=self.sens_down),
            None,
            rumps.MenuItem("終了", callback=self.quit_app),
        ]

        self._icon_hacked = False
        self._click_target = None

        # 録音とワーカーを開始（録音は停止状態でスタート。アイコンのクリックで開始する）
        self.recorder.start()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- 自動停止のUI同期 ----
    @rumps.timer(1)
    def _sync_state(self, _):
        if not self.recorder.listening and self.title == ICON_REC:
            self.title = ICON_IDLE
            self.item_toggle.title = "🎤 録音開始"
            self.item_status.title = "状態: 停止中(自動)"

    # ---- アイコン直接クリック（左=録音トグル / 右or⌃=メニュー）----
    @rumps.timer(0.5)
    def _ensure_icon_click(self, _):
        if not self._icon_hacked:
            self._setup_icon_click()

    def _setup_icon_click(self):
        try:
            item = self._nsapp.nsstatusitem
            button = item.button()
            if button is None:
                return
            self._click_target = _IconClickTarget.alloc().initWithApp_(self)
            button.setTarget_(self._click_target)
            button.setAction_("onClick:")
            button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
            item.setMenu_(None)
            self._icon_hacked = True
            log("アイコンの直接クリックを有効化（左=録音 / 右=メニュー）")
        except Exception as e:
            log(f"アイコンクリック設定に失敗: {e}")

    def on_icon_click(self):
        event = _AKNSApp().currentEvent()
        is_right = False
        try:
            is_right = (event.type() == NSEventTypeRightMouseUp) or \
                       bool(event.modifierFlags() & NSEventModifierFlagControl)
        except Exception:
            pass
        if is_right:
            item = self._nsapp.nsstatusitem
            item.setMenu_(self._menu)
            item.button().performClick_(None)
            item.setMenu_(None)
        else:
            self.toggle(None)

    # ---- メニュー操作 ----
    def toggle(self, _):
        if not self.recorder.listening:
            # 録音開始
            self.recorder.listening = True
            self.title = ICON_REC
            self.item_toggle.title = "■ 録音停止"
            self.item_status.title = "状態: 録音中"
            log("録音開始")
        else:
            # 録音停止 → 残りのバッファを確定して認識
            self.recorder.listening = False
            self.recorder.flush()
            self.title = ICON_IDLE
            self.item_toggle.title = "🎤 録音開始"
            self.item_status.title = "状態: 停止中"
            log("録音停止")

    def sens_up(self, _):
        global SENSITIVITY
        SENSITIVITY = max(1.2, SENSITIVITY - 0.3)
        rumps.notification("VoiceType", "感度", f"感度: {SENSITIVITY:.1f}（小さいほど拾いやすい）")

    def sens_down(self, _):
        global SENSITIVITY
        SENSITIVITY = min(6.0, SENSITIVITY + 0.3)
        rumps.notification("VoiceType", "感度", f"感度: {SENSITIVITY:.1f}（大きいほど拾いにくい）")

    def quit_app(self, _):
        self.recorder.stop()
        rumps.quit_application()

    # ---- 認識ワーカー ----
    def _enqueue(self, audio):
        self.jobs.put(audio)

    def _worker(self):
        while True:
            audio = self.jobs.get()
            try:
                self.title = ICON_WORK
                text = self.transcriber.transcribe(audio)
                if text and looks_like_hallucination(text):
                    log(f"幻聴として破棄: {text[:30]}")
                elif text:
                    log(f"認識: {text}")
                    self.inserter.insert(text, on_perm_error=self._warn_accessibility)
            except Exception as e:
                log(f"認識エラー: {e}")
            finally:
                self.title = ICON_REC if self.recorder.listening else ICON_IDLE

    def _warn_accessibility(self):
        rumps.notification(
            "VoiceType", "アクセシビリティ許可が必要",
            "システム設定 > プライバシーとセキュリティ > アクセシビリティ で許可してください",
        )
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])


if __name__ == "__main__":
    VoiceTypeApp().run()
