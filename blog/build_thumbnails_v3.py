# -*- coding: utf-8 -*-
"""v3 (Windowsポート編) のサムネイル(1200x630)をPillowで生成する。

Windows全面押し出しデザイン:
- 背景: Windowsブルーのグラデーション
- 中央: 大きく「Apex Voice for Windows」
- 上部: キャッチコピー
- 下部: サブとメタ
- 装飾: Windowsロゴ風の4分割パネル
"""
import glob
import os
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630

# Windows blue
WIN_BLUE_TOP = (0, 120, 212)        # #0078d4
WIN_BLUE_BOTTOM = (0, 90, 158)      # #005a9e
WIN_BLUE_DARK = (0, 60, 110)        # 装飾用

WHITE = (255, 255, 255)
LIGHT = (200, 220, 245)
SOFT = (160, 195, 235)
DARK = (10, 20, 35)


def _find_font(*candidates):
    for c in candidates:
        if "*" in c:
            hits = sorted(glob.glob(c))
            if hits:
                return hits[0]
        elif os.path.exists(c):
            return c
    raise FileNotFoundError(f"font not found: {candidates}")


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


def make_gradient(width: int, height: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (width, height), top)
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(width):
            px[x, y] = (r, g, b)
    return img


def draw_centered(draw: ImageDraw.ImageDraw, text: str, y: int, size: int,
                  font_path: str, color: tuple) -> None:
    font = ImageFont.truetype(font_path, size)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    x = (W - text_w) // 2 - bbox[0]
    draw.text((x, y), text, font=font, fill=color)


def draw_windows_logo(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int,
                       color: tuple) -> None:
    """Windowsロゴ風の4分割ブロック。装飾用。"""
    gap = max(4, size // 14)
    half = size // 2
    # 左上 / 右上 / 左下 / 右下 の4タイル
    x0 = cx - half
    y0 = cy - half
    tile = half - gap // 2
    draw.rectangle([(x0, y0), (x0 + tile, y0 + tile)], fill=color)
    draw.rectangle([(x0 + tile + gap, y0), (x0 + size, y0 + tile)], fill=color)
    draw.rectangle([(x0, y0 + tile + gap), (x0 + tile, y0 + size)], fill=color)
    draw.rectangle([(x0 + tile + gap, y0 + tile + gap), (x0 + size, y0 + size)], fill=color)


def build(lang: str, out_path: str) -> None:
    img = make_gradient(W, H, WIN_BLUE_TOP, WIN_BLUE_BOTTOM)
    d = ImageDraw.Draw(img)

    # 装飾: 右下の半透明Windowsロゴ
    deco = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(deco)
    draw_windows_logo(dd, W - 170, H - 170, 220, (255, 255, 255, 28))
    img.paste(Image.alpha_composite(img.convert("RGBA"), deco).convert("RGB"))

    # 装飾: 左上のミニWindowsロゴ
    draw_windows_logo(d, 90, 90, 80, LIGHT)

    if lang == "ja":
        head_font = FONT_JA_HEAVY
        meta_font = FONT_JA_HEAVY
        # 上部キャッチ
        catch = "macOSアプリを"
        catch2 = "Windowsに移植した"
        sub = "faster-whisper × pyaudio × pywin32 × AutoHotkey"
        meta = "Yuuki Yamashita ／ AWS Community Builder"
    else:
        head_font = FONT_EN_HEAVY
        meta_font = FONT_EN_BOLD
        catch = "Ported the macOS app"
        catch2 = "to Windows"
        sub = "faster-whisper × pyaudio × pywin32 × AutoHotkey"
        meta = "Yuuki Yamashita / AWS Community Builder"

    # 上部キャッチコピー
    draw_centered(d, catch, 60, 40, head_font, LIGHT)
    draw_centered(d, catch2, 110, 40, head_font, LIGHT)

    # メインタイトル "Apex Voice"
    draw_centered(d, "Apex Voice", 195, 120, FONT_EN_HEAVY, WHITE)

    # for Windows
    draw_centered(d, "for Windows", 345, 80, FONT_EN_HEAVY, (255, 240, 100))

    # サブ (技術スタック)
    draw_centered(d, sub, 470, 22, FONT_EN_BOLD, SOFT)

    # 区切り
    d.rectangle([(W // 2 - 200, 510), (W // 2 + 200, 513)], fill=LIGHT)

    # メタ
    draw_centered(d, meta, 540, 22, meta_font, LIGHT)

    img.save(out_path, "PNG", optimize=True)
    print(f"OK: {out_path}")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    build("ja", os.path.join(here, "apex_voice_thumbnail_v3_ja.png"))
    build("en", os.path.join(here, "apex_voice_thumbnail_v3_en.png"))
