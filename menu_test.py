#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""メニューバー安定性の純粋テスト。録音・LLM・スレッド一切なし。
これでもメニューが消えるなら原因はrumps/macOSにある。"""
import rumps


class MenuTestApp(rumps.App):
    def __init__(self):
        super().__init__("🧪", quit_button=None)

        # Apex Voiceと同程度の項目数のメニュー
        lang_menu = rumps.MenuItem("言語: 日本語")
        for label in ["自動判定", "日本語", "English", "中文", "한국어",
                      "Español", "Français", "Deutsch", "Italiano",
                      "Português", "Русский"]:
            lang_menu.add(rumps.MenuItem(label, callback=lambda _, l=label: self._on(l)))

        pp_menu = rumps.MenuItem("後処理: 生")
        for label in ["生", "整文", "敬語化", "英訳", "箇条書き", "エージェント実行"]:
            pp_menu.add(rumps.MenuItem(label, callback=lambda _, l=label: self._on(l)))

        purchase_menu = rumps.MenuItem("購入承認設定")
        for label in ["1回上限: ¥5,000", "1日累計上限: ¥20,000",
                      "承認ダイアログ: ON", "承認方式: ローカル(macOS)", "履歴を見る…"]:
            purchase_menu.add(rumps.MenuItem(label, callback=lambda _, l=label: self._on(l)))

        self.menu = [
            rumps.MenuItem("🎤 録音開始", callback=lambda _: self._on("録音")),
            rumps.MenuItem("状態: 停止中", callback=None),
            None,
            lang_menu,
            pp_menu,
            rumps.MenuItem("ホットキー: ⌃⌥V", callback=lambda _: self._on("hk")),
            purchase_menu,
            rumps.MenuItem("語彙ヒント注入: ON", callback=lambda _: self._on("vocab")),
            None,
            rumps.MenuItem("感度を上げる", callback=lambda _: self._on("sens+")),
            rumps.MenuItem("感度を下げる", callback=lambda _: self._on("sens-")),
            None,
            rumps.MenuItem("マイクを再取得", callback=lambda _: self._on("mic")),
            rumps.MenuItem("学習語彙をリセット", callback=lambda _: self._on("reset")),
            rumps.MenuItem("再起動", callback=lambda _: self._on("restart")),
            None,
            rumps.MenuItem("終了", callback=lambda _: rumps.quit_application()),
        ]

    def _on(self, label):
        print(f"[click] {label}")


if __name__ == "__main__":
    MenuTestApp().run()
