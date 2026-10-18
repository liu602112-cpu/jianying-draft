#!/usr/bin/env python3
"""
给没有逐字时间的剪映字幕补上逐字时间（jy_tighten 的 "words_file"）。

用法：
  python3 jy_words.py <草稿名> <whisper -ojf 输出的 json> -o words.json

whisper 的输入音频必须是按草稿时间线渲染的（jy_check.render），这样识别时间 = 时间线时间。
做法：
  - 分词：连续的英文/数字（含 . , % +）算一个词，其余逐字；空格丢掉。
  - 每句字幕取 [起-1.5s, 止+1.5s] 内 whisper 的字，与字幕逐字对齐（difflib）；对上的字用 whisper 时间，
    对不上的按前后插值。然后把这句在 whisper 里的跨度线性映射到剪映给的字幕起止上——
    剪映的整句起止比 whisper 准，whisper 只用来定句内每个字的相对位置。
  - 剪映字幕可能整体比声音晚、而且越往后越晚（2 小时的片子实测从 0 漂到 +0.75s）。
    给 --audio（同一份时间线音频）时，每 30 秒用「字幕占位 vs 有声段」做互相关测出局部偏移，
    平滑后把字幕起止校正回声音上。逐字时间允许为负（相对原字幕起点往前）。
"""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
from jy import decrypt_text, resolve  # noqa: E402

ASCII = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%+]*")


def tokenize(text):
    toks, i = [], 0
    while i < len(text):
        m = ASCII.match(text, i)
        if m:
            toks.append(m.group().rstrip(".,"))   # 词尾的 . , 丢掉
            i = m.end()
            continue
        if not text[i].isspace():
            toks.append(text[i])
        i += 1
    return toks


def norm_char(c):
    return c.lower()


def whisper_chars(path):
    j = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    out = []
    for seg in j["transcription"]:
        for t in seg.get("tokens", []):
            tx = t["text"]
            if tx.startswith("[_") or not tx.strip():
                continue
            a, b = t["offsets"]["from"] / 1000, t["offsets"]["to"] / 1000
            chars = [c for c in tx if not c.isspace() and c not in "，。、,.?？!！：:；;“”\"'"]
            if not chars:
                continue
            step = (b - a) / len(chars)
            for k, c in enumerate(chars):
                out.append((norm_char(c), a + k * step, a + (k + 1) * step))
    return out


MODEL = next((p for p in R.whisper_models() if p.is_file()), R.whisper_models()[0])


def line_chars(audio, a, b):
    """截 [a, b] 秒单独识别，返回 [(字, 起, 止)]（时间线秒）。-ml 1 让 whisper 按 token 出时间，短片段里比整段识别准。"""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory(prefix="jy_words_", dir=R.whisper_cache()) as tmp:
        wav = str(Path(tmp) / "audio.wav")
        subprocess.run([R.tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{max(0, a):.3f}", "-t", f"{b - a:.3f}", "-i", audio,
                        "-ar", "16000", "-ac", "1", wav], check=True)
        o = subprocess.run([R.tool("whisper-cli"), "-m", R.short_path(MODEL), "-l", "zh", "-t", "4", "-np", "-ml", "1",
                            "--prompt", "以下是普通话的句子。", "-f", wav], capture_output=True, text=True, encoding="utf-8-sig", errors="replace", check=True).stdout
    out = []
    for m in re.finditer(r"\[(\d+):(\d+):([\d.]+) --> (\d+):(\d+):([\d.]+)\]\s*(.*)", o):
        t0 = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        t1 = int(m[4]) * 3600 + int(m[5]) * 60 + float(m[6])
        cs = [c for c in m[7] if not c.isspace() and c not in "，。、,.?？!！：:；;“”\"'"]
        for k, c in enumerate(cs):
            out.append((norm_char(c), max(0, a) + t0 + (t1 - t0) * k / len(cs), max(0, a) + t0 + (t1 - t0) * (k + 1) / len(cs)))
    return out


