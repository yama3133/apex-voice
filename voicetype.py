#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apex Voice - macOS 常駐の音声タイピング & AIエージェント

仕組み:
  メニューバーのマイクで録音ON → 音量(RMS)ベースの簡易VADで発話区間を切り出し
  → ローカルWhisper(mlx-whisper)で文字起こし
  → クリップボード経由でアクティブなテキスト欄に挿入(Cmd+V)
  → もう一度クリックで録音OFF（押している間だけ認識するので誤入力・幻聴を抑制）

メニューバーから 録音ON/OFF・感度調整・終了 が可能。
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

import numpy as np
import sounddevice as sd
import rumps
from AppKit import NSPasteboard, NSPasteboardTypeString

# ============================================================
# 設定（環境変数で上書き可）
# ============================================================
# 認識モデル（HuggingFace上のmlx-community形式）。初回起動時に自動DLされる。
MODEL = os.environ.get("VOICETYPE_MODEL", "mlx-community/whisper-large-v3-turbo")
# 認識言語（自動判定にしたい場合は空文字 "" にする）
# 環境変数が指定されていればそれを優先。空ならconfigファイルから読み込む。
LANGUAGE = os.environ.get("VOICETYPE_LANG", "ja")

# 言語選択用のメニュー定義（表示名, Whisperコード）
# Whisperは100言語に対応。よく使う言語を主菜単に並べ、その他はサブメニューにできる。
LANGUAGES = [
    ("自動判定", ""),
    ("日本語", "ja"),
    ("English", "en"),
    ("中文", "zh"),
    ("한국어", "ko"),
    ("Español", "es"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Italiano", "it"),
    ("Português", "pt"),
    ("Русский", "ru"),
]

# 設定の永続化（メニューバーで選んだ言語等を保存）
CONFIG_PATH = Path.home() / ".apexvoice" / "config.json"
# 旧名 ~/.whistype/ からの自動移行用
_LEGACY_CONFIG_DIR = Path.home() / ".whistype"

# 後処理モード (label, mode_key, prompt)
# 'raw'はLLMを呼ばずそのまま返す。それ以外はBedrock Claude Haiku 4.5に投げる。
POSTPROCESS_MODES = [
    ("生（そのまま）", "raw", None),
    ("整文（フィラー除去・誤認識補正）", "polish",
     "次は音声認識の結果テキストです。以下のルールで整えてください:\n"
     "1. フィラー(えーと、あの、その等)や言い淀みを取り除く\n"
     "2. 音声認識特有の同音異義の誤り(例: 聖典→晴天、機構→気候、慣性→歓声 等)を文脈から判断して修正\n"
     "3. 自然な書き言葉にする(意味や情報は変えない、内容は追加しない)\n"
     "4. ユーザーが意図的に繰り返している語句は省略せず保持する\n"
     "5. 同じ言語のまま、整形結果のテキストのみを返す。前置きや説明は不要"),
    ("敬語化", "formal",
     "次のテキストを、ビジネスで使える丁寧な敬語に書き換えてください。意味は変えず、"
     "同じ言語で整形結果のテキストのみを返してください。前置きや説明は不要です。"),
    ("英訳", "english",
     "次のテキストを、フィラー(えーと、あの、um, uh等)を取り除き、"
     "自然で読みやすい英語に翻訳してください。"
     "翻訳結果のテキストのみを返してください。前置きや説明は不要です。"),
    ("箇条書きに要約", "bullets",
     "次のテキストの要点を短い箇条書き(各行の先頭に「・」)にまとめてください。"
     "元の言語で出力し、箇条書きのみを返してください。前置きや説明は不要です。"),
    ("エージェント実行（リマインダー・カレンダー・検索）", "agent", None),
]

# Bedrockモデル(後処理用)。アカウント761018866498/us-east-1で疎通確認済み
BEDROCK_MODEL_ID = os.environ.get(
    "APEXVOICE_BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_REGION = os.environ.get("APEXVOICE_BEDROCK_REGION", "us-east-1")

# AgentCore Memory: ユーザー語彙の永続化先
AGENTCORE_MEMORY_ID = os.environ.get(
    "APEXVOICE_MEMORY_ID", "whistype_personal_vocabulary-YrdZy493pf"
)
AGENTCORE_ACTOR_ID = os.environ.get("APEXVOICE_ACTOR_ID", "default-user")

# ローカル語彙ファイル
VOCAB_PATH = Path.home() / ".apexvoice" / "vocabulary.json"

# 旧名 ~/.whistype/ にデータがあれば ~/.apexvoice/ に1回だけ自動移行
def _migrate_legacy_config():
    if _LEGACY_CONFIG_DIR.exists() and not CONFIG_PATH.parent.exists():
        try:
            import shutil
            shutil.copytree(_LEGACY_CONFIG_DIR, CONFIG_PATH.parent)
            print(f"[apex-voice] 旧設定 {_LEGACY_CONFIG_DIR} を {CONFIG_PATH.parent} に移行")
        except Exception as e:
            print(f"[apex-voice] 設定移行失敗: {e}")
_migrate_legacy_config()
# initial_prompt に注入する上位語の最大数
VOCAB_TOP_N = 30

# グローバルホットキー（pynput形式の文字列）。configで上書き可。
# 例: "<ctrl>+<alt>+v" / "<cmd>+<shift>+<space>" / "<f5>"
# OFFにしたい場合は空文字 "" を指定。
DEFAULT_HOTKEY = "<ctrl>+<alt>+v"


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"設定保存に失敗: {e}")
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
# 語彙メモリ（AgentCore Memory + ローカルキャッシュ）
# ============================================================
import re

# 抽出対象: 2文字以上の連続漢字、または2文字以上の連続カタカナ、または3文字以上の英数
_VOCAB_RE = re.compile(
    r"[一-鿿]{2,}|"          # 連続漢字
    r"[゠-ヿ]{2,}|"          # 連続カタカナ
    r"[A-Za-z][A-Za-z0-9]{2,}"  # 英数(先頭は英字、3文字以上)
)
# 抽出から除外する一般語(ノイズ)
_VOCAB_STOPWORDS = {
    "これ", "それ", "あれ", "今日", "明日", "昨日", "本日", "昨年", "今年",
    "今月", "来月", "今週", "来週", "毎日", "毎週", "毎月",
    "場合", "時間", "問題", "対応", "確認", "状態", "状況", "内容",
    "皆様", "私達", "自分", "相手", "誰か", "何か",
}
# 語彙ヒント注入の閾値
VOCAB_MIN_FREQ = 3      # この回数以上出現した語のみヒントに採用
VOCAB_TOP_N_HINT = 15   # ヒントに採用する上位N語(注入過多で幻聴を招くため小さく)


class MemoryManager:
    """ユーザー語彙(固有名詞・専門用語・表記嗜好)を蓄積し、Whisperにヒントとして注入する。

    - ローカル: ~/.apexvoice/vocabulary.json に語の頻度を保存(即座に効く)
    - クラウド: AgentCore Memory に event を非同期書き込み(他端末同期・永続化)
    """

    def __init__(self):
        self.vocab = {}                      # term -> freq
        self.session_id = f"apexvoice-{int(time.time())}"
        self._client = None
        self._client_tried = False
        self._lock = threading.Lock()
        self._load_local()

    def _load_local(self):
        try:
            if VOCAB_PATH.exists():
                self.vocab = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"語彙ロード失敗: {e}")
            self.vocab = {}

    def _save_local(self):
        try:
            VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
            VOCAB_PATH.write_text(
                json.dumps(self.vocab, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log(f"語彙保存失敗: {e}")

    def _ensure_client(self):
        if self._client_tried:
            return
        self._client_tried = True
        if not AGENTCORE_MEMORY_ID:
            return
        try:
            import boto3
            self._client = boto3.client("bedrock-agentcore", region_name=BEDROCK_REGION)
        except Exception as e:
            log(f"AgentCore Memoryクライアント初期化失敗: {e}")
            self._client = None

    def _extract_terms(self, text: str):
        terms = []
        for m in _VOCAB_RE.findall(text):
            if m in _VOCAB_STOPWORDS:
                continue
            if len(m) < 2:
                continue
            terms.append(m)
        return terms

    def record(self, raw_text: str, polished_text: str = None):
        """認識結果を語彙に反映。整文済みテキストがあればそれを優先。
        AgentCoreへのevent書き込みは別スレッドで非同期に実行する。"""
        target = (polished_text or raw_text or "").strip()
        if not target:
            return
        with self._lock:
            for term in self._extract_terms(target):
                self.vocab[term] = self.vocab.get(term, 0) + 1
            self._save_local()
        threading.Thread(
            target=self._write_event_to_cloud,
            args=(raw_text or "", polished_text or raw_text or ""),
            daemon=True,
        ).start()

    def _write_event_to_cloud(self, raw_text: str, polished_text: str):
        self._ensure_client()
        if self._client is None:
            return
        try:
            # 会話形式のイベント(ユーザー発話=raw, アシスタント出力=polished)
            payload = [
                {"conversational": {"role": "USER",
                                    "content": {"text": raw_text or "(空)"}}},
                {"conversational": {"role": "ASSISTANT",
                                    "content": {"text": polished_text or "(空)"}}},
            ]
            from datetime import datetime
            self._client.create_event(
                memoryId=AGENTCORE_MEMORY_ID,
                actorId=AGENTCORE_ACTOR_ID,
                sessionId=self.session_id,
                eventTimestamp=datetime.utcnow(),
                payload=payload,
            )
        except Exception as e:
            log(f"AgentCore event書き込み失敗: {e}")

    def get_initial_prompt(self) -> str:
        """Whisperに渡す initial_prompt。
        過剰注入は幻聴を招くため、出現回数 VOCAB_MIN_FREQ 以上の語のみ
        上位 VOCAB_TOP_N_HINT 件に絞る。configで無効化可能。"""
        # configで無効化されていれば空文字を返す
        cfg = load_config()
        if not cfg.get("vocab_hint_enabled", True):
            return ""
        with self._lock:
            filtered = [(t, c) for t, c in self.vocab.items() if c >= VOCAB_MIN_FREQ]
            top = sorted(filtered, key=lambda x: -x[1])[:VOCAB_TOP_N_HINT]
        if not top:
            return ""
        terms = "、".join(t for t, _ in top)
        return f"用語: {terms}。"

    def sync_from_cloud(self):
        """起動時にクラウドから過去event取得し、ローカル語彙を再構築。
        全session横断で最近のイベントを取得。"""
        self._ensure_client()
        if self._client is None:
            return
        try:
            # 過去session一覧(最新20件)
            sr = self._client.list_sessions(
                memoryId=AGENTCORE_MEMORY_ID,
                actorId=AGENTCORE_ACTOR_ID,
                maxResults=20,
            )
            count = 0
            for s in sr.get("sessionSummaries", []):
                sid = s.get("sessionId")
                if not sid or sid == self.session_id:
                    continue
                try:
                    er = self._client.list_events(
                        memoryId=AGENTCORE_MEMORY_ID,
                        actorId=AGENTCORE_ACTOR_ID,
                        sessionId=sid,
                        maxResults=50,
                    )
                except Exception:
                    continue
                for ev in er.get("events", []):
                    for blk in ev.get("payload", []):
                        conv = blk.get("conversational")
                        # USER(生音声テキスト)から学習。
                        # ASSISTANT(LLM出力)は汎用語が多くWhisperを誤導するため除外。
                        if conv and conv.get("role") == "USER":
                            txt = conv.get("content", {}).get("text", "")
                            for term in self._extract_terms(txt):
                                self.vocab[term] = self.vocab.get(term, 0) + 1
                                count += 1
            if count:
                self._save_local()
                log(f"クラウドから語彙{count}件取り込み(全session)")
        except Exception as e:
            log(f"AgentCore同期失敗: {e}")


# ============================================================
# グローバルホットキー（録音トグル）
# ============================================================
class HotkeyManager:
    """pynputで指定のキー組み合わせを監視し、押されたらコールバックを呼ぶ。"""

    def __init__(self, hotkey: str, callback):
        self.hotkey = hotkey
        self.callback = callback
        self._listener = None
        self._thread = None

    def start(self):
        if not self.hotkey:
            log("ホットキー: 未設定（無効）")
            return
        try:
            from pynput import keyboard
            def _on_activate():
                try:
                    self.callback()
                except Exception as e:
                    log(f"ホットキー処理エラー: {e}")
            self._listener = keyboard.GlobalHotKeys({self.hotkey: _on_activate})
            self._listener.daemon = True
            self._listener.start()
            log(f"ホットキー登録: {self.hotkey}")
        except Exception as e:
            log(f"ホットキー登録失敗: {e}")
            self._listener = None

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def update(self, new_hotkey: str):
        self.stop()
        self.hotkey = new_hotkey
        self.start()


# ============================================================
# 後処理（LLMで整文・敬語化・翻訳など）
# ============================================================
class Postprocessor:
    """Bedrock Claude Haiku 4.5に整文プロンプトを投げる。失敗時は生テキストを返す。"""

    def __init__(self):
        self._client = None       # 遅延初期化
        self._tried_init = False

    def _ensure(self):
        if self._tried_init:
            return
        self._tried_init = True
        try:
            import boto3
            self._client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
        except Exception as e:
            log(f"Bedrockクライアント初期化失敗: {e}")
            self._client = None

    def apply(self, text: str, mode: str) -> str:
        if not text or mode == "raw":
            return text
        prompt_template = None
        for _, key, p in POSTPROCESS_MODES:
            if key == mode:
                prompt_template = p
                break
        if not prompt_template:
            return text
        self._ensure()
        if self._client is None:
            return text  # クライアント未初期化なら何もせず通す
        try:
            t0 = time.time()
            r = self._client.converse(
                modelId=BEDROCK_MODEL_ID,
                messages=[{"role": "user",
                           "content": [{"text": f"{prompt_template}\n\n{text}"}]}],
                inferenceConfig={"maxTokens": 1000, "temperature": 0.3},
            )
            out = r["output"]["message"]["content"][0]["text"].strip()
            log(f"後処理({mode}) {time.time()-t0:.1f}s: {out[:50]}")
            return out
        except Exception as e:
            log(f"後処理エラー({mode}): {e}")
            return text


# ============================================================
# エージェント（音声→アクション実行 / macOS連携）
# ============================================================
AGENT_TOOLS = [
    {
        "toolSpec": {
            "name": "create_reminder",
            "description": "macOSのリマインダーアプリにリマインダーを作成する。"
                           "「あとで〇〇する」「〇〇するのを忘れないように」など、"
                           "時刻や時間後の通知を伴うタスクを記録する。",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "リマインダーのタイトル"},
                    "minutes_later": {
                        "type": "integer",
                        "description": "今から何分後に通知するか（指定がなければ省略）",
                    },
                },
                "required": ["title"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "create_calendar_event",
            "description": "macOSのカレンダーに予定を追加する。"
                           "日時と予定タイトルが含まれる発話で使う。",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "予定のタイトル"},
                    "start_iso": {
                        "type": "string",
                        "description": "開始日時(ISO 8601、ローカル時刻)。例: '2026-06-15T15:00:00'",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "所要時間(分)。指定がなければ60。",
                    },
                },
                "required": ["title", "start_iso"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "open_url_or_search",
            "description": "URLを開く、またはWeb検索を実行する。"
                           "「〇〇のドキュメントを開いて」「〇〇を検索して」等で使う。",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "開きたいURL。指定があればqueryより優先。",
                    },
                    "query": {
                        "type": "string",
                        "description": "検索クエリ(URLが無い場合)。Google検索で開く。",
                    },
                },
            }},
        }
    },
]


