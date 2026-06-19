# Apex Voice

[日本語](README.md) | [English](README_en.md)

macOSメニューバーに常駐する **音声タイピング＋AIエージェント**。マイクに話すと、ローカルWhisperで文字に起こし、整文・敬語化・翻訳までこなして、**いま開いているアプリのカーソル位置に挿入**します。さらにエージェントモードでは音声から**リマインダー追加・カレンダー予定作成・Web検索・Webページ要約**といったアクションも実行可能。

- **音声認識**: ローカルWhisper (`mlx-whisper`, `whisper-large-v3-turbo`)
- **AI後処理**: Amazon Bedrock Claude Haiku 4.5 で整文・敬語・英訳・箇条書き
- **AIエージェント**: Strands Agents によるマルチステップ実行
  - macOS連携: リマインダー / カレンダー / URLオープン
  - Web取得 + 要約 (1発話で複数ツール順次実行)
- **語彙学習**: Amazon Bedrock AgentCore Memory に固有名詞・専門用語を蓄積し、Whisperに自動ヒント注入
- **グローバルホットキー**: ⌃⌥V でどこからでも録音トグル
- 多言語対応（11言語をメニューから切替）

## 動作環境
- **Apple Silicon Mac**（M1以降）。Intel Macは `mlx` 非対応のため不可
- macOS（マイク必須）
- 初回のみネット接続（Whisperモデル約1.5GBを自動DL。以降はオフライン動作）
- 後処理・エージェント・Memory機能はAWS認証必要（`aws login`）

## クイックスタート

### 1. ソースから動かす
```bash
git clone https://github.com/yama3133/apex-voice.git
cd apex-voice
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python voicetype.py
```

### 2. LaunchAgentで常駐させる（推奨）
ログイン時に自動起動・クラッシュ時に自動復帰する方式。`.app`バンドルより安定。

```bash
# plistのパスは絶対パスなので、リポジトリのcloneパスに合わせて編集が必要
cp com.yamashita.apexvoice.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
```

停止・再起動:
```bash
launchctl unload ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
launchctl load ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
```

ログ: `/tmp/apexvoice.log`

## 権限（初回だけ必要）
1. **マイク** — 初回録音時に許可
2. **アクセシビリティ** — 他アプリへ貼り付け（Cmd+V送出）に必須
3. **入力監視** — グローバルホットキー（⌃⌥V）に必要
4. **AWS認証** — 後処理・エージェント・Memory機能を使う場合（`aws login`）

## メニュー

メニューバー 🎤 を **左クリック** でメニューが開きます。

| 項目 | 内容 |
|---|---|
| 🎤 録音開始 / ■ 録音停止 | クリックで録音トグル（ホットキー ⌃⌥V でも可） |
| 状態 | 録音中 / 停止中 |
| 言語 | 11言語切替（自動判定/日本語/English/中文/한국어/Español/Français/Deutsch/Italiano/Português/Русский） |
| 後処理 | 生 / 整文 / 敬語化 / 英訳 / 箇条書き / **エージェント実行** |
| ホットキー | 現在のキー組合せ表示・変更 |
| 感度を上げる/下げる | VAD閾値の調整 |
| 終了 | アプリ終了 |

選んだ設定は `~/.apexvoice/config.json` に保存され、次回起動時に復元されます。

## 後処理モード

| モード | 動作 | 外部アクション |
|---|---|---|
| 生 | Whisper結果そのまま挿入 | なし |
| 整文 | フィラー除去・同音異義誤り補正・自然な書き言葉化 | なし |
| 敬語化 | ビジネス敬語に書き換え | なし |
| 英訳 | 自然な英語に翻訳 | なし |
| 箇条書き | 要点を箇条書きに | なし |
| **エージェント** | 発話内容を判定し、適切なツール実行 or テキスト挿入 | **あり** |

エージェントモードのみ外部アクションを伴います。普段は「生」または「整文」を選んでおけば、意図せず予定が作られたりブラウザが開くことはありません。

### エージェントが実行できるアクション (Strands Agents)
1発話で複数を順次実行可能。例えば「1分後にメール送るのを思い出させて、ついでに明日10時にミーティング予定追加して」と言えば2件同時に処理。

- **リマインダー追加** — 「30分後にメール送るのを思い出させて」
- **カレンダー予定作成** — 「明日の3時に田中さんと打ち合わせをカレンダーに入れて」
- **URL/検索を開く** — 「PythonのドキュメントをWebで検索して」
- **Web取得+要約** — 「Pythonの公式サイトから最新バージョン教えて」「Wikipediaで富士山について調べて」
  - 内容を取得→Claude Haiku 4.5で要約→カーソル位置に挿入

## 語彙メモリ（AgentCore Memory）

認識した整文済みテキストから固有名詞・専門用語を抽出し、ローカル（`~/.apexvoice/vocabulary.json`）＋クラウド（AgentCore Memory）に蓄積。次回以降の認識時に Whisper の `initial_prompt` として注入し、同音異義の誤認識を減らします。

使うほど自分専用辞書が育つ仕組み。

## 設定（環境変数）
| 変数 | 既定 | 説明 |
|---|---|---|
| `VOICETYPE_MODEL` | `mlx-community/whisper-large-v3-turbo` | 認識モデル |
| `VOICETYPE_LANG` | `ja` | 認識言語（空文字で自動判定） |
| `VOICETYPE_PROMPT` | (空) | 追加の用語ヒント |
| `VOICETYPE_SENSITIVITY` | `2.5` | VAD閾値（小さいほど拾いやすい） |
| `VOICETYPE_MIC` | (空=OS既定) | 使用マイク（index番号または名前の一部） |
| `VOICETYPE_DEBUG` | (空) | `1`でRMS音量ログ表示 |
| `APEXVOICE_BEDROCK_MODEL` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 後処理・エージェント用モデル |
| `APEXVOICE_BEDROCK_REGION` | `us-east-1` | Bedrockリージョン |
| `APEXVOICE_MEMORY_ID` | (既定値あり) | AgentCore Memory ストアID |
| `APEXVOICE_ACTOR_ID` | `default-user` | Memory上のユーザー識別子 |

## `.app` ビルド（参考・非推奨）
```bash
.venv/bin/python setup.py py2app
# → dist/Apex Voice.app
```
py2app は `python312.zip` 破損問題が起きやすく、`mlx-whisper` の読み込みが不安定なため非推奨。常駐運用には上記の **LaunchAgent 方式** を推奨。

## 仕組み
```
マイク → 簡易VAD → 発話区間
  → mlx-whisper (initial_prompt: 学習語彙)
  → 後処理(Bedrock Claude Haiku 4.5)
    ├─ 生/整文/敬語/英訳/箇条書き → テキスト挿入
    └─ エージェント(Strands) → Tool判定
        ├─ リマインダー/カレンダー/URL/Web要約 を順次実行
        └─ テキスト挿入(ツール未発火時)
  → AgentCore Memory に書き込み(語彙蓄積)
```

## バージョン履歴
- **v0.2.1** — LaunchAgent方式に変更（安定動作）／メニューバーアイコンを SF Symbols PNG 化／プロセス名を `setproctitle` で固定／Apex Voice Web 連携
- **v0.2.0** — Apex Voice にリネーム。Strands Agents マルチステップ、Web取得+要約、AgentCore Memory、グローバルホットキー追加
- **v0.1.0** — 初期リリース (WhisType として公開)

## ライセンス
MIT
