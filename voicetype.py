#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WhisType - macOS 常駐の音声タイピングツール

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
CONFIG_PATH = Path.home() / ".whistype" / "config.json"

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
    "WHISTYPE_BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_REGION = os.environ.get("WHISTYPE_BEDROCK_REGION", "us-east-1")

# AgentCore Memory: ユーザー語彙の永続化先
AGENTCORE_MEMORY_ID = os.environ.get(
    "WHISTYPE_MEMORY_ID", "whistype_personal_vocabulary-YrdZy493pf"
)
AGENTCORE_ACTOR_ID = os.environ.get("WHISTYPE_ACTOR_ID", "default-user")

# ローカル語彙ファイル
VOCAB_PATH = Path.home() / ".whistype" / "vocabulary.json"
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


class MemoryManager:
    """ユーザー語彙(固有名詞・専門用語・表記嗜好)を蓄積し、Whisperにヒントとして注入する。

    - ローカル: ~/.whistype/vocabulary.json に語の頻度を保存(即座に効く)
    - クラウド: AgentCore Memory に event を非同期書き込み(他端末同期・永続化)
    """

    def __init__(self):
        self.vocab = {}                      # term -> freq
        self.session_id = f"whistype-{int(time.time())}"
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
        """Whisperに渡す initial_prompt: 上位N語の固有名詞・専門用語を列挙。
        句読点ヒント付きの定型文も先頭に置いて句読点が付きやすくする。"""
        with self._lock:
            top = sorted(self.vocab.items(), key=lambda x: -x[1])[:VOCAB_TOP_N]
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
                        if conv and conv.get("role") == "ASSISTANT":
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


# ----------- Web取得 + Claude要約 -----------
# requests + BeautifulSoup でHTML取得→本文抽出→Bedrockで要約
# (JS必須サイトは AgentCore Browser SDK 経由に拡張可能。v1はシンプル版)
_WEB_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X) WhisType/0.2",
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
                open_url_or_search, web_fetch_and_summarize]

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
                "次の4操作の明確な依頼が含まれていればツールを順に呼び出す:\n"
                " - リマインダー追加: create_reminder\n"
                " - カレンダー予定作成: create_calendar_event\n"
                " - URL/検索をブラウザで開く: open_url_or_search\n"
                " - Web内容を要約 or 質問に回答(本文取得して回答):"
                " web_fetch_and_summarize\n"
                "    └「〇〇調べて」「〇〇について教えて」「〇〇要約して」「〇〇は?」等\n\n"
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


# ============================================================
# メニューバー常駐アプリ
# ============================================================
ICON_IDLE = "🎤"     # 待機中（クリックで録音開始）
ICON_REC = "🔴"      # 録音中（クリックで停止）
ICON_WORK = "✍️"     # 認識処理中


class WhisTypeApp(rumps.App):
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

        # ホットキー表示メニュー（クリックで変更ダイアログ）
        self.item_hotkey = rumps.MenuItem(
            self._hotkey_label(self.config.get("hotkey", DEFAULT_HOTKEY)),
            callback=self.change_hotkey,
        )

        self.menu = [
            self.item_toggle,
            self.item_status,
            None,
            lang_menu,
            pp_menu,
            self.item_hotkey,
            None,
            rumps.MenuItem("感度を上げる (拾いやすく)", callback=self.sens_up),
            rumps.MenuItem("感度を下げる (拾いにくく)", callback=self.sens_down),
            None,
            rumps.MenuItem("終了", callback=self.quit_app),
        ]

        # 録音とワーカーを開始（録音は停止状態でスタート。メニューの「録音開始」で開始する）
        self.recorder.start()
        threading.Thread(target=self._worker, daemon=True).start()

        # グローバルホットキー
        self.hotkey = self.config.get("hotkey", DEFAULT_HOTKEY)
        self.hotkey_mgr = HotkeyManager(self.hotkey, lambda: self.toggle(None))
        self.hotkey_mgr.start()

    # ---- 自動停止のUI同期 ----
    @rumps.timer(1)
    def _sync_state(self, _):
        if not self.recorder.listening and self.title == ICON_REC:
            self.title = ICON_IDLE
            self.item_toggle.title = "🎤 録音開始"
            self.item_status.title = "状態: 停止中(自動)"

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
        rumps.notification("WhisType", "ホットキー", self._hotkey_label(new_key))

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
        rumps.notification("WhisType", "感度", f"感度: {SENSITIVITY:.1f}（小さいほど拾いやすい）")

    def sens_down(self, _):
        global SENSITIVITY
        SENSITIVITY = min(6.0, SENSITIVITY + 0.3)
        rumps.notification("WhisType", "感度", f"感度: {SENSITIVITY:.1f}（大きいほど拾いにくい）")

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
                self.title = ICON_WORK
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
                                "WhisType", "アクション実行", result["message"]
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
                    # 挿入したテキストを語彙学習へ
                    if final_text:
                        self.memory.record(raw_text, final_text)
            except Exception as e:
                log(f"認識エラー: {e}")
            finally:
                self.title = ICON_REC if self.recorder.listening else ICON_IDLE

    def _warn_accessibility(self):
        rumps.notification(
            "WhisType", "アクセシビリティ許可が必要",
            "システム設定 > プライバシーとセキュリティ > アクセシビリティ で許可してください",
        )
        subprocess.run([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])


if __name__ == "__main__":
    WhisTypeApp().run()
