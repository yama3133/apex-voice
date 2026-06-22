---
title: "Apex Voice をWindowsに移植したら全部詰まった話 (faster-whisper + pystray + pywin32)"
published: false
tags: aws, bedrock, python, windows
---

> macOS版の **Apex Voice** をWindowsに移植した。
> Mac版と機能パリティを目指して挑んだが、想像以上に全工程で詰まった。
> その記録。

## 出発点

macOSで毎日使っている [Apex Voice](https://github.com/yama3133/apex-voice)
は mlx-whisper + rumps + LaunchAgent。Apple Silicon前提で組んだので、
当然そのままではWindowsで動かない。

「Windows版があれば使いたい」というニーズと、検証環境としては
**UTM上のWindows 11 ARM64評価版** だけ手元にある状態でスタート。
正直、ここで地獄を覗くとは思っていなかった。

- リポジトリ: [github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
- Windows向けREADME: [README_win.md](https://github.com/yama3133/apex-voice/blob/main/README_win.md)

## 採用スタック

最初に決めた置き換え方針:

| | macOS | Windows |
|---|---|---|
| 音声認識 | mlx-whisper | faster-whisper (CPU/int8) |
| トレイ | rumps | pystray |
| 挿入 | NSPasteboard + osascript Cmd+V | pyperclip + pynput Ctrl+V |
| 自動起動 | LaunchAgent | タスクスケジューラ |
| Caps Lock 1キー化 | Karabiner-Elements | AutoHotkey v2 |

頭の中では「ライブラリを差し替えるだけ」と思っていた。
実際は、差し替えた**全ての層**で何かが詰まった。

## ハマり1: sounddeviceがARM64で動かない

最初の障害。マイク入力に sounddevice を使うつもりだったが、
ARM64 Windows では `libportaudioarm64.dll: error 0x7e` で
ロード失敗。PortAudioのARM64バイナリが提供されていない。

→ pyaudio に切り替えてフォールバック。pyaudio は ARM64 wheel が
あって普通に動く。Recorder クラスを丸ごと書き直し。

```python
class Recorder:
    """sounddeviceがARM64 Windowsで動かない場合、pyaudioにフォールバック。"""

    def _start_pyaudio(self):
        import pyaudio
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            rate=SAMPLE_RATE, channels=1, format=pyaudio.paFloat32,
            input=True, frames_per_buffer=BLOCK
        )
```

## ハマり2: 起動しても何も出力されない

`python voicetype_win.py` を実行しても、コンソールに何も出ない。
プロセスは動いてるのに止まったように見える。

最初は「pystrayがブロッキングしてる」と思った。
print文を増やしても一切出力されない。

実際の原因は **Windowsコンソールが標準エラーへ書かれる
バイナリ文字列でクラッシュ** していた。pyaudio や pystray が
起動時にstderrへ何か出していて、それがコンソールのコードページと
合わずに表示処理が詰まる。

回避策:

```cmd
python -u voicetype_win.py 2>err.txt
```

`-u` でstdoutを非バッファ化し、`2>err.txt` でstderrを
ファイルに分離する。これでようやく `STEP1` `STEP2` ... という
診断プリントが見えるようになった。

「`flush=True` 付き print が出ない」を疑った時間は1時間以上。

## ハマり3: pynputがF19を捕まえない

macOS版では Caps Lock → F19 リマップ + pynput で F19 を受ける構成。
同じことをWindows + PowerToysで再現しようとした。

PowerToysでCaps Lock → F19のリマップ自体は効いた
（Caps Lockランプが点灯しない＝Caps Lock本来の動作は無効化されている）。
でも pynputが F19 を捕まえない。`ホットキー登録: <f19>` のログは
出ているのに、押しても無反応。

Windowsの pynput は F13以降のFキーが不安定という既知の話を後で見つけた。
ホットキーを `<ctrl>+<alt>+r` に変更したら一発で動いた。

## ハマり4: PowerToysが Ctrl+V を奪った

ホットキー変更の途中、PowerToysのKeyboard Managerが
**Ctrl+V （ペースト）まで奪う**事態に。Apex Voice を止めても
Ctrl+V が効かない。PowerToysのキーボードフックが残骸として
スタックしていた。

最終的にメモ帳でキーボード入力が**全く**効かなくなり、
VM再起動するしかなくなった。再起動後、PowerToysは
信頼性に問題がありそうなので採用を見送り、**AutoHotkey v2** で
リマップする方針に変えた。

```ahk
#Requires AutoHotkey v2.0
SetCapsLockState("AlwaysOff")
CapsLock::Send("^!r")
```

`SetCapsLockState("AlwaysOff")` は Caps Lock のトグル動作
（大文字モード）を抑制する。

## ハマり5: タスクトレイ操作でフォーカスが奪われる

トレイアイコンをクリックして録音停止→Whisperが認識→
ペースト先のメモ帳に挿入、という流れで**挿入完了ログは出ているのに
メモ帳にテキストが入らない**。トレイクリックの瞬間、フォーカスが
コマンドプロンプトに移っていて、そっちに貼り付いていた。

修正は pywin32 で前面ウィンドウのハンドルを録音開始時に保存し、
挿入直前に復元する:

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
    # この後 pyperclip + Ctrl+V
```

macOS版では osascript が裏側でアクティブアプリ復元してくれていたので
要らなかった処理。OS差を埋める一手。

## ハマり6: BedrockがMissingDependencyException

`aws login` で認証して `aws sts get-caller-identity` は通るのに、
Pythonから boto3 で Bedrock を呼ぶと:

```
botocore.exceptions.MissingDependencyException: Missing Dependency:
Using the login credentials provider requires an additional dependency.
You will need to pip install "botocore[crt]"
```

`aws login` (AWS CLI 2.32.0以降) で生成される認証情報を読むには、
botocoreの追加依存`crt`が必要。

```cmd
pip install "botocore[crt]"
```

これでようやくBedrockの整文/敬語/英訳/箇条書きが動いた。

## ハマり7: UTMでCaps LockがVMに届かない

AutoHotkeyのスクリプトを管理者権限で実行しても、Caps Lockを押しても
何も起きない。AHKが反応しない。

切り分けたところ、**Mac → UTM → Windows のキー伝搬で
Caps Lock自体がWindowsまで届いていない** のが原因。
UTM側のキーパススルー仕様か、Mac側のCaps Lockが
OSレベルで吸収されているか、特定困難。

実機Windowsなら起きない問題のはず。今回は妥協して、
**ホットキーは `<ctrl>+<alt>+r` を直接押す運用** で完成とした。
Macキーボードから Control+Option+R を押すと UTM経由で
Windowsに Ctrl+Alt+R として届くので、機能としては問題なし。

## ログイン時自動起動

タスクスケジューラに `/sc onlogon /rl highest` で登録:

```cmd
schtasks /create /tn "ApexVoice" /tr "%USERPROFILE%\apex_voice_start.bat" /sc onlogon /rl highest /f
schtasks /create /tn "ApexCapsAHK" /tr "%USERPROFILE%\apex_caps.ahk" /sc onlogon /rl highest /f
```

注意点として、Apex Voice本体が管理者権限で動いているなら、
AutoHotkeyも管理者権限で起動する必要がある（Windowsの権限分離で
低権限プロセスから高権限プロセスへキー送信できないため）。

## 完成形と性能

実機の手元にないのでUTM上で計測:

| 項目 | UTM (ARM64) | 期待値（実機Windows推定） |
|---|---|---|
| 音声4秒の認識 | ~37秒 (CPU/int8, large-v3-turbo) | 数秒 |
| 挿入レイテンシ | ~250ms | 同程度 |
| Bedrock後処理 | 動作OK | 同上 |
| Caps Lock 1キー化 | UTM制約で不可 | 動くはず |

UTMの仮想CPUではWhisper処理が重すぎる。実機Windowsなら
`large-v3-turbo` でも実用的な速度のはず。それは実機の人に
試してもらう領域。

## 学んだこと

- **「ライブラリを差し替えるだけ」は嘘**。OSの権限モデル、
  キーコード、コードページ、フォーカス管理など、
  Pythonアプリでも下のレイヤーは全部違う。
- **コンソール出力が消えるバグはstderrを疑う**。
  `2>err.txt` で分離するだけで見えるようになる。
- **修飾キーを奪うキーリマップツールは怖い**。PowerToysの
  Keyboard Managerが残骸を残してCtrl+Vを死亡させた。
  AutoHotkey v2 の方が安定。
- **クロスプラットフォーム化の現実コスト**は、コード量ではなく
  「OSごとに異なる落とし穴を踏む数」で決まる。

## 次にやりたいこと

- **実機Windowsでの再検証**。UTMは検証ツールとしては
  十分でも、性能とCaps Lockは実機で確認したい。
- **GPU推論への対応**。faster-whisper は CUDA対応で
  GPU推論できるので、NVIDIA GPU 搭載Windowsなら
  劇的に速くなるはず。
- **AgentCore Memory / Browser / Payments の動作確認**。
  コードパスは共通だがWindows実機ではまだ検証していない。

---

UTM環境でも一通り動く状態まで持っていけたので、
GitHub と README_win.md にまとめて公開した。
実機Windowsで試した人は Issue/PR 待ってます:
[github.com/yama3133/apex-voice](https://github.com/yama3133/apex-voice)
