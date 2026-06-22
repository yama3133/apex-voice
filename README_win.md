# Apex Voice for Windows

[日本語(Win)](README_win.md) | [日本語(macOS)](README.md) | [English](README_en.md)

Windows向け **音声タイピング＋AIエージェント**。マイクに話すと、ローカルfaster-whisperで文字に起こし、整文・敬語化・翻訳までこなして、**いま開いているアプリのカーソル位置に挿入**します。macOS版（mlx-whisper / rumps / LaunchAgent）と機能パリティを目指したWindowsポート。

- **音声認識**: ローカルfaster-whisper (CPU/int8, 既定 `large-v3-turbo`)
- **AI後処理**: Amazon Bedrock Claude Haiku 4.5 で整文・敬語・英訳・箇条書き
- **タスクトレイ常駐**: pystray
- **テキスト挿入**: pyperclip + pynput (Ctrl+V) + pywin32 でフォアグラウンド復元
- **グローバルホットキー**: 既定 `<ctrl>+<alt>+r`（AutoHotkeyで Caps Lock ワンキー化を推奨）
- **幻聴対策**: Silero VAD + STRONG/WEAK フレーズフィルタ
- **自動起動**: タスクスケジューラ（ログイン時起動・管理者権限）

## 動作環境

- Windows 11（x64 推奨。ARM64でも動作確認済みだが faster-whisper のCPU処理は重い）
- Python 3.12 / 3.13
- マイク
- 後処理を使う場合は AWS 認証

> **UTM (Apple Silicon Mac上の仮想Windows ARM64) で動かす場合の注意:**
> - faster-whisper の CPU 処理が実機より大幅に遅い
> - Caps Lock が UTM の仕様で Windows 側に届かないことがあり、AutoHotkey 経由のリマップが効かない場合がある（代替: `<ctrl>+<alt>+r` を直接押す）
> - Mac のスクショショートカット (Cmd+Shift+数字) が UTM 経由で Windows の「Win+Shift+数字＝タスクバー N 番目のアプリ起動」と解釈されることがある

## セットアップ

### 1. リポジトリ取得

```cmd
cd %USERPROFILE%
curl -L -o voicetype_win.py https://raw.githubusercontent.com/yama3133/apex-voice/main/voicetype_win.py
```

### 2. 依存パッケージ

```cmd
pip install faster-whisper silero-vad torch numpy pyaudio pyperclip pynput pystray Pillow pywin32 boto3 botocore setproctitle
pip install "botocore[crt]"
```

`botocore[crt]` は `aws login` で生成された認証情報を boto3 が読むのに必要。

### 3. AWS 認証（後処理を使う場合のみ）

AWS CLI v2.32.0 以降をインストール:
```cmd
winget install --source winget Amazon.AWSCLI
```

新しいコマンドプロンプトを開いてサインイン:
```cmd
aws login
aws sts get-caller-identity
```

### 4. config.json 作成

```cmd
mkdir %USERPROFILE%\.apexvoice
notepad %USERPROFILE%\.apexvoice\config.json
```

中身:
```json
{
  "hotkey": "<ctrl>+<alt>+r",
  "postprocess": "raw"
}
```

### 5. 起動

```cmd
python -u voicetype_win.py 2>"%USERPROFILE%\apexvoice_err.txt"
```

`Silero VAD ロード完了` が出たらタスクトレイにマイクアイコンが出る。

> **注意:** `2>err.txt` で stderr を分離しないと、pystray/pyaudio の警告で起動表示が止まって見えることがある。

## ホットキー（Caps Lock ワンキー化）

物理 F19 が無いキーボードでは AutoHotkey 経由で Caps Lock → Ctrl+Alt+R にリマップ。

### AutoHotkey v2 インストール

```cmd
winget install --source winget AutoHotkey.AutoHotkey
```

### スクリプト作成

```cmd
notepad %USERPROFILE%\apex_caps.ahk
```

中身:
```
#Requires AutoHotkey v2.0
SetCapsLockState("AlwaysOff")
CapsLock::Send("^!r")
```

### 実行

エクスプローラーで `apex_caps.ahk` を**右クリック → 管理者として実行**。タスクトレイに緑のHアイコンが出ればOK。

