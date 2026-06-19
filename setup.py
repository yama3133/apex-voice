# -*- coding: utf-8 -*-
"""
py2app ビルド設定。
  ビルド: .venv/bin/python setup.py py2app
  生成物: dist/Apex Voice.app

メニューバー常駐(LSUIElement)・マイク権限説明を含む。
未署名のため配布先では初回「右クリック→開く」で起動する。
"""
import os
import sys
import sysconfig
from setuptools import setup

# py2app(modulegraph)が大きな依存解析で再帰上限に達するのを防ぐ
sys.setrecursionlimit(10000)

# mlxはnamespace package(__init__.py無し)なのでpy2appが見つけられない。
# 空の__init__.pyを置いてregular packageに昇格させる（mlx/mlx_metalは同じ物理ディレクトリ）。
_site = sysconfig.get_paths()["purelib"]
_mlx_init = os.path.join(_site, "mlx", "__init__.py")
if os.path.isdir(os.path.join(_site, "mlx")) and not os.path.exists(_mlx_init):
    with open(_mlx_init, "w") as f:
        f.write("")
    print(f"[setup] created {_mlx_init} for py2app")

APP = ["voicetype.py"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Apex Voice",
        "CFBundleDisplayName": "Apex Voice",
        "CFBundleIdentifier": "com.yamashita.apexvoice",
        "CFBundleVersion": "0.2.0",
        "CFBundleShortVersionString": "0.2.0",
        # Dockに出さずメニューバーのみに常駐
        "LSUIElement": True,
        # マイク利用の説明（初回ダイアログに表示）
        "NSMicrophoneUsageDescription": "音声を認識してテキストに変換するためにマイクを使用します。",
    },
    # ネイティブ依存はパッケージ丸ごと同梱する
    "packages": ["mlx_whisper", "mlx", "sounddevice", "numpy", "rumps",
                 "huggingface_hub", "certifi", "tqdm",
                 "pynput", "boto3", "botocore", "requests",
                 "bs4", "mcp", "anyio", "strands", "bedrock_agentcore"],
    # py2app が動的importを見落とすサブモジュールを明示
    "includes": [
        "pynput.keyboard._darwin",
        "pynput.mouse._darwin",
    ],
    # すべてのパッケージをzipに詰めない（mlx等の大きなネイティブ拡張はzip化で壊れる）
    "zip_include_packages": [],
}

setup(
    app=APP,
    name="Apex Voice",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
