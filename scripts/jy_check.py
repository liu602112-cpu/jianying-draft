#!/usr/bin/env python3
"""
写入后自检：按草稿切点把主轨音频拼出来（含倍速、复合片段），用 whisper.cpp 识别，和字幕逐条对照。

用法：
  python3 jy_check.py <草稿名或目录> [...] [-o 输出目录]

输出每条的识别全文和字幕全文，并列出差异片段（多出来的 = 可能漏进了区间外的声音；少了的 = 可能切掉了字）。
重点看每段区间的首尾：区间外紧挨着的话会被带进来（见 jy_tighten 的 edge_pre / edge_post）。
识别本身有错字（操盘手→操完手），差异里同音错字可以忽略，只看多出 / 缺失的整词。

依赖：Windows whisper-cli.exe 与 ggml-small.bin；由 install.ps1 安装，也可通过环境变量指定。
"""

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
import jy_audio
from jy import decrypt_text, resolve  # noqa: E402

MODELS = R.whisper_models()


def main_track(info):
    """Keep outer compound trims/speeds; jy_audio resolves their source audio."""
    tr = next(t for t in info["tracks"] if t["type"] == "video" and t["segments"])
    return sorted(tr["segments"], key=lambda s: s["target_timerange"].get("start", 0)), jy_audio._index(info)


def render(info, out_wav, draft_dir=None):
    """按主轨切点拼音频（16k 单声道，只用于识别）。逐段用 -ss 定位读取再拼接：
    几百段都从同一个长文件里 atrim 会每段从头解码，长片要卡很久。"""
    segs, idx = main_track(info)
    speeds = [float(s.get("speed") or next((idx[r][1].get("speed", 1.0) for r in s.get("extra_material_refs", []) if idx.get(r, ("",))[0] == "speeds"), 1.0)) for s in segs]
    if any(sp <= 0 for sp in speeds):
        raise ValueError("不支持零或负倍速")
    uniform = len({round(sp, 6) for sp in speeds}) == 1
    pcm = bytearray()
    for s, speed in zip(segs, speeds):
        # 占位符路径、混在主轨里的复合片段都走 jy_audio
        path = jy_audio.seg_audio_path(info, s, draft_dir, idx) or idx[s["material_id"]][1]["path"]
        a = s["source_timerange"].get("start", 0) / 1e6
        dur = s["source_timerange"]["duration"] / 1e6
        n = int(round(dur * 16000))
        raw = subprocess.run([R.tool("ffmpeg"), "-v", "error", "-ss", f"{a:.4f}", "-t", f"{dur:.4f}", "-i", path, "-vn",
                              "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        raw = raw[:n * 2] + b"\0" * max(0, n * 2 - len(raw))   # 每段按理论长度对齐，免得误差累积
        # Mixed speeds are adjusted individually; identical speeds keep v3's single atempo pass.
        if not uniform:
            raw = subprocess.run([R.tool("ffmpeg"), "-v", "error", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "-", "-af", f"atempo={speed}", "-f", "s16le", "-"], input=raw, capture_output=True, check=True).stdout
        scale = speeds[0] if uniform else 1.0
        tr = s["target_timerange"]
        start = round(tr.get("start", 0) / 1e6 * 16000 * scale) * 2
        size = round(tr["duration"] / 1e6 * 16000 * scale) * 2
        if start < len(pcm) - 2:
            raise ValueError("主视频轨片段重叠，无法可靠渲染识别音频")
        pcm += b"\0" * max(0, start - len(pcm))
        pcm += raw[:size] + b"\0" * max(0, size - len(raw))
    # 拼完再统一变速（逐段 atempo 会累积误差）；草稿里倍速一致时取第一段的
    sp0 = speeds[0] if uniform else 1.0
    cmd = [R.tool("ffmpeg"), "-v", "error", "-y", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "-"]
    if abs(sp0 - 1) > 1e-6:
        cmd += ["-af", f"atempo={sp0}"]
    subprocess.run(cmd + ["-ar", "16000", "-ac", "1", str(out_wav)], input=bytes(pcm), check=True)   # 只用于识别；量响度别这么做


def subtitles(info):
    tx = {m["id"]: json.loads(m["content"])["text"] for m in info["materials"].get("texts", [])}
    segs = [s for t in info["tracks"] if t["type"] == "text" for s in t["segments"] if s["material_id"] in tx]
    return "".join(tx[s["material_id"]] for s in sorted(segs, key=lambda s: s["target_timerange"].get("start", 0)))


def norm(s):
    return re.sub(r"[\s,，。.?？!！、:：;；\"'“”]", "", s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drafts", nargs="+")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    model = next((m for m in MODELS if m.exists()), None)
    if not model:
        sys.exit("找不到 ggml-small.bin 模型")
    out = Path(a.out or tempfile.mkdtemp(prefix="jy_check_"))
    out.mkdir(parents=True, exist_ok=True)
    for name in a.drafts:
        f = resolve(name)
        info = json.loads(decrypt_text(f.read_text(encoding="utf-8-sig")))
        wav = out / f"{f.parent.name}.wav"
        render(info, wav, f.parent)
        asr = R.transcribe(wav, model)
        wav.with_suffix(".txt").write_text(asr, encoding="utf-8")
        asr = asr.replace("\n", "")
        sub = subtitles(info)
        print(f"\n▶ {f.parent.name}\n  识别：{asr}\n  字幕：{sub}")
        sm = difflib.SequenceMatcher(None, norm(sub), norm(asr), autojunk=False)
        diffs = [(norm(sub)[i1:i2], norm(asr)[j1:j2]) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
        extra = [d for d in diffs if not d[0] and len(d[1]) >= 2]
        miss = [d for d in diffs if d[0] and not d[1] and len(d[0]) >= 2]
        print(f"  识别多出（查漏音）：{[d[1] for d in extra]}")
        print(f"  字幕有、识别没有（查切字）：{[d[0] for d in miss]}")
    print(f"\n音频和识别稿在 {out}")


if __name__ == "__main__":
    main()
