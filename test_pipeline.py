#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""認識パイプラインの疎通＆精度確認（test.raw を文字起こし）"""
import time
import numpy as np
from voicetype import Transcriber

audio = np.fromfile("test.raw", dtype=np.float32)
print(f"音声長: {len(audio)/16000:.1f}秒")

t = Transcriber()
s = time.time()
text = t.transcribe(audio)
print("認識結果:", repr(text))
print(f"所要(初回はモデルDL込み): {time.time()-s:.1f}秒")