def _osascript(script: str) -> tuple[int, str]:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode, (r.stdout or r.stderr).strip()


def _action_create_reminder(title: str, minutes_later: int = None) -> str:
    if minutes_later:
        script = (
            f'tell application "Reminders" to make new reminder with properties '
            f'{{name:"{title}", remind me date:(current date) + {int(minutes_later)} * minutes}}'
        )
    else:
        script = (
            f'tell application "Reminders" to make new reminder with properties '
            f'{{name:"{title}"}}'
        )
    code, out = _osascript(script)
    if code == 0:
        when = f"{minutes_later}分後" if minutes_later else "(時刻指定なし)"
        return f"リマインダー追加: 「{title}」{when}"
    return f"リマインダー追加失敗: {out}"


def _action_create_calendar_event(title: str, start_iso: str,
                                  duration_minutes: int = 60) -> str:
    # ISO 8601 → AppleScriptの date 形式は環境依存なので、年月日時分秒を分解して組み立てる
    from datetime import datetime, timedelta
    try:
        start = datetime.fromisoformat(start_iso)
    except Exception as e:
        return f"日時解釈失敗({start_iso}): {e}"
    end = start + timedelta(minutes=int(duration_minutes or 60))
    fmt = lambda d: f'date "{d.strftime("%Y/%m/%d %H:%M:%S")}"'
    script = (
        'tell application "Calendar"\n'
        '  set targetCal to first calendar whose writable is true\n'
        '  tell targetCal\n'
        f'    make new event with properties {{summary:"{title}", '
        f'start date:{fmt(start)}, end date:{fmt(end)}}}\n'
        '  end tell\n'
        'end tell'
    )
    code, out = _osascript(script)
    if code == 0:
        return f"予定追加: 「{title}」{start.strftime('%m/%d %H:%M')}〜"
    return f"予定追加失敗: {out}"


