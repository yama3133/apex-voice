#!/bin/bash
# blog/apex_voice_architecture_{ja,en}.svg を 1800x1120 PNG に変換する。
# cairosvg はmacOSの日本語フォントを解決できないため、
# Chrome ヘッドレスを使う。

set -euo pipefail
cd "$(dirname "$0")"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ ! -x "$CHROME" ]; then
    echo "Google Chrome not found at $CHROME" >&2
    exit 1
fi

for lang in ja en; do
    html=$(mktemp -t apex_arch_${lang}.XXXXXX).html
    cat > "$html" <<EOF
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  html,body{margin:0;padding:0;background:transparent;}
  svg{width:1800px;height:1120px;display:block;}
</style></head><body>
EOF
    cat "apex_voice_architecture_${lang}.svg" >> "$html"
    echo "</body></html>" >> "$html"

    "$CHROME" --headless --disable-gpu \
        --screenshot="$(pwd)/apex_voice_architecture_${lang}.png" \
        --window-size=1800,1120 \
        --hide-scrollbars \
        "file://${html}"

    rm -f "$html"
done

echo "Done: apex_voice_architecture_{ja,en}.png"
