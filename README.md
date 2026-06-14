# WhisType

macOSメニューバーに常駐する **音声タイピングツール**。マイクに話すと、その内容をローカルWhisperで文字に起こし、**いま開いているアプリのカーソル位置にそのまま入力**します（Slack・メモ・ブラウザなど何でも）。

- **完全ローカル / オフライン**（モデルDL後はネット不要）
- **無料**・クラウドAPI不使用
- 認識: [`mlx-whisper`](https://github.com/ml-explore/mlx-examples/tree/main/whisper) (Apple Silicon最適化, `whisper-large-v3-turbo`)
- メニューバーから 左クリックで録音ON/OFF、右クリックで設定メニュー
- 8秒無音で自動停止（停止し忘れ防止）

## 動作環境
- **Apple Silicon Mac**（M1以降）。Intel Macは `mlx` 非対応のため不可。
- macOS（マイク必須）
- 初回のみネット接続（Whisperモデル約1.5GBを自動DL。以降はオフライン）

## インストール（ソースから動かす）

```bash
git clone https://github.com/yama3133/whistype.git
cd whistype

# Python 3.12 推奨（Homebrew）
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 起動
.venv/bin/python voicetype.py
```

## 権限（初回だけ必要）
1. **マイク**: 初回録音時にダイアログ → 「許可」。
2. **アクセシビリティ**: 他アプリへ貼り付け（Cmd+V送出）に必須。
   - システム設定 → プライバシーとセキュリティ → アクセシビリティ
   - 起動元（開発実行なら「ターミナル」、`.app`版なら「WhisType」）を **ON**
   - 許可がないと挿入されず、アプリが設定画面を自動で開きます。

## 使い方
1. メニューバーの 🎤 を **左クリック** → 録音開始（🔴）
2. テキスト欄にカーソルを置いて話す → 区切り（無音0.8秒）ごとに挿入
3. もう一度 🔴 を **左クリック** → 録音停止
4. **右クリック**でメニュー（感度調整・終了）
5. 録音ONのまま8秒無音が続くと自動停止

## 設定（環境変数）
| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICETYPE_MODEL` | `mlx-community/whisper-large-v3-turbo` | 認識モデル |
| `VOICETYPE_LANG` | `ja` | 認識言語（空文字で自動判定） |
| `VOICETYPE_PROMPT` | (空) | 用語ヒント。専門用語を入れると誤変換減 |
| `VOICETYPE_SENSITIVITY` | `2.5` | 小さいほど拾いやすい／大きいほど鈍感 |
| `VOICETYPE_MIC` | (空=OS既定) | 使用マイク（index番号または名前の一部） |
| `VOICETYPE_DEBUG` | (空) | `1`でRMS音量ログを表示 |

例（外付けマイク + 専門用語ヒント）:
```bash
VOICETYPE_MIC="THRONMAX" VOICETYPE_PROMPT="Bedrock。AgentCore。mlx。" .venv/bin/python voicetype.py
```

## `.app` ビルド（配布用）

```bash
.venv/bin/python setup.py py2app
# → dist/WhisType.app
```

- メニューバー常駐（LSUIElement、Dockに出ない）。マイク権限説明 (`NSMicrophoneUsageDescription`) 同梱。
- 未署名のため初回は **右クリック → 開く**。以降は通常起動。
- バンドルサイズは約875MB（mlx本体・numpy・PyObjC同梱のため）。

## 仕組み
```
マイク → 簡易VAD（音量ベース） → 発話区間を切り出し
  → mlx-whisper（文字起こし）
  → NSPasteboard（クリップボード退避→貼付→復元） → Cmd+V送出 → カーソル位置に挿入
```

## 既知の制約
- Whisperは音響だけで認識するため、**五十音の羅列**や固有名詞は誤変換が増えます（普通の文章は実用的）。
- 録音ON中に無言が続くと幻聴が出ることがあるため、8秒で自動停止しています。
- 完璧な整文・フィラー除去がほしい場合は後段にLLM（ローカルmlx or Amazon Bedrock）を足す拡張余地があります。

## ライセンス
MIT