def _action_open_url_or_search(url: str = None, query: str = None) -> str:
    import urllib.parse
    if url:
        subprocess.run(["open", url])
        return f"URLを開く: {url}"
    if query:
        target = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        subprocess.run(["open", target])
        return f"Google検索: {query}"
    return "URLもqueryも指定されていません"


AGENT_DISPATCH = {
    "create_reminder": _action_create_reminder,
    "create_calendar_event": _action_create_calendar_event,
    "open_url_or_search": _action_open_url_or_search,
}


# ----------- アプリ起動 -----------
def _action_open_app(app_name: str) -> str:
    r = subprocess.run(["open", "-a", app_name], capture_output=True, text=True)
    if r.returncode == 0:
        return f"アプリ起動: {app_name}"
    return f"アプリ起動失敗({app_name}): {r.stderr.strip()}"


# ----------- メモ追加(Notes.app) -----------
def _action_add_note(title: str, body: str = "") -> str:
    safe_title = title.replace('"', '\\"').replace("\n", " ")
    safe_body = (body or "").replace('"', '\\"').replace("\n", "<br>")
    script = (
        'tell application "Notes"\n'
        f'  set newNote to make new note with properties {{name:"{safe_title}", '
        f'body:"<h1>{safe_title}</h1>{safe_body}"}}\n'
        'end tell'
    )
    code, out = _osascript(script)
    if code == 0:
        return f"メモ追加: 「{title}」"
    return f"メモ追加失敗: {out}"


# ----------- メール下書き(Mail.app) -----------
def _action_compose_email(to: str, subject: str = "", body: str = "") -> str:
    safe_to = to.replace('"', '\\"')
    safe_subject = subject.replace('"', '\\"')
    safe_body = (body or "").replace('"', '\\"')
    script = (
        'tell application "Mail"\n'
        f'  set newMessage to make new outgoing message with properties '
        f'{{subject:"{safe_subject}", content:"{safe_body}", visible:true}}\n'
        '  tell newMessage\n'
        f'    make new to recipient at end of to recipients '
        f'with properties {{address:"{safe_to}"}}\n'
        '  end tell\n'
        '  activate\n'
        'end tell'
    )
    code, out = _osascript(script)
    if code == 0:
        return f"メール下書き: {to} 「{subject or '(無題)'}」"
    return f"メール下書き失敗: {out}"


# ----------- システム操作(音量・明度・ダークモード) -----------
def _action_system_control(action: str, value: int = None) -> str:
    """action: volume_up/volume_down/volume_mute/volume_set/
              brightness_up/brightness_down/
              dark_mode_on/dark_mode_off/dark_mode_toggle"""
    a = (action or "").lower()
    if a == "volume_set" and value is not None:
        v = max(0, min(100, int(value)))
        _osascript(f"set volume output volume {v}")
        return f"音量: {v}%"
    if a == "volume_up":
        _osascript("set volume output volume ((output volume of (get volume settings)) + 10)")
        return "音量を上げました"
    if a == "volume_down":
        _osascript("set volume output volume ((output volume of (get volume settings)) - 10)")
        return "音量を下げました"
    if a == "volume_mute":
        _osascript("set volume with output muted")
        return "ミュートしました"
    if a == "brightness_up":
        # F15キーシミュレートで明度UP(全てのMacで動くわけではないが多くで動作)
        subprocess.run(["osascript", "-e",
                        'tell application "System Events" to key code 144'])
        return "画面を明るくしました"
    if a == "brightness_down":
        subprocess.run(["osascript", "-e",
                        'tell application "System Events" to key code 145'])
        return "画面を暗くしました"
    if a == "dark_mode_on":
        _osascript('tell app "System Events" to tell appearance preferences to set dark mode to true')
        return "ダークモードON"
    if a == "dark_mode_off":
        _osascript('tell app "System Events" to tell appearance preferences to set dark mode to false')
        return "ダークモードOFF"
    if a == "dark_mode_toggle":
        _osascript('tell app "System Events" to tell appearance preferences '
                   'to set dark mode to not dark mode')
        return "ダークモード切替"
    return f"未対応のシステム操作: {action}"


# ----------- 購入承認(Guardrails for AgentCore Payments) -----------
# 音声 → エージェント判定 → ガードレール → ユーザー承認 → 実行(現状は検索URL)
# 本格的なAgentCore Paymentsへの差し替え時は _execute_purchase をprocess_paymentに置換。
PURCHASE_LOG_PATH = Path.home() / ".apexvoice" / "purchases.json"
DEFAULT_GUARDRAILS = {
    "max_amount_per_request": 5000,   # 1回あたり上限(円)
    "max_total_per_day": 20000,       # 1日累計上限(円)
    "require_approval": True,         # 承認ダイアログを出すか
}