> Apex Voice が管理者権限プロセスから起動されている場合、AutoHotkey も**管理者権限**で起動する必要がある（権限分離によりキーが届かない）。

## ログイン時自動起動（タスクスケジューラ）

### Apex Voice 本体

起動用バッチを作成:
```cmd
notepad %USERPROFILE%\apex_voice_start.bat
```
```
@echo off
cd /d %USERPROFILE%
python -u voicetype_win.py 2>"%USERPROFILE%\apexvoice_err.txt"
```

タスクスケジューラに登録（管理者プロンプトで）:
```cmd
schtasks /create /tn "ApexVoice" /tr "%USERPROFILE%\apex_voice_start.bat" /sc onlogon /rl highest /f
```

動作確認:
```cmd
schtasks /run /tn "ApexVoice"
```

### AutoHotkey

```cmd
schtasks /create /tn "ApexCapsAHK" /tr "%USERPROFILE%\apex_caps.ahk" /sc onlogon /rl highest /f
schtasks /run /tn "ApexCapsAHK"
```

## メニュー

タスクトレイの 🎤 を**左クリック**してメニューが開きます。

| 項目 | 内容 |
|---|---|
| 🎤 録音開始 / ■ 録音停止 | クリックで録音トグル（ホットキー `<ctrl>+<alt>+r` でも可） |
| 後処理: raw / 整文 / 敬語 / 英訳 / 箇条書き | Bedrock後処理モード切替 |
| 終了 | アプリ終了 |

選んだ設定は `~/.apexvoice/config.json` に保存され、次回起動時に復元されます。

## 設定（環境変数）

| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICETYPE_MODEL` | `large-v3-turbo` | faster-whisper モデル（`tiny`/`base`/`small`/`medium`/`large-v3`/`large-v3-turbo`） |
| `VOICETYPE_LANG` | `ja` | 認識言語 |
| `APEXVOICE_BEDROCK_MODEL` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 後処理モデル |
| `APEXVOICE_BEDROCK_REGION` | `us-east-1` | Bedrockリージョン |

タスクスケジューラ経由で起動するときに環境変数を渡すには、起動用バッチに `set VOICETYPE_MODEL=medium` を追記する。

## 仕組み

```
マイク (pyaudio)
  → push-to-talk バッファ
  → Silero VAD (音声区間なしなら破棄)
  → faster-whisper (CPU/int8)
  → 幻聴フィルタ (STRONG/WEAK)
  → 後処理 (Bedrock Claude Haiku 4.5) ※モード時のみ
  → 前面ウィンドウ復元 (pywin32)
  → クリップボード書込 (pyperclip) + Ctrl+V 送出 (pynput)
  → クリップボード復元 (非同期)
```

## トラブルシューティング

- **起動しても何も出力されない** → `2>err.txt` で stderr 分離して起動。コンソールが対応していない文字でクラッシュしているケース
- **ホットキーで反応しない** → Apex Voice ログに `ホットキー登録: <ctrl>+<alt>+r` が出ているか確認。出ていなければ config.json のパスや内容を確認
- **AutoHotkey で Caps Lock が効かない** → Apex Voice と AutoHotkey の権限を揃える（両方とも管理者 or 両方とも標準）
- **Bedrock呼び出しで `MissingDependencyException`** → `pip install "botocore[crt]"`
- **Whisper が遅すぎる** → `set VOICETYPE_MODEL=small` で軽量化（精度は落ちる）

## macOS版との差分

| | macOS | Windows |
|---|---|---|
| 音声認識 | mlx-whisper (Apple Silicon) | faster-whisper (CPU/int8) |
| トレイ | rumps | pystray |
| 挿入 | NSPasteboard + osascript Cmd+V | pyperclip + pynput Ctrl+V |
| 自動起動 | LaunchAgent | タスクスケジューラ |
| Caps Lockリマップ | Karabiner-Elements | AutoHotkey v2 |
| フォーカス復元 | 不要 | pywin32 SetForegroundWindow |
| AgentCore Browser / Payments | 実装済み・検証済み | コードパス共通・未検証 |

## ライセンス
MIT