def drift_map(audio, segs, step=30, win=90, span=1.5):
    """字幕相对声音的局部偏移（秒，正 = 字幕晚）。返回 t → 偏移 的插值函数。"""
    import numpy as np
    import subprocess
    raw = subprocess.run([R.tool("ffmpeg"), "-v", "error", "-i", audio, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.int16).astype(float)
    db = 10 * np.log10((x[:len(x) // 80 * 80].reshape(-1, 80) ** 2).mean(1) + 1)   # 10ms 一格
    n = len(db)
    occ = np.zeros(n + 400)
    for s in segs:
        a0 = int(s["target_timerange"].get("start", 0) / 1e4)
        occ[a0:a0 + int(s["target_timerange"]["duration"] / 1e4)] = 1
    ts, offs = [], []
    sp = int(span * 100)
    for c in range(0, n, step * 100):
        lo, hi = max(sp, c - win * 50), min(n - sp, c + win * 50)
        if hi - lo < 3000:
            continue
        seg = db[lo:hi]
        v = (seg > np.percentile(seg, 40)).astype(float)
        sc = [np.dot(v, occ[lo + k:hi + k]) - 0.5 * occ[lo + k:hi + k].sum() for k in range(-sp, sp + 1)]
        ts.append(c / 100)
        offs.append((int(np.argmax(sc)) - sp) / 100)
    if not offs:
        print("音频不足以估计字幕漂移，保留原字幕起止。")
        return lambda t: 0.0
    offs = [float(np.median(offs[max(0, i - 2):i + 3])) for i in range(len(offs))]   # 去掉个别跳点
    print(f"字幕偏移（正=字幕晚）：开头 {offs[0]:+.2f}s，中段 {offs[len(offs) // 2]:+.2f}s，结尾 {offs[-1]:+.2f}s")
    return lambda t: float(np.interp(t, ts, offs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft")
    ap.add_argument("asr")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--audio", help="按时间线渲染的音频（jy_check.render），用来校正字幕与声音的偏移")
    ap.add_argument("--refine", help="逐句精修的字幕序号（按时间排序、从 0 起，逗号分隔，或 @文件）：这些句子单独截音频用 whisper 逐字识别，"
                    "用识别出的字时间做句内锚点。句内要删字（口误、重复）的句子用它，插值估的时间会差到 0.5 秒以上")
    a = ap.parse_args()
    f = resolve(a.draft)
    d = json.loads(decrypt_text(f.read_text(encoding="utf-8-sig")))
    texts = {m["id"]: m for m in d["materials"]["texts"]}
    segs = sorted((s for t in d["tracks"] if t["type"] == "text" for s in t["segments"]),
                  key=lambda s: s["target_timerange"].get("start", 0))
    wc = whisper_chars(a.asr)
    starts = [x[1] for x in wc]
    import bisect
    off = drift_map(a.audio, segs) if a.audio else (lambda t: 0.0)
    refine = set()
    if a.refine:
        txt = Path(a.refine[1:]).read_text(encoding="utf-8-sig") if a.refine.startswith("@") else a.refine
        refine = {int(x) for x in re.split(r"[,\s]+", txt.strip()) if x}
        if not a.audio:
            sys.exit("--refine 需要 --audio")
    out, stats = {}, [0, 0, 0]
    for si, s in enumerate(segs):
        m = texts[s["material_id"]]
        text = json.loads(m["content"])["text"]
        toks = tokenize(text)
        if not toks:
            continue
        st0 = s["target_timerange"].get("start", 0) / 1e6       # 字幕片段原起点（逐字时间相对它）
        st = st0 - off(st0)
        en = st0 + s["target_timerange"]["duration"] / 1e6
        en -= off(en)
        lo, hi = bisect.bisect_left(starts, st - 1.5), bisect.bisect_right(starts, en + 1.5)
        win = wc[lo:hi]
        # 字幕逐字（英文词按字母展开，记住属于哪个词）
        chars, owner = [], []
        for k, t in enumerate(toks):
            for c in t:
                chars.append(norm_char(c))
                owner.append(k)
        times = [None] * len(chars)
        sm = difflib.SequenceMatcher(None, chars, [x[0] for x in win], autojunk=False)
        for blk in sm.get_matching_blocks():
            for q in range(blk.size):
                times[blk.a + q] = (win[blk.b + q][1], win[blk.b + q][2])
        if si in refine:
            loc = line_chars(a.audio, st - 0.4, en + 0.4)
            times = [None] * len(chars)
            sm2 = difflib.SequenceMatcher(None, chars, [x[0] for x in loc], autojunk=False)
            for blk in sm2.get_matching_blocks():
                for q in range(blk.size):
                    times[blk.a + q] = (loc[blk.b + q][1], loc[blk.b + q][2])
            if times[0]:      # 精修时整句起止也信逐句识别（剪映的整句起止可能偏）
                st = min(st, times[0][0]) if times[0][0] > st - 0.4 else st
            if times[-1]:
                en = max(en, times[-1][1])
            stats[2] += 1
        hit = sum(1 for x in times if x)
        stats[0] += hit
        stats[1] += len(chars)
        # 锚点：整句起止用（校正后的）剪映时间；句内只采用落在起止之间、且前后顺序正确的 whisper 字时间，
        # 其余按字数插值。对齐窗口外的同音字、whisper 时间回跳都会被丢掉，不会让逐字时间乱序。
        anchors, last = [(0, st)], st
        for q, x in enumerate(times):
            if x and 0 < q and st + 0.03 < x[0] < en - 0.03 and x[0] > last + 0.02:
                anchors.append((q, x[0]))
                last = x[0]
        anchors.append((len(chars), en))
        import numpy as np
        qs, tv = [x[0] for x in anchors], [x[1] for x in anchors]
        b_ = np.interp(range(len(chars) + 1), qs, tv)
        times = [(float(b_[q]), float(b_[q + 1])) for q in range(len(chars))]
        # 字 → 词
        ws, we = {}, {}
        for q, k in enumerate(owner):
            ws.setdefault(k, times[q][0])
            we[k] = times[q][1]
        out[s["material_id"]] = {
            "start_time": [int(round((ws[k] - st0) * 1000)) for k in range(len(toks))],
            "end_time": [int(round((we[k] - st0) * 1000)) for k in range(len(toks))],
            "text": toks}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8-sig")
    print(f"{len(out)} 句（逐句精修 {stats[2]} 句），逐字对上 whisper {stats[0]}/{stats[1]}（{stats[0] / max(1, stats[1]):.0%}）→ {a.out}")


if __name__ == "__main__":
    main()
