---
title: "Apex Voice v2 — 幻聴を根絶し、レイテンシを詰め、Caps Lock一発に着地させた話"
published: false
tags: aws, bedrock, python, macos
---

> 前回の続編。macOSの音声タイピングツール **Apex Voice**
> （mlx-whisper + Amazon Bedrock）を「日常で実際に気持ちよく使える」
> ところまで仕上げ直した記録。

## 前回のおさらい

数週間前にv0.2を公開した。ローカルWhisper・Bedrock後処理・
Strands Agentsのマルチステップ・AgentCore Memoryでの語彙学習。
動いた。が、毎日使ううちに3つのザラつきが残った：

1. **YouTube系の幻聴** — 何も言ってないのに「ご視聴ありがとうございました」
   「次の動画でお会いしましょう」が混入する
2. **レイテンシ** — 話してからテキストが出るまでの間に体感の遅延
3. **ホットキーの操作性** — `⌃⌥V` は動くが、3本指を使う組み合わせで
   全然馴染まなかった

v2では、この3つを潰しつつ、保留していた Bedrock AgentCore の
2機能を本格実装した。

- リポジトリ: [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
- 連携Web: [apex-voice-web.vercel.app](https://apex-voice-web.vercel.app)

## 1. 幻聴を根絶する（多層防御）

Whisper hallucinationはバグではなくモデルの性質。日本語訓練データの
多くがYouTube字幕で、無音や雑音を入れると尤度の最も高い「動画の
締めの定型句」を吐く。単一の対策では潰せないので、多層に重ねる。

**Layer 1: VAD自動切り出しをやめてpush-to-talkに。**
元のrecorderはRMS-VADで発話の始まりと終わりを検出していた。
これだと、マイクがキーボード音や息を拾った瞬間に「幻の発話」が
Whisperに渡って、Whisperが律儀に何か言葉を発明する。撤廃した：

```python
# listening中は判定なしで全部取る
if not self._speaking:
    self._speaking = True
    self._buf = list(self._pre)
self._buf.append(block)
```

ユーザーが明示的に録音をON/OFFする（Caps Lockで、§3参照）以上、
録音中のVAD判定は冗長。**ゲートを録音の境界に移して、中はノーガードに。**

**Layer 2: Whisperの前段に Silero VAD を最終ゲートとして置く。**
録音が終わったら[Silero VAD](https://github.com/snakers4/silero-vad)
に通す。Sileroが「ここに発話なし」と言ったら、音声はWhisperに
触れさせない。

```python
if not _silero_has_speech(audio):
    log("Silero VAD: 音声区間なしと判定 → 破棄")
    return ""
```

Sileroは起動時にバックグラウンドで先読みする。最初の発話で
モデルロード待ちが発生しないように。

**Layer 3: mlx-whisperのデコードを厳しめに。**
3つのつまみ：

```python
kwargs = dict(
    no_speech_threshold=0.7,         # 0.5→0.7 無音側に倒す
    logprob_threshold=-0.5,          # 自信なし出力を捨てる
    compression_ratio_threshold=2.2, # 反復スパイラルを捨てる
    temperature=0.0,                 # fallbackの梯子を廃止
)
```

`temperature` fallback はレイテンシのコストでもあった（§2参照）。

**Layer 4: STRONG / WEAK の二段フレーズフィルタ。**
以前のフィルタは「YouTube系フレーズが主成分」のときだけ破棄していた。
ところが「次の動画でお会いしましょう」はトリガーの「次の動画」が
頭にあって、その後ろに「でお会いしましょう」と9文字続く。
「主成分」判定をすり抜ける。

修正はシンプル。フレーズを2層に分けた：

- **STRONG**: 含まれた時点で問答無用で破棄
  （「次の動画」「ご視聴ありがとう」「お会いしましょう」「チャンネル登録」等）
- **WEAK**: フレーズが主成分のときだけ破棄
  （「おやすみなさい」— 普通の発話の可能性あり）

```python
for p in STRONG_HALLUCINATION_PHRASES:
    if p in text:
        return True  # 即殺
```

## 2. レイテンシを詰める

まず各段の所要時間を計測ログに出した：

```
audio=4.8s vad=56ms whisper=1499ms → 19文字
```

律速はWhisper。常に。なので3方向から攻めた。

**クリップボード復元を非同期化。** ここがブロッキングだった：

```python
self._pb_set(text)
ok = self._paste()  # ユーザーが体感するのはここまで
# その後 sleep 150ms、復元… ← これも待ってた
```

復元を別スレッドに移した。ユーザー体感の挿入時間が
初回 ~450ms → 定常 ~150ms に。

**Whisperモデルの量子化版に切替。** 既定モデルを
`mlx-community/whisper-large-v3-turbo` から
4bit量子化版 `whisper-large-v3-turbo-q4` に変更。正直な比較：

| | turbo (素) | turbo-q4 |
|---|---|---|
| モデルサイズ | ~800MB | ~350MB |
| Whisper処理時間 (音声4秒) | ~1500ms | ~1500ms |
| 体感精度 | 高 | 同じ or わずかに良い |

MLXは既に最適化されているので**速度差はほぼゼロ**。
ただしメモリ・ディスクが半分、精度は劣化なし。取らない理由がない。

**失敗実験。** `mlx-community/distil-whisper-large-v3` も試した。
理論上2倍速い。が、**日本語精度が大幅に崩壊**。Distil-Whisperは
英語特化モデルでそれが如実に出た。30秒で戻した。声を大にして書く：
**スループットだけ見ず、エンドツーエンドで測れ。**

最終的に、Mシリーズ Macで4秒の音声なら~1.5秒で挿入完了。
このモデル構成での床はここ。

## 3. Caps Lockを「ワンキー」ホットキーに

元のホットキーは `pynput.GlobalHotKeys` 経由の `⌃⌥V`。動く。が、
3本指の組み合わせには結局慣れなかった。**1キーを叩くだけ**で
発火するようにしたい — ところがまともな単キー候補にはどれも問題がある：

- 普通の文字キー（`v`、`a`等の単独） — 通常のタイピングが壊れる
- 右Cmd / 右Option — pynputが左右を区別できない
- `fn` (グローブキー) — macOSがユーザー空間から隠している
- Caps Lock — macOSがトグル動作で握っている
- F13–F19 — 理想的だが、ほとんどのキーボードに物理キーがない

解決策：OS層で **Karabiner-Elements** を使い Caps Lock を F19 に
リマップし、Apex Voice側でF19を待つ。Karabinerは長年運用されている
macOSのキーリマップツール。設定はルール1個のJSON：

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

Apex Voice はこのルールと `karabiner://import?url=…` リンクを同梱し、
**ワンクリックで取り込める**ようにした。結果、**Caps Lock を一度
叩くと録音開始、もう一度叩くと停止**。プロジェクト全体で
最高のUX改善。

ここで横の話を一つ。**`hotkey` 設定の `<f19>` は Windows でもそのまま
動く**（`pynput.GlobalHotKeys`の解釈が同じ）。Windowsでは
**PowerToys Keyboard Manager** で Caps Lock → F19 にリマップすれば、
**コードを1行も変えずに同じ体験**が再現できる。

## 4. AgentCore Browser で本物のWeb検索

エージェントの「Web検索→要約」ツールは最初、`requests` +
BeautifulSoup でGoogleのHTMLを取りに行く実装だった。
Googleのbot検出がServerlessのIPに気付いた瞬間にブロックされる。当然。

修正は二段構え：

```
1) requests + BeautifulSoup (高速・無料)
   ↓ 失敗 or 本文薄 or Googleブロック
2) AgentCore Browser (マネージドChromium + Playwright over CDP)
```

AgentCore Browser はAWS上のマネージドヘッドレスChromium。
boto3 + 認証付きWebSocketでアクセスし、Playwrightを
`connect_over_cdp` で繋いで通常のブラウザセッションとして駆動する。
bot検出があるサイトも、JS必須のサイトも、Googleの検索結果も動く。
**本物のブラウザだから**。

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

セッションはリクエストごとに起動・停止。Web要約は使用頻度が
低いので、コストを優先。

## 5. AgentCore Payments (x402) — エージェントに財布を渡す

これが目玉の新機能。**エージェントが支払いを実行できる。**

[AgentCore Payments](https://aws.amazon.com/bedrock/agentcore/) は、
埋込みクリプトウォレット（Coinbase CDP / Stripe Privy）経由で
AIエージェントが決済できるBedrockサービス。話す言語は
**[x402](https://www.x402.org/)**、エージェント時代に蘇った
「HTTP 402 Payment Required」プロトコル — 有料APIが 402 と
支払いマニフェストで返し、エージェントが支払いヘッダを生成して再送する。

Apex Voiceのツールはこれを既存の承認フローに繋いだ：

```python
@tool
def pay_for_paid_resource(
    url: str, max_amount_usd: float, description: str = ""
) -> str:
    """有料HTTPリソースに対し x402 で決済する。

    1) URLにGET → 402を受け取る
    2) PaymentSession作成(上限額付き)
    3) 402本文から generate_payment_header
    4) 支払いヘッダ付きで再GET
    """
```

フロー全体：

```
発話「このAPIに5セントまで払って」
  → Whisper転写
  → Strands Agent が pay_for_paid_resource を選択
  → ガードレールチェック(1回/1日上限)
  → 人間承認ダイアログ(またはAegis経由Slack承認)
  → PaymentManager.create_payment_session
  → generate_payment_header
  → 支払いヘッダ付きで HTTP再送
  → 結果をカーソル位置に挿入
```

セットアップは軽くない — AWSコンソールでPaymentManager + Connector
を作成し（Coinbase CDP or Stripe Privyのクレデンシャル必要）、
ユーザー用の埋込みウォレットを作成・ファンドする必要がある。
気軽なデモではないが、**コードパスは本物で、承認フローは
「AIエージェントに財布を渡す」べき形に構造化されている。**

## 新しいアーキテクチャ

すべてが組み合わさって、一日中動かしていられるものになった。

（構成図は下に）

パイプラインはローカルファースト：マイク → 録音 → Silero VAD
→ mlx-whisper → 挿入。Bedrock系の機能は、モードが要求した時だけ
発話単位で乗っかる。

## 学んだこと (v2版)

- **幻聴対策は多層防御が単一の対策に勝つ。** 前段(Silero VAD)、
  途中(logprob/no_speech)、後段(フレーズフィルタ)。
  どれか1層では足りない。
- **「明らかに速くなるはず」でも測る。** 量子化版は速くなると
  確信していた。違った。MLXがすでに勝負を決めていた。
- **正しい原始操作はクレバーな小細工に勝つ。** 3キーコンボ vs
  Caps Lock + Karabiner、目的は同じ。でも片方はユーザーから
  見えない。
- **AgentCoreはMemoryだけじゃない。** Browserはbot検出という
  現実問題を解決する。Paymentsは「エージェントに財布を渡す」を
  思考実験から、承認込みの実コードパスにする。

## 次にやりたいこと

- **本物の x402 エンドポイントを叩く。** いまPaymentsの
  コードパスは動くが、合成402レスポンダで検証している段階。
  本物の有料APIに繋ぎたい。
- **Windows移植。** mlx-whisper → faster-whisper、
  rumps → pystray。ホットキー設定(`<f19>`)は既に移植可能なので、
  Windowsユーザーは PowerToys でリマップ。
- **語彙UI。** AgentCore Memoryが裏で何週間も固有名詞を学び続けている。
  確認・編集するUIがまだない。

---

v0.2で幻聴やホットキーに泣かされた人がいたら、v2は戻って試す価値あり。
Issue/PRは [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice) で。