def _load_purchase_log() -> list:
    try:
        if PURCHASE_LOG_PATH.exists():
            return json.loads(PURCHASE_LOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_purchase_log(entries: list):
    try:
        PURCHASE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        PURCHASE_LOG_PATH.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        log(f"購入ログ保存失敗: {e}")


def _today_total_amount() -> int:
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    total = 0
    for e in _load_purchase_log():
        if e.get("date", "").startswith(today) and e.get("status") == "approved":
            total += e.get("amount", 0)
    return total


def _show_approval_dialog(title: str, body: str, default: str = "拒否") -> bool:
    """macOSネイティブの確認ダイアログ。承認ボタン押下時のみTrue。"""
    # AppleScriptのダブルクオートを避けるためバックスラッシュエスケープ
    safe_body = body.replace('"', '\\"').replace("\n", "\\n")
    safe_title = title.replace('"', '\\"')
    script = (
        f'tell application "System Events" to display dialog "{safe_body}" '
        f'with title "{safe_title}" '
        f'buttons {{"拒否", "承認"}} default button "{default}" '
        f'with icon caution'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return False
    return "承認" in (r.stdout or "")


# ----------- 承認バックエンド: Aegis (Slack MCP) -----------
# Aegisは stdio MCP サーバとして起動する。初回利用時にspawnし以後は再利用。
# 既定で ~/aegis-slack-app/src/mcp-server.ts を npx tsx で起動。
AEGIS_DEFAULT_DIR = str(Path.home() / "aegis-slack-app")


class AegisApprovalClient:
    """Aegis MCP(stdio)へ request_approval/wait_for_approval を発行するクライアント。
    プロセスはAppex Voice起動中は常駐(初回利用時にspawn)。"""

    def __init__(self):
        self._client = None         # MCPセッション
        self._proc = None           # 子プロセス
        self._lock = threading.Lock()

    def _ensure(self):
        if self._client is not None:
            return
        try:
            cfg = load_config()
            aegis_dir = cfg.get("aegis_dir", AEGIS_DEFAULT_DIR)
            if not (Path(aegis_dir) / "src" / "mcp-server.ts").exists():
                raise RuntimeError(
                    f"Aegis MCPサーバ({aegis_dir}/src/mcp-server.ts)が見つかりません"
                )
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            import anyio

            # 同期APIに統一するため、asyncを別スレッドで動かす
            self._anyio = anyio
            self._ClientSession = ClientSession
            self._stdio_client = stdio_client
            self._server_params = StdioServerParameters(
                command="npx",
                args=["tsx", "src/mcp-server.ts"],
                cwd=aegis_dir,
            )
            # セッション維持のためバックグラウンドループを起動
            self._loop_thread = threading.Thread(
                target=self._run_loop, daemon=True
            )
            self._loop_ready = threading.Event()
            self._request_q = queue.Queue()
            self._loop_thread.start()
            ok = self._loop_ready.wait(timeout=20)
            if not ok or self._client is None:
                raise RuntimeError("Aegis MCPセッション開始がタイムアウト")
        except Exception as e:
            log(f"Aegis接続失敗: {e}")
            self._client = None
            raise

    def _run_loop(self):
        import anyio
        anyio.run(self._async_main)

    async def _async_main(self):
        try:
            async with self._stdio_client(self._server_params) as (read, write):
                async with self._ClientSession(read, write) as session:
                    await session.initialize()
                    self._client = session
                    self._loop_ready.set()
                    log("Aegis MCPセッション開始")
                    # キューから来たリクエストを順次処理
                    while True:
                        item = await anyio.to_thread.run_sync(
                            self._request_q.get
                        )
                        if item is None:
                            break
                        future, kind, args = item
                        try:
                            result = await session.call_tool(kind, args)
                            future.set(("ok", result))
                        except Exception as e:
                            future.set(("err", e))
        except Exception as e:
            log(f"Aegis _async_main エラー: {e}")
            self._loop_ready.set()  # ブロック解除
            self._client = None

    def _call_sync(self, kind: str, args: dict, timeout: float = 70.0):
        class _Future:
            def __init__(self):
                self._ev = threading.Event()
                self._val = None
            def set(self, v):
                self._val = v
                self._ev.set()
            def get(self, t):
                self._ev.wait(t)
                return self._val
        fut = _Future()
        self._request_q.put((fut, kind, args))
        v = fut.get(timeout)
        if v is None:
            raise TimeoutError(f"Aegis {kind} timed out")
        status, payload = v
        if status == "err":
            raise payload
        return payload

    @staticmethod
    def _extract_json(result) -> dict:
        # MCP CallToolResult から JSON テキストを取り出す
        for c in (result.content or []):
            txt = getattr(c, "text", None)
            if txt:
                try:
                    return json.loads(txt)
                except Exception:
                    return {"raw": txt}
        return {}

    def request_approval(self, item: str, max_price_yen: int,
                         store: str, note: str = None) -> bool:
        """Aegis経由で承認を求める。承認ボタン押下時のみTrue。"""
        with self._lock:
            self._ensure()
        # 1) request_approval
        req_args = {
            "agent": "apex-voice",
            "action": "purchase",
            "args": {
                "item": item,
                "max_price_yen": max_price_yen,
                "store": store or "amazon",
                "note": note or "",
            },
            "risk": "high",
            "reason": "音声起点の購入リクエスト",
        }
        r = self._call_sync("request_approval", req_args)
        d = self._extract_json(r)
        request_id = d.get("request_id")
        if not request_id:
            log(f"Aegis request_approval失敗: {d}")
            return False
        if d.get("status") == "approved" and d.get("auto_approved"):
            return True
        # 2) wait_for_approval (最大55秒×繰り返し、合計2分まで)
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                wr = self._call_sync(
                    "wait_for_approval",
                    {"request_id": request_id, "timeout_seconds": 50},
                    timeout=70,
                )
                wd = self._extract_json(wr)
                st = wd.get("status")
                if st == "approved":
                    return True
                if st in ("denied", "expired"):
                    return False
                # info_requested / pending → 続けて待機
            except Exception as e:
                log(f"Aegis wait_for_approval エラー: {e}")
                return False
        log("Aegis 承認待ちタイムアウト(2分)")
        return False


# シングルトン
_AEGIS_CLIENT = AegisApprovalClient()


def _show_approval_via_slack(title: str, body: str,
                              item: str, amount: int, store: str,
                              note: str = None) -> bool:
    try:
        return _AEGIS_CLIENT.request_approval(item, amount, store, note)
    except Exception as e:
        log(f"Slack(Aegis)承認失敗 → ローカルダイアログにフォールバック: {e}")
        return _show_approval_dialog(title, body)


def _execute_purchase(item: str, max_price_yen: int, store: str) -> str:
    """承認後の購入実行。デモではamazon検索を開くのみ。
    本番AgentCore Paymentsならここで process_payment を呼ぶ。"""
    import urllib.parse
    store_lc = (store or "").lower()
    if "rakuten" in store_lc or "楽天" in store_lc:
        url = "https://search.rakuten.co.jp/search/mall/" + urllib.parse.quote(item)
    elif "mercari" in store_lc or "メルカリ" in store_lc:
        url = "https://jp.mercari.com/search?keyword=" + urllib.parse.quote(item)
    else:
        url = ("https://www.amazon.co.jp/s?k=" + urllib.parse.quote(item) +
               f"&rh=p_36:-{max_price_yen}00")  # 上限フィルタ(amazonは銭単位)
    subprocess.run(["open", url])
    return url


def _action_purchase_request(item: str, max_price_yen: int,
                             store: str = None, note: str = None) -> str:
    """音声起点の購入依頼: ガードレール → 承認 → 実行(検索URL表示)。"""
    from datetime import datetime

    # 設定読み込み(configファイル上書きあれば反映)
    cfg = load_config()
    grd = {**DEFAULT_GUARDRAILS, **cfg.get("guardrails", {})}

    # --- Guardrail 1: 1回あたり上限 ---
    if max_price_yen > grd["max_amount_per_request"]:
        msg = (f"❌ ガードレール: 1回あたり上限超過 "
               f"(¥{max_price_yen:,} > ¥{grd['max_amount_per_request']:,})")
        _log_purchase(item, max_price_yen, store, "blocked_per_request_limit")
        return msg

    # --- Guardrail 2: 1日累計上限 ---
    today_total = _today_total_amount()
    if today_total + max_price_yen > grd["max_total_per_day"]:
        msg = (f"❌ ガードレール: 本日累計上限超過 "
               f"(¥{today_total:,} + ¥{max_price_yen:,} > ¥{grd['max_total_per_day']:,})")
        _log_purchase(item, max_price_yen, store, "blocked_daily_limit")
        return msg

    # --- 承認ダイアログ ---
    if grd["require_approval"]:
        body = (f"商品: {item}\n"
                f"上限価格: ¥{max_price_yen:,}\n"
                f"店舗: {store or '指定なし'}\n"
                f"本日累計: ¥{today_total:,} → ¥{today_total + max_price_yen:,}")
        if note:
            body += f"\nメモ: {note}"
        backend = cfg.get("approval_backend", "local")
        if backend == "slack":
            approved = _show_approval_via_slack(
                "購入リクエスト承認", body,
                item, max_price_yen, store, note,
            )
        else:
            approved = _show_approval_dialog("購入リクエスト承認", body)
        if not approved:
            _log_purchase(item, max_price_yen, store, "denied")
            return f"🛑 ユーザーが拒否: {item}"

    # --- 実行 ---
    url = _execute_purchase(item, max_price_yen, store or "amazon")
    _log_purchase(item, max_price_yen, store, "approved", url)
    return f"✅ 承認・購入処理開始: {item} (上限¥{max_price_yen:,}) → {url}"


def _log_purchase(item: str, amount: int, store: str, status: str, url: str = None):
    from datetime import datetime
    entries = _load_purchase_log()
    entries.append({
        "date": datetime.now().isoformat(timespec="seconds"),
        "item": item,
        "amount": amount,
        "store": store,
        "status": status,
        "url": url,
    })
    # 直近100件のみ保持
    if len(entries) > 100:
        entries = entries[-100:]
    _save_purchase_log(entries)


# ----------- Web取得 + Claude要約 -----------
# requests + BeautifulSoup でHTML取得→本文抽出→Bedrockで要約
# (JS必須サイトは AgentCore Browser SDK 経由に拡張可能。v1はシンプル版)
_WEB_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X) ApexVoice/0.2",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}


def _fetch_page_text(url: str, max_chars: int = 8000) -> str:
    import requests
    from bs4 import BeautifulSoup
    r = requests.get(url, headers=_WEB_FETCH_HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    # ノイズ要素を除去
    for tag in soup(["script", "style", "noscript", "header", "footer",
                     "nav", "aside", "form", "iframe"]):
        tag.decompose()
    # 本文っぽい要素を優先抽出
    main = (soup.find("article") or soup.find("main") or soup.body or soup)
    text = " ".join(main.get_text(separator=" ", strip=True).split())
    return text[:max_chars]


def _summarize_with_claude(content: str, question: str = None) -> str:
    import boto3
    client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    if question:
        prompt = (
            f"以下のWebページ本文から、ユーザーの質問に簡潔に答えてください。"
            f"答えのテキストのみを返し、前置きや出典説明は不要です。\n\n"
            f"【質問】{question}\n\n【ページ本文】\n{content}"
        )
    else:
        prompt = (
            "以下のWebページ本文の要点を、日本語で3〜6行の箇条書きにまとめてください。"
            "各行は「・」で始め、要約テキストのみを返してください。\n\n"
            f"【ページ本文】\n{content}"
        )
    r = client.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.2},
    )
    return r["output"]["message"]["content"][0]["text"].strip()


