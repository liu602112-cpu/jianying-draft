#!/usr/bin/env python3
"""
主轨片段 → 能用 ffmpeg 读的音频文件。

- 素材拷进草稿目录后，路径写成 `##_draftpath_placeholder_<uuid>_##/materials/...`，换成草稿目录。
- 复合片段（素材 path 为空，extra_material_refs 里挂 drafts 条目）：把子草稿里的音频/视频轨
  按时间线混成一条 wav（单声道 16kHz，缓存在 %LOCALAPPDATA%/Codex/Jianying/analysis/），片段的 source_timerange
  就是子草稿时间线上的时间，可以直接拿来 atrim。只用于分析音量、识别回听，不用于量响度。
"""

import hashlib
import json
import re
import subprocess
from pathlib import Path
import jy_runtime as R

PH = re.compile(r"##_draftpath_placeholder_[^#]+_##")
CACHE = R.analysis_cache()


def real_path(path, draft_dir):
    if path and draft_dir and PH.match(path):
        return PH.sub(str(draft_dir).replace("\\", "/"), path, count=1)
    return path


def _index(info):
    return {m["id"]: (k, m) for k, l in info["materials"].items() if isinstance(l, list)
            for m in l if isinstance(m, dict) and "id" in m}


def flatten_compound(dr, draft_dir):
    """drafts 条目 → 子草稿声音混成的 wav 路径（有缓存）。"""
    sub = dr["draft"]
    idx = _index(sub)
    items = []
    for t in sub["tracks"]:
        if t["type"] not in ("audio", "video"):
            continue
        for s in t["segments"]:
            m = idx.get(s["material_id"], (None, {}))[1]
            p = real_path(m.get("path", ""), draft_dir)
            if not p or not Path(p).exists() or m.get("type") == "photo" or p.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            sp = s.get("speed") or 1.0
            tr, sr = s["target_timerange"], s.get("source_timerange") or {}
            items.append((p, sr.get("start", 0) / 1e6, sr.get("duration", tr["duration"]) / 1e6,
                          tr.get("start", 0) / 1e6, sp))
    if not items:
        raise ValueError("复合片段没有可读取的音频素材；请检查素材路径。")
    key = hashlib.md5(json.dumps([items, [(p, Path(p).stat().st_size, Path(p).stat().st_mtime_ns) for p, *_ in items]]).encode()).hexdigest()
    out = CACHE / f"compound_{key}.wav"
    if out.exists():
        return str(out)
    CACHE.mkdir(parents=True, exist_ok=True)
    inputs, parts = {}, []
    for n, (p, a, dur, at, sp) in enumerate(items):
        inputs.setdefault(p, len(inputs))
        tempo = f",atempo={sp}" if abs(sp - 1) > 1e-6 else ""
        parts.append(f"[{inputs[p]}:a]atrim={a:.4f}:{a + dur:.4f},asetpts=PTS-STARTPTS{tempo},"
                     f"aresample=16000,aformat=channel_layouts=mono,adelay={int(at * 1000)}:all=1[a{n}]")
    fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(len(items))) + \
        f"amix=inputs={len(items)}:normalize=0:dropout_transition=0[o]"
    cmd = [R.tool("ffmpeg"), "-v", "error", "-y"]
    for p in inputs:
        cmd += ["-i", p]
    tmp = out.with_suffix(".tmp.wav")
    cmd += ["-filter_complex", fc, "-map", "[o]", "-ar", "16000", "-ac", "1", str(tmp)]
    subprocess.run(cmd, check=True)
    tmp.rename(out)
    return str(out)


def seg_audio_path(info, seg, draft_dir, idx=None):
    """主轨片段对应的可读音频文件；读不到返回 None。"""
    idx = idx or _index(info)
    m = idx.get(seg["material_id"], (None, {}))[1]
    p = real_path(m.get("path", ""), draft_dir)
    if p and Path(p).exists():
        return p
    for r in seg.get("extra_material_refs", []):
        k, dr = idx.get(r, (None, None))
        if k == "drafts":
            return flatten_compound(dr, draft_dir)
    return None
