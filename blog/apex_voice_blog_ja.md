---
title: "macOSのどこでも音声入力できるツールを作った — Apex Voice (mlx-whisper + Amazon Bedrock + Strands Agents)"
published: false
tags: aws, bedrock, python, macos
---

> 週末プロジェクトのつもりが日常ツールになった話。Slack・ブラウザ・
> メモ — macOSのあらゆる入力欄に声でテキストを入れられる
> メニューバー常駐アプリを、ローカルWhisperとAmazon Bedrockで作った。

## TL;DR

**Apex Voice** という macOS 向け音声タイピングツールを作って
OSSで公開した。マイクから話すと
[mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
がオフラインで文字起こしし、カーソル位置に挿入する。Amazon Bedrock
を上に重ねれば、整文・翻訳・「リマインダー追加」「ページを要約して」
といったエージェント操作までこなす。

- リポジトリ: [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
- 連携Web: [apex-voice-web.vercel.app](https://apex-voice-web.vercel.app)

## なぜ作ったか

macOSには標準のディクテーションがあるし、Aqua Voice のような
完成度の高い有料ツールもある。それでも作った理由：

1. **標準ディクテーション** はアプリによって動かない、日本語精度も
   ムラがある。
2. **Aqua Voice** はよくできているが、クローズドで有料。
3. **自分でスタックを選びたかった**。モデル・後処理・エージェント
   ツールを自分で決められる方が圧倒的に楽しい。

## 何をするか

コアループ：

```
マイク
  → VAD
  → mlx-whisper
  → (Bedrock 後処理：任意)
  → クリップボード
  → ⌘V で任意のアプリへ
```

これに以下が乗る：

- **後処理モード**（Claude Haiku 4.5 on Bedrock）：
  `整文` / `敬語化` / `翻訳` / `箇条書き`
- **エージェントモード**（Strands Agents）：
  1発話で複数ツール呼び出し。リマインダー追加、カレンダー予定作成、
  Webページ取得→要約まで連続実行可能。
- **語彙学習**（Amazon Bedrock AgentCore Memory）：
  発話から固有名詞・専門用語を抽出してクラウドに永続化。
  次回起動時に Whisper の `initial_prompt` として注入される。
  使うほど認識精度が上がる。

## アーキテクチャ

`launchd` で常駐する Python プロセス。デフォルトはローカル完結で、
Bedrock 機能はオプトインで有効化する。

メインパイプライン（マイク→Whisper→挿入）は完全オフライン。
Bedrock と AgentCore Memory は補助で、なくても動く。

## AWS まわり

Bedrock を3つの形で使っている：

| 部品 | 役割 |
|---|---|
| **Amazon Bedrock (Claude Haiku 4.5)** | 後処理・エージェント分類・要約 |
| **Strands Agents** | ツールスキーマ＋マルチステップ呼び出し |
| **AgentCore Memory** | 語彙のセッションを跨いだ永続化 |

モデルに **Haiku 4.5** を選んだ理由は、後処理が *発話のたびに* 走る
から。ここでは推論力よりサブ秒のレイテンシが効く。エージェント
モードでも、Haiku は ~10種類のツール選択を十分こなせる。

連携 Web (`apex-voice-web`) は **Vercel** 上の Python サーバーレス
関数が Bedrock を叩いて URL 分類と要約をする。履歴フィードは
**Upstash Redis** で保持。macOS 側からは非ブロッキング POST で
履歴を送る。

## 大変だったのは AI じゃない。パッケージングだった。

`py2app` でほぼ丸一日溶かした。

筋書きは簡単だった：

```
setup.py py2app → dist/Apex Voice.app
→ /Applications に置く → 完成
```

現実：

```
[12:12:22] 認識エラー: bad local file header:
  '.../Apex Voice.app/Contents/Resources/lib/python312.zip'
```

`py2app` は依存ライブラリを `python312.zip` に詰めるが、
`mlx-whisper` のネイティブ拡張が zip 化に耐えられない。
`zip_include_packages: []` は py2app には無いオプション。
`.app` の中にシェルスクリプトのランチャを置いたら macOS に
「Rosettaが必要です」と言われた。手書きの arm64 ランチャを
コンパイルしたら動いたが、再ビルドのたびに署名が変わるので
アクセシビリティ権限を毎回付け直す羽目に。

**解決策：`.app` を作るのをやめた。**

代わりに **LaunchAgent** の plist を書いた。venv の Python と
スクリプトを直接指す。`~/Library/LaunchAgents/` に置いて
`launchctl load` で完了。`KeepAlive: true` にしておけば
クラッシュ時に自動復帰する。メニューの「再起動」は
`rumps.quit_application()` を呼ぶだけで、あとは launchd が
復活させてくれる。

仕上げに2点：

```python
import setproctitle
setproctitle.setproctitle("Apex Voice")
```

これで Activity Monitor に「python3.12」ではなく「Apex Voice」と
出る。メニューバーアイコンは SF Symbols
（`waveform.and.mic` / `mic.fill`）を PNG に書き出して
テンプレート画像として読み込んだ。ライト/ダークモードに自動追従
する。

## 学んだこと

- **OS のパッケージング流儀に逆らわない。** LaunchAgent + venv は
  py2app より全ての軸で勝った：シンプル・安定・更新も楽。
- **モデルはレイテンシで選ぶ、ランキングではなく。** 音声入力
  ループでは 500 ms 単位でユーザーが体感する。Haiku 4.5 が正解。
- **Bluetoothマイクの音質問題は OS の問題、アプリの問題じゃない。**
  マイクが HFP モードに入った瞬間 8 kHz に落ちる。macOS アプリ側で
  解決する手段はない。
- **アクセシビリティ経由のキー注入（`osascript` で Cmd+V）は
  正解の原始操作。** Slack でもブラウザでもネイティブエディタでも
  動く。アプリ毎の対応が要らない。

## 次にやりたいこと

- **語彙学習の UI** — AgentCore Memory が何を覚えたかを見える化。
- **Windows 移植** — `mlx-whisper` を `faster-whisper` に、
  `rumps` を `pystray` に差し替えるだけ。コアループは OS 非依存。
- **承認ゲート付きエージェント** — 別途作っている [Aegis]
  (https://github.com/yama3133/aegis-slack-app)（Slack ベースの
  AIエージェント承認プレーン）と連携させる。

## 試す

```bash
git clone https://github.com/yama3133/apex-voice.git
cd apex-voice
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 直接実行
.venv/bin/python voicetype.py

# LaunchAgent として常駐させる（自動起動・自動復帰）
# 先に plist のパスを自分の clone 先に書き換える
cp com.yamashita.apexvoice.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.yamashita.apexvoice.plist
```

Apple Silicon Mac（mlx は Apple Silicon 専用）と、後処理・
エージェント機能を使うなら AWS の認証情報が必要。

---

同じようなものを作ってる人、`py2app` で同じ壁にぶつかった人がいたら
ぜひ話を聞かせてほしい。Issue や PR は
[GitHub](https://github.com/yama3133/apex-voice) で歓迎。