def _action_web_fetch_and_summarize(url: str = None, query: str = None,
                                    question: str = None) -> str:
    """URLまたは検索クエリで取得→要約。質問があればそれに答える形で。"""
    target_url = url
    if not target_url and query:
        # Google検索結果ページから1位リンクを抜く
        import requests, urllib.parse
        from bs4 import BeautifulSoup
        search_url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        try:
            r = requests.get(search_url, headers=_WEB_FETCH_HEADERS, timeout=10)
            soup = BeautifulSoup(r.content, "html.parser")
            # Google検索結果の最初のリンク取得(構造変化に弱いがv1としては可)
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if href.startswith("/url?q="):
                    target_url = urllib.parse.unquote(href.split("/url?q=")[1].split("&")[0])
                    break
                if href.startswith("http") and "google.com" not in href:
                    target_url = href
                    break
        except Exception as e:
            return f"検索失敗: {e}"
    if not target_url:
        return "URLも検索クエリも特定できませんでした"
    try:
        text = _fetch_page_text(target_url)
        if not text:
            return f"本文取得失敗: {target_url}"
        summary = _summarize_with_claude(text, question)
        return summary
    except Exception as e:
        return f"取得・要約エラー({target_url}): {e}"


class Agent:
    """Strands Agents ベースのマルチステップ・エージェント。

    1発話で複数のアクション(例: リマインダー＋カレンダー)を順に実行できる。
    ツール呼び出しが0件の発話は通常のテキストとして扱う。
    """

    def __init__(self):
        self._agent = None
        self._actions_log = []  # 1回のprocessで発火したアクションのメッセージ集
        self._web_result = None  # web_fetch_and_summarize の結果(挿入対象テキスト)

    def _build_tools(self):
        from strands import tool

        actions = self._actions_log

        @tool
        def create_reminder(title: str, minutes_later: int = None) -> str:
            """macOSリマインダーにタスクを追加する。

            Args:
                title: リマインダーのタイトル(必須)
                minutes_later: 今から何分後に通知するか(任意。指定なければ通知時刻なし)
            """
            msg = _action_create_reminder(title, minutes_later)
            actions.append(msg)
            return msg

        @tool
        def create_calendar_event(title: str, start_iso: str,
                                  duration_minutes: int = 60) -> str:
            """macOSカレンダーに予定を追加する。

            Args:
                title: 予定のタイトル
                start_iso: 開始日時のISO 8601(ローカル時刻)。例: 2026-06-15T15:00:00
                duration_minutes: 所要時間(分)。デフォルト60。
            """
            msg = _action_create_calendar_event(title, start_iso, duration_minutes)
            actions.append(msg)
            return msg

        @tool
        def open_url_or_search(url: str = None, query: str = None) -> str:
            """ブラウザでURLを開く、またはGoogle検索を実行する。

            Args:
                url: 開きたいURL(指定があればqueryより優先)
                query: 検索クエリ(URLがない場合)
            """
            msg = _action_open_url_or_search(url, query)
            actions.append(msg)
            return msg

        @tool
        def open_app(app_name: str) -> str:
            """macOS上の任意のアプリを起動する。「〇〇を開いて」「〇〇起動」等で使う。

            Args:
                app_name: アプリ名(例: Slack, Notion, Safari, Mail, Visual Studio Code)
            """
            msg = _action_open_app(app_name)
            actions.append(msg)
            return msg

        @tool
        def add_note(title: str, body: str = "") -> str:
            """macOSのメモ(Notes)アプリにメモを追加する。「メモして」「アイデア記録」等で使う。

            Args:
                title: メモのタイトル(または短い件名)
                body: 本文(任意)
            """
            msg = _action_add_note(title, body)
            actions.append(msg)
            return msg

        @tool
        def compose_email(to: str, subject: str = "", body: str = "") -> str:
            """Mail.appに新規メールの下書きを作成して表示する(送信はしない、ユーザーが手動送信)。

            Args:
                to: 宛先メールアドレス
                subject: 件名
                body: 本文(改行は自然に含めてOK)
            """
            msg = _action_compose_email(to, subject, body)
            actions.append(msg)
            return msg

        @tool
        def system_control(action: str, value: int = None) -> str:
            """システム操作(音量・明度・ダークモード)を実行する。

            Args:
                action: volume_up / volume_down / volume_mute / volume_set
                       / brightness_up / brightness_down
                       / dark_mode_on / dark_mode_off / dark_mode_toggle
                value: volume_set 時の音量(0-100、任意)
            """
            msg = _action_system_control(action, value)
            actions.append(msg)
            return msg

        @tool
        def purchase_request(item: str, max_price_yen: int,
                             store: str = None, note: str = None) -> str:
            """購入リクエストを起こす(ガードレール+ユーザー承認必須)。
            「Amazonで〇〇を1000円までで買って」「楽天で△△注文」等で使う。

            Args:
                item: 商品名(必須)
                max_price_yen: 上限価格(円、必須)
                store: 購入元(amazon/rakuten/mercari等、任意)
                note: 補足メモ(任意)
            """
            msg = _action_purchase_request(item, max_price_yen, store, note)
            actions.append(msg)
            return msg

        @tool
        def web_fetch_and_summarize(url: str = None, query: str = None,
                                    question: str = None) -> str:
            """Webページの内容を取得し、Claudeで要約または質問に答える。
            「〇〇調べて」「〇〇について教えて」「〇〇要約して」等で使う。

            Args:
                url: 取得したいURL(直接指定する場合)
                query: 検索クエリ(URLが分からない場合、Google検索1位を取得)
                question: ページ内容から答えてほしい質問(任意。なければ要約)
            """
            result = _action_web_fetch_and_summarize(url, query, question)
            # 要約結果は ユーザーへのテキスト挿入対象なので actions ではなく
            # 「テキスト」として返したい。挿入したい本文を返却用変数に渡す。
            self._web_result = result
            return result

        return [create_reminder, create_calendar_event,
                open_url_or_search, web_fetch_and_summarize,
                open_app, add_note, compose_email, system_control,
                purchase_request]

    def _ensure(self):
        if self._agent is not None:
            return
        try:
            from strands import Agent as StrandsAgent
            from strands.models import BedrockModel
            from datetime import datetime
            now = datetime.now()
            system_prompt = (
                "あなたは音声入力アシスタントです。ユーザー発話を分析し、"
                "次の操作の明確な依頼が含まれていればツールを順に呼び出す:\n"
                " - リマインダー追加: create_reminder\n"
                " - カレンダー予定作成: create_calendar_event\n"
                " - URL/検索をブラウザで開く: open_url_or_search\n"
                " - Web内容を要約 or 質問に回答: web_fetch_and_summarize\n"
                "    └「〇〇調べて」「〇〇について教えて」「〇〇要約して」等\n"
                " - アプリ起動: open_app\n"
                "    └「Slackを開いて」「Notion起動して」「Safariを立ち上げて」等\n"
                " - メモ追加: add_note\n"
                "    └「メモして〇〇」「アイデア記録: 〇〇」「Notesに〇〇って残して」等\n"
                " - メール下書き作成: compose_email\n"
                "    └「〇〇さんにメール下書きして」(宛先メアド要)\n"
                " - システム操作: system_control\n"
                "    └「音量上げて/下げて/ミュート」「画面明るく/暗く」"
                "「ダークモードに/解除」等\n"
                " - 購入リクエスト(承認必須): purchase_request\n"
                "    └「〇〇買って」「Amazonで〇〇を〇〇円まで」等。"
                "必ず max_price_yen を明確化する\n\n"
                "1発話に複数の依頼があれば、必要なツールを全て順番に呼ぶ。"
                "操作の依頼が全くない発話には、ツールを呼ばずに空応答を返す。"
                "会話的な返答・要約・翻訳・絵文字は禁止。\n\n"
                f"現在日時: {now.strftime('%Y-%m-%d %H:%M (%a)')}\n"
                "曖昧な相対時刻(明日の朝→翌09:00、今夜→当日21:00 等)はこれを基準に解釈する。"
            )
            model = BedrockModel(
                model_id=BEDROCK_MODEL_ID,
                region_name=BEDROCK_REGION,
                temperature=0.0,
            )
            self._agent = StrandsAgent(
                model=model,
                tools=self._build_tools(),
                system_prompt=system_prompt,
            )
        except Exception as e:
            log(f"Strands Agent初期化失敗: {e}")
            self._agent = None

    def process(self, text: str) -> dict:
        """戻り値: {'kind': 'action', 'message': '...'} または {'kind': 'text', 'value': '...'}"""
        self._ensure()
        if self._agent is None:
            return {"kind": "text", "value": text}
        try:
            t0 = time.time()
            self._actions_log.clear()
            self._web_result = None
            result = self._agent(text)
            elapsed = time.time() - t0
            actions = list(self._actions_log)
            web_result = self._web_result
            log(f"Strands Agent {elapsed:.1f}s actions={len(actions)} web={bool(web_result)}")
            # Web取得・要約が走った場合 → 要約結果をテキストとして挿入
            if web_result:
                return {"kind": "text", "value": web_result}
            if actions:
                for a in actions:
                    log(f"  → {a}")
                return {"kind": "action", "message": " / ".join(actions)}
            # ツール未発火 → 元の音声認識テキストをそのまま挿入
            return {"kind": "text", "value": text}
        except Exception as e:
            log(f"Strands Agentエラー: {e}")
            return {"kind": "text", "value": text}


