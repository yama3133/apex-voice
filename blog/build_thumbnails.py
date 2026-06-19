# -*- coding: utf-8 -*-
"""ブログ用サムネイル(1200x630)をPillowで生成する。

DEV.toやAWS Builder CenterはSVGをカバー画像にできないため、
PNGを用意する。cairosvgではmacOSの日本語フォントを解決できず
豆腐になるので、Pillowで直接描画する。
"""
import glob
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (216, 217, 222)        # #d8d9de
RED = (255, 59, 48)         # #ff3b30
WHITE = (255, 255, 255)
DARK = (26, 26, 26)         # #1a1a1a


def _find_font(*candidates_with_globs):
    """指定パスの先頭からヒットしたフォントを返す。
    macOS Tahoe(27)はフォントが動的アセットなのでgrobで探す必要がある。"""
    for c in candidates_with_globs:
        if "*" in c:
            hits = sorted(glob.glob(c))
            if hits:
                return hits[0]
        elif __import__("os").path.exists(c):
            return c
    raise FileNotFoundError(f"font not found: {candidates_with_globs}")


# 日本語: BIZ UDGothic (Tahoeでも入っているUDフォント) → 無ければ Hiragino Sans GB
FONT_JA_HEAVY = _find_font(
    "/System/Library/AssetsV2/com_apple_MobileAsset_Font8/*/AssetData/BIZ_UDGothic.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)
FONT_EN_HEAVY = _find_font(
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Arial Black.ttf",
)
FONT_EN_BOLD = _find_font(
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


def draw_centered(draw, text, y, size, font_path, color):
    font = ImageFont.truetype(font_path, size)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (W - text_w) // 2 - bbox[0]
    draw.text((x, y), text, font=font, fill=color)


def build(lang: str, out_path: str):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 中央の赤帯
    d.rectangle([(540, 0), (660, H)], fill=RED)

    if lang == "ja":
        head_font = FONT_JA_HEAVY
        main_font = FONT_EN_HEAVY
        meta_font = FONT_JA_HEAVY
        lines = ["macOSのどこでも", "音声入力できる", "アプリを作った"]
        sub = "mlx-whisper × Amazon Bedrock × Strands Agents"
        meta = "Yuuki Yamashita ／ AWS Community Builder"
    else:
        head_font = FONT_EN_HEAVY
        main_font = FONT_EN_HEAVY
        meta_font = FONT_EN_BOLD
        lines = ["Voice Typing", "Anywhere on", "macOS"]
        sub = "mlx-whisper × Amazon Bedrock × Strands Agents"
        meta = "Yuuki Yamashita / AWS Community Builder"

    # 上部キャッチコピー(白)
    y = 60
    for line in lines:
        draw_centered(d, line, y, 56, head_font, WHITE)
        y += 70

    # メインタイトル (Arial Blackで太く)
    draw_centered(d, "Apex Voice", 310, 130, FONT_EN_HEAVY, WHITE)

    # サブ
    draw_centered(d, sub, 480, 22, FONT_EN_BOLD, WHITE)

    # 下部メタ(背景部分・濃色)
    draw_centered(d, meta, 575, 22, meta_font, DARK)

    img.save(out_path, "PNG", optimize=True)
    print(f"OK: {out_path}")


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    build("ja", os.path.join(here, "apex_voice_thumbnail_ja.png"))
    build("en", os.path.join(here, "apex_voice_thumbnail_en.png"))