# ============================================================
# 文字起こし（mlx-whisper）
# ============================================================
class Transcriber:
    def __init__(self, model=MODEL, language=LANGUAGE, prompt=PROMPT, memory=None):
        self.model = model
        self.language = language
        self.prompt = prompt
        self.memory = memory  # MemoryManager(任意)。語彙ヒント自動付与
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
        # ユーザー定義プロンプト + 学習語彙ヒントを結合してinitial_promptに
        prompt_parts = []
        if self.prompt:
            prompt_parts.append(self.prompt)
        if self.memory is not None:
            mp = self.memory.get_initial_prompt()
            if mp:
                prompt_parts.append(mp)
        if prompt_parts:
            kwargs["initial_prompt"] = " ".join(prompt_parts)
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

    def start(self, device=None):
        # device指定があればそれを使い、なければ resolve_device()→OS既定
        if device is None:
            device = resolve_device()
        try:
            shown = device if device is not None else sd.default.device[0]
            name = sd.query_devices(shown)["name"]
        except Exception:
            name = "(デフォルト)"
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            blocksize=BLOCK, device=device, callback=self._callback,
        )
        self.stream.start()
        self._current_device = device
        log(f"マイク入力ストリーム開始: [{device if device is not None else 'OS既定'}] {name}")

    def switch_device(self, device):
        """録音中でも安全に入力デバイスを切替える。"""
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as e:
                log(f"既存ストリーム停止失敗: {e}")
            self.stream = None
        # 内部状態リセット
        self._speaking = False
        self._buf = []
        self._silence_blocks = 0
        self._hot_blocks = 0
        self._idle_blocks = 0
        self.start(device=device)

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


# ============================================================
# メニューバー常駐アプリ
# ============================================================
ICON_IDLE = "🎤"     # 待機中（クリックで録音開始）
ICON_REC = "🔴"      # 録音中（クリックで停止）
ICON_WORK = "✍️"     # 認識処理中


class ApexVoiceApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_IDLE, quit_button=None)

        # 設定読み込み: configファイルに保存された言語があれば反映（環境変数指定がない場合のみ）
        self.config = load_config()
        initial_lang = LANGUAGE
        if "VOICETYPE_LANG" not in os.environ and "language" in self.config:
            initial_lang = self.config["language"]

        # 後処理モードもconfigから復元
        self.postprocess_mode = self.config.get("postprocess", "raw")
        if not any(k == self.postprocess_mode for _, k, _ in POSTPROCESS_MODES):
            self.postprocess_mode = "raw"

        # 語彙メモリ(AgentCore Memory + ローカルキャッシュ)
        self.memory = MemoryManager()
        threading.Thread(target=self.memory.sync_from_cloud, daemon=True).start()

        self.transcriber = Transcriber(language=initial_lang, memory=self.memory)
        self.postprocessor = Postprocessor()
        self.agent = Agent()
        self.inserter = Inserter()
        self.recorder = Recorder(on_segment=self._enqueue)
        self.jobs = queue.Queue()

        self.item_toggle = rumps.MenuItem("🎤 録音開始", callback=self.toggle)
        self.item_status = rumps.MenuItem("状態: 停止中", callback=None)

        # 言語サブメニュー
        self.lang_items = {}                  # code -> MenuItem
        lang_menu = rumps.MenuItem(self._lang_menu_title(initial_lang))
        self.item_lang_menu = lang_menu
        for label, code in LANGUAGES:
            item = rumps.MenuItem(label, callback=self._make_lang_callback(code))
            if code == initial_lang:
                item.state = 1                # チェックマーク
            self.lang_items[code] = item
            lang_menu.add(item)

        # 後処理サブメニュー
        self.pp_items = {}
        pp_menu = rumps.MenuItem(self._pp_menu_title(self.postprocess_mode))
        self.item_pp_menu = pp_menu
        for label, key, _ in POSTPROCESS_MODES:
            item = rumps.MenuItem(label, callback=self._make_pp_callback(key))
            if key == self.postprocess_mode:
                item.state = 1
            self.pp_items[key] = item
            pp_menu.add(item)

        # 語彙ヒント注入トグル(過剰注入で幻聴が出ることがあるため切れるように)
        self.item_vocab_toggle = rumps.MenuItem(
            self._vocab_hint_label(),
            callback=self.toggle_vocab_hint,
        )

        # ホットキー表示メニュー（クリックで変更ダイアログ）
        self.item_hotkey = rumps.MenuItem(
            self._hotkey_label(self.config.get("hotkey", DEFAULT_HOTKEY)),
            callback=self.change_hotkey,
        )

        # 購入承認設定サブメニュー
        purchase_menu = rumps.MenuItem("購入承認設定")
        purchase_menu.add(rumps.MenuItem(
            self._guardrail_per_request_label(),
            callback=self.change_per_request_limit,
        ))
        purchase_menu.add(rumps.MenuItem(
            self._guardrail_daily_label(),
            callback=self.change_daily_limit,
        ))
        purchase_menu.add(rumps.MenuItem(
            self._approval_mode_label(),
            callback=self.toggle_approval_mode,
        ))
        purchase_menu.add(rumps.MenuItem(
            self._approval_backend_label(),
            callback=self.toggle_approval_backend,
        ))
        purchase_menu.add(rumps.MenuItem(
            "履歴を見る…",
            callback=self.show_purchase_history,
        ))
        self.item_purchase_menu = purchase_menu

        self.menu = [
            self.item_toggle,
            self.item_status,
            None,
            lang_menu,
            pp_menu,
            self.item_hotkey,
            purchase_menu,
            self.item_vocab_toggle,
            None,
            rumps.MenuItem("感度を上げる (拾いやすく)", callback=self.sens_up),
            rumps.MenuItem("感度を下げる (拾いにくく)", callback=self.sens_down),
            None,
            rumps.MenuItem("マイクを再取得 (OS既定を読み直す)", callback=self.refresh_mic),
            rumps.MenuItem("学習語彙をリセット", callback=self.reset_vocab),
            rumps.MenuItem("再起動 (Apex Voice)", callback=self.restart_app),
            None,
            rumps.MenuItem("終了", callback=self.quit_app),
        ]

        # 録音とワーカー(デバッグ用に環境変数で完全無効化可能)
        if not os.environ.get("APEXVOICE_NO_AUDIO"):
            self.recorder.start(device=None)
            threading.Thread(target=self._worker, daemon=True).start()
        else:
            log("[DEBUG] APEXVOICE_NO_AUDIO=1: 録音無効モード")

        # グローバルホットキー(同上)
        if not os.environ.get("APEXVOICE_NO_HOTKEY"):
            self.hotkey = self.config.get("hotkey", DEFAULT_HOTKEY)
            self.hotkey_mgr = HotkeyManager(self.hotkey, lambda: self.toggle(None))
            self.hotkey_mgr.start()
        else:
            log("[DEBUG] APEXVOICE_NO_HOTKEY=1: ホットキー無効モード")
            self.hotkey_mgr = HotkeyManager("", lambda: None)

    # ---- 自動停止のUI同期 ----
    # 注: アイコンタイトルは触らない(メニューを閉じる)。
    # 自動停止時はrecorderが内部でlisteningをFalseにするので、
    # 次回ユーザーがクリックした時に正しい状態が見える。

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

    def _lang_label(self, code):
        for label, c in LANGUAGES:
            if c == code:
                return label
        return code or "自動判定"

    def _lang_menu_title(self, code):
        return f"言語: {self._lang_label(code)}"

    def _make_lang_callback(self, code):
        def cb(_):
            for c, item in self.lang_items.items():
                item.state = 1 if c == code else 0
            self.transcriber.language = code
            self.item_lang_menu.title = self._lang_menu_title(code)
            self.config["language"] = code
            save_config(self.config)
            log(f"言語切替: {self._lang_label(code)} ({code or 'auto'})")
        return cb

    def _pp_label(self, key):
        for label, k, _ in POSTPROCESS_MODES:
            if k == key:
                return label
        return key

    def _pp_menu_title(self, key):
        # 長いラベルは縮めて表示
        short = self._pp_label(key).split("（")[0]
        return f"後処理: {short}"

    def _hotkey_label(self, hotkey: str) -> str:
        # pynput形式("<ctrl>+<alt>+v")を見やすい表示に変換
        if not hotkey:
            return "ホットキー: 無効"
        nice = (hotkey.replace("<cmd>", "⌘").replace("<ctrl>", "⌃")
                      .replace("<alt>", "⌥").replace("<shift>", "⇧")
                      .replace("<space>", "Space").replace("+", "")
                      .replace("<", "").replace(">", "").upper())
        return f"ホットキー: {nice}"

    def change_hotkey(self, _):
        win = rumps.Window(
            message=(
                "pynput形式で入力。例:\n"
                "  <ctrl>+<alt>+v  (⌃⌥V)\n"
                "  <cmd>+<shift>+<space>  (⌘⇧Space)\n"
                "  <f5>\n"
                "空欄でホットキー無効化"
            ),
            title="ホットキーを変更",
            default_text=self.config.get("hotkey", DEFAULT_HOTKEY),
            ok="変更",
            cancel="キャンセル",
        )
        res = win.run()
        if not res.clicked:
            return
        new_key = (res.text or "").strip()
        self.hotkey = new_key
        self.config["hotkey"] = new_key
        save_config(self.config)
        self.hotkey_mgr.update(new_key)
        self.item_hotkey.title = self._hotkey_label(new_key)
        rumps.notification("Apex Voice", "ホットキー", self._hotkey_label(new_key))

    # --- マイク再取得 / アプリ再起動 ---
    def refresh_mic(self, _):
        """OS既定マイクを読み直す。システム設定でマイクを切替えた後に使う。"""
        try:
            self.recorder.switch_device(None)
            try:
                name = sd.query_devices(sd.default.device[0]).get("name", "?")
            except Exception:
                name = "(OS既定)"
            rumps.notification("Apex Voice", "マイク再取得", f"現在: {name}")
            log(f"OS既定マイクを読み直し: {name}")
        except Exception as e:
            log(f"マイク再取得失敗: {e}")
            rumps.notification("Apex Voice", "マイク再取得失敗", str(e))

    def restart_app(self, _):
        """Apex Voice 自身を再起動する。"""
        try:
            self.hotkey_mgr.stop()
        except Exception:
            pass
        try:
            self.recorder.stop()
        except Exception:
            pass
        # 同じプロセスとしてexec(設定や環境を引継ぎ)
        log("Apex Voiceを再起動します")
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _vocab_hint_label(self):
        on = self.config.get("vocab_hint_enabled", True)
        return f"語彙ヒント注入: {'ON' if on else 'OFF'}"

    def toggle_vocab_hint(self, _):
        cur = self.config.get("vocab_hint_enabled", True)
        self.config["vocab_hint_enabled"] = not cur
        save_config(self.config)
        self.item_vocab_toggle.title = self._vocab_hint_label()
        state = "OFF" if cur else "ON"
        rumps.notification(
            "Apex Voice", "語彙ヒント注入",
            f"切替: {state}\n(OFFにすると幻聴が減ることがあります)"
        )

    def reset_vocab(self, _):
        w = rumps.Window(
            message="学習した語彙(ローカルキャッシュ)を全削除します。\n"
                    "クラウド(AgentCore Memory)の履歴は残り、次回起動時に再構築されます。\n"
                    "本当に削除しますか?",
            title="学習語彙のリセット",
            default_text="",
            ok="削除する", cancel="キャンセル",
        )
        r = w.run()
        if not r.clicked:
            return
        try:
            self.memory.vocab.clear()
            self.memory._save_local()
            rumps.notification("Apex Voice", "語彙リセット", "ローカル語彙を削除しました")
            log("ローカル語彙をリセット")
        except Exception as e:
            log(f"語彙リセット失敗: {e}")

    # --- 購入承認関連 ---
    def _current_guardrails(self):
        cfg = load_config()
        return {**DEFAULT_GUARDRAILS, **cfg.get("guardrails", {})}

    def _guardrail_per_request_label(self):
        g = self._current_guardrails()
        return f"1回上限: ¥{g['max_amount_per_request']:,}"

    def _guardrail_daily_label(self):
        g = self._current_guardrails()
        return f"1日累計上限: ¥{g['max_total_per_day']:,}"

    def _approval_mode_label(self):
        g = self._current_guardrails()
        return "承認ダイアログ: " + ("ON" if g["require_approval"] else "OFF")

    def _approval_backend_label(self):
        cfg = load_config()
        backend = cfg.get("approval_backend", "local")
        nice = "ローカル(macOS)" if backend == "local" else "Slack(Aegis)"
        return f"承認方式: {nice}"

    def _ask_int(self, title: str, prompt: str, current: int) -> int:
        w = rumps.Window(
            message=prompt, title=title,
            default_text=str(current),
            ok="変更", cancel="キャンセル",
        )
        r = w.run()
        if not r.clicked:
            return None
        try:
            v = int(r.text.replace(",", "").replace("¥", "").strip())
            if v < 0:
                return None
            return v
        except ValueError:
            rumps.notification("Apex Voice", "入力エラー", "整数で入力してください")
            return None

    def _refresh_purchase_menu_labels(self):
        # サブメニュー項目を作り直すのは面倒なので、各titleを書き換える
        items = list(self.item_purchase_menu.values())
        if len(items) >= 4:
            items[0].title = self._guardrail_per_request_label()
            items[1].title = self._guardrail_daily_label()
            items[2].title = self._approval_mode_label()
            items[3].title = self._approval_backend_label()

    def change_per_request_limit(self, _):
        g = self._current_guardrails()
        v = self._ask_int("1回あたり上限変更",
                          "1回の購入で承認できる最大金額(円)を入力",
                          g["max_amount_per_request"])
        if v is None:
            return
        cfg = load_config()
        cfg.setdefault("guardrails", {})["max_amount_per_request"] = v
        save_config(cfg)
        self._refresh_purchase_menu_labels()
        rumps.notification("Apex Voice", "ガードレール更新", f"1回上限: ¥{v:,}")

    def change_daily_limit(self, _):
        g = self._current_guardrails()
        v = self._ask_int("1日累計上限変更",
                          "1日に承認できる合計金額(円)を入力",
                          g["max_total_per_day"])
        if v is None:
            return
        cfg = load_config()
        cfg.setdefault("guardrails", {})["max_total_per_day"] = v
        save_config(cfg)
        self._refresh_purchase_menu_labels()
        rumps.notification("Apex Voice", "ガードレール更新", f"1日累計上限: ¥{v:,}")

    def toggle_approval_mode(self, _):
        cfg = load_config()
        g = cfg.setdefault("guardrails", {})
        cur = g.get("require_approval", DEFAULT_GUARDRAILS["require_approval"])
        g["require_approval"] = not cur
        save_config(cfg)
        self._refresh_purchase_menu_labels()
        state = "ON" if g["require_approval"] else "OFF"
        rumps.notification("Apex Voice", "承認ダイアログ", f"切替: {state}")

    def toggle_approval_backend(self, _):
        cfg = load_config()
        cur = cfg.get("approval_backend", "local")
        new = "slack" if cur == "local" else "local"
        cfg["approval_backend"] = new
        save_config(cfg)
        self._refresh_purchase_menu_labels()
        nice = "Slack(Aegis)" if new == "slack" else "ローカル(macOS)"
        rumps.notification("Apex Voice", "承認方式切替", nice)

    def show_purchase_history(self, _):
        entries = _load_purchase_log()
        if not entries:
            rumps.alert(title="購入履歴", message="まだ履歴がありません")
            return
        # 直近20件を表示用に整形
        recent = entries[-20:][::-1]
        lines = []
        emoji = {
            "approved": "✅", "denied": "🛑",
            "blocked_per_request_limit": "⛔",
            "blocked_daily_limit": "⛔",
        }
        for e in recent:
            mark = emoji.get(e.get("status", ""), "·")
            date = e.get("date", "")[:16].replace("T", " ")
            item = e.get("item", "?")
            amount = e.get("amount", 0)
            store = e.get("store") or "-"
            lines.append(f"{mark} {date}  ¥{amount:,}  {item} [{store}]")
        text = "\n".join(lines)
        # rumps.Windowで表示(コピー可能)
        w = rumps.Window(
            message=text, title=f"購入履歴 (直近{len(recent)}件)",
            default_text="", ok="閉じる", cancel=None,
            dimensions=(600, 320),
        )
        w.run()

    def _make_pp_callback(self, key):
        def cb(_):
            for k, item in self.pp_items.items():
                item.state = 1 if k == key else 0
            self.postprocess_mode = key
            self.item_pp_menu.title = self._pp_menu_title(key)
            self.config["postprocess"] = key
            save_config(self.config)
            log(f"後処理切替: {self._pp_label(key)}")
        return cb

    def sens_up(self, _):
        global SENSITIVITY
        SENSITIVITY = max(1.2, SENSITIVITY - 0.3)
        rumps.notification("Apex Voice", "感度", f"感度: {SENSITIVITY:.1f}（小さいほど拾いやすい）")

    def sens_down(self, _):
        global SENSITIVITY
        SENSITIVITY = min(6.0, SENSITIVITY + 0.3)
        rumps.notification("Apex Voice", "感度", f"感度: {SENSITIVITY:.1f}（大きいほど拾いにくい）")

    def quit_app(self, _):
        try:
            self.hotkey_mgr.stop()
        except Exception:
            pass
        self.recorder.stop()
        rumps.quit_application()

    # ---- 認識ワーカー ----
    def _enqueue(self, audio):
        self.jobs.put(audio)

    def _worker(self):
        while True:
            audio = self.jobs.get()
            try:
                # メニュー項目への書き込みは一切行わない(背景ノイズで頻繁に走ると
                # メニューバーを開いた瞬間に閉じてしまうため)。
                # 状態はログにのみ出す。
                log("認識処理開始")
                text = self.transcriber.transcribe(audio)
                if text and looks_like_hallucination(text):
                    log(f"幻聴として破棄: {text[:30]}")
                elif text:
                    log(f"認識: {text}")
                    raw_text = text
                    final_text = text
                    if self.postprocess_mode == "agent":
                        result = self.agent.process(text)
                        if result["kind"] == "action":
                            rumps.notification(
                                "Apex Voice", "アクション実行", result["message"]
                            )
                            # アクション時は語彙学習しない
                            final_text = None
                        else:
                            final_text = result["value"]
                            self.inserter.insert(
                                final_text,
                                on_perm_error=self._warn_accessibility,
                            )
                    else:
                        if self.postprocess_mode != "raw":
                            final_text = self.postprocessor.apply(
                                text, self.postprocess_mode
                            )
                        self.inserter.insert(
                            final_text, on_perm_error=self._warn_accessibility
                        )
                    # 語彙学習は「ユーザーが実際に発話したRAWテキスト」のみを対象に。
                    # LLM後処理結果・Web要約・整文後等を学習すると汎用語が増えて
                    # Whisperを誤導(幻聴)するため記録しない。
                    if raw_text:
                        self.memory.record(raw_text, raw_text)
            except Exception as e:
                log(f"認識エラー: {e}")

    def _warn_accessibility(self):
        rumps.notification(
            "Apex Voice", "アクセシビリティ許可が必要",
            "システム設定 > プライバシーとセキュリティ > アクセシビリティ で許可してください",
        )
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])


if __name__ == "__main__":
    ApexVoiceApp().run()
