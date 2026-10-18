#!/usr/bin/env python3
"""
按响度自动定口播音量（「冒红尖儿」标准）。

用法：
  python3 jy_loud.py <草稿名或目录> [...] [--target -18] [--dry-run] [--force]

做法：把主视频轨按草稿切点拼出音频（原始音量），用 EBU R128 量整体响度 L（LUFS），
需要的增益 = target − L。
  · 增益 ≤ +20 dB：直接设在每个画面片段的音量上（剪映单段音量上限就是 +20 dB）。
  · 增益 > +20 dB：片段先拉满 +20，再把整条主轨建成一个复合片段，复合片段上补剩下的（最多再 +20）。
    这是用户自己的做法（0922直播里手动建过：里面 +20、外面 +6）。
target 默认 -13 LUFS 是源作者示例参数；仅在当前用户明确要求响度处理时调用。
那批是单边录音（左比右响 6 dB），左声道填充至右侧后原始 -34.0～-31.9 LUFS，+20 后 -14～-12，平均 -13。
电平表看的是最响那个声道的峰值，填充前后不变，所以基准按填充后的算。
注意：必须按立体声原样量（ebur128 默认），不要先混成单声道——这批素材左右声道差 6 dB，混单声道会偏低 3 dB 多。觉得整体偏大/偏小就改 --target（spec 里 "loudness_target"）。

已经是复合片段的草稿（主轨只有一个复合片段）会跳过。只改音量/包复合片段，不动文字和时间。
写入前要关闭剪映；用户改过的草稿要 --force（改动在现有文件上做，用户的其他修改保留）。
"""

import argparse
import copy
import json
import math
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
from jy import decrypt_text, encrypt_text, resolve  # noqa: E402

TARGET = -13.0
MAX_DB = 20.0
CH_DIFF = 3.0     # 左右声道 RMS 差这么多 dB 以上，当成单边录音，自动把响的那边填充到另一边
MIN_OUTER = 1.0   # 拉满 +20 后只差不到这么多 dB，就不建复合片段（听不出来，没必要多套一层）
TEMPLATE = Path(__file__).parent.parent / "presets" / "compound_template.json"


def new_id():
    return str(uuid.uuid4()).upper()


def db2vol(db):
    return 10 ** (db / 20)


def main_video_track(info):
    vt = [t for t in info["tracks"] if t["type"] == "video" and t["segments"]]
    return vt[0] if vt else None


def mat_index(info):
    return {m["id"]: (typ, m) for typ, lst in info["materials"].items() if isinstance(lst, list)
            for m in lst if isinstance(m, dict) and "id" in m}


def channel_levels(info):
    """主轨拼出的音频左右声道 RMS（dB）。单声道素材返回 (x, None)。"""
    err = _run(info, "astats=measure_perchannel=RMS_level:measure_overall=none")
    vals = [float(x) if x != "-inf" else -120.0 for x in re.findall(r"RMS level dB: (-?[\d.]+|-inf)", err)]
    return (vals[0], vals[1]) if len(vals) >= 2 else ((vals[0], None) if vals else (None, None))


def pick_mapping(info):
    """左右声道差 ≥ CH_DIFF dB 就当成单边录音：1 = 左声道填充至右侧，2 = 右声道填充至左侧，0 = 不动。"""
    l, r = channel_levels(info)
    if l is None or r is None or abs(l - r) < CH_DIFF:
        return 0, l, r
    return (1 if l > r else 2), l, r


def set_mapping(info, mapping):
    """给主轨每段设剪映「声道配置」（sound_channel_mappings 素材；没有就新建一个挂上）。"""
    idx = mat_index(info)
    for s in main_video_track(info)["segments"]:
        refs = [r for r in s.get("extra_material_refs", []) if idx.get(r, ("",))[0] == "sound_channel_mappings"]
        if refs:
            m = idx[refs[0]][1]
        else:
            m = {"id": new_id(), "type": "none"}
            info["materials"].setdefault("sound_channel_mappings", []).append(m)
            s.setdefault("extra_material_refs", []).append(m["id"])
        m["audio_channel_mapping"] = mapping
        m["is_config_open"] = bool(mapping)


def measure(info, mapping=0):
    """主轨按切点拼音频（原始音量、按片段倍速、按声道配置填充），返回 (LUFS, 真峰值 dBTP)。"""
    pan = {1: "pan=stereo|c0=c0|c1=c0,", 2: "pan=stereo|c0=c1|c1=c1,"}.get(mapping, "")
    err = _run(info, pan + "ebur128=peak=true")
    summ = err[err.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", summ)
    p = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summ)
    return (float(i.group(1)) if i else None), (float(p.group(1)) if p and p.group(1) != "-inf" else None)


def _run(info, filt):
    """主轨按切点拼音频后接 filt，返回 ffmpeg 的 stderr。"""
    tr = main_video_track(info)
    idx = mat_index(info)
    segs = sorted(tr["segments"], key=lambda s: s["target_timerange"].get("start", 0))
    parts, inputs, n = [], {}, 0
    for s in segs:
        typ, m = idx.get(s["material_id"], (None, {}))
        path = m.get("path")
        if not path or not Path(path).exists():
            continue
        if path not in inputs:
            inputs[path] = len(inputs)
        sp = 1.0
        for r in s.get("extra_material_refs", []):
            t2, m2 = idx.get(r, (None, {}))
            if t2 == "speeds":
                sp = m2.get("speed", 1.0) or 1.0
        a = s["source_timerange"].get("start", 0) / 1e6
        b = a + s["source_timerange"]["duration"] / 1e6
        tempo = "" if abs(sp - 1) < 1e-6 else f",atempo={sp}"
        parts.append(f"[{inputs[path]}:a]atrim={a:.4f}:{b:.4f},asetpts=PTS-STARTPTS{tempo}[a{n}]")
        n += 1
    if not n:
        return ""
    fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,{filt}[o]"
    cmd = [R.tool("ffmpeg"), "-nostats", "-hide_banner"]
    for p in inputs:
        cmd += ["-i", p]
    cmd += ["-filter_complex", fc, "-map", "[o]", "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=True).stderr


def plan(lufs, target=TARGET):
    """返回 (片段增益 dB, 复合片段增益 dB, 是否拉满后仍不够)。"""
    need = target - lufs
    inner = max(-MAX_DB, min(MAX_DB, need))
    outer = max(0.0, min(MAX_DB, need - MAX_DB))
    if outer < MIN_OUTER:
        outer = 0.0
    return inner, outer, need - inner - outer >= MIN_OUTER


def set_volume(segs, db):
    v = db2vol(db)
    for s in segs:
        s["volume"] = v
        s["last_nonzero_volume"] = v


def is_compound(info):
    tr = main_video_track(info)
    idx = mat_index(info)
    return len(tr["segments"]) == 1 and any(idx.get(r, ("",))[0] == "drafts" for r in tr["segments"][0].get("extra_material_refs", []))


def make_compound(info, dst: Path, outer_db, name="复合片段1"):
    """把主视频轨整条包成复合片段（剪映「新建复合片段」），复合片段音量 outer_db。"""
    tpl = json.loads(TEMPLATE.read_text(encoding="utf-8-sig"))
    tr = main_video_track(info)
    idx = mat_index(info)
    segs = sorted(tr["segments"], key=lambda s: s["target_timerange"].get("start", 0))
    total = max(s["target_timerange"].get("start", 0) + s["target_timerange"]["duration"] for s in segs)

    # 1) 片段用到的素材搬进子草稿（主草稿里没有别的轨再引用的才移走）
    used_inner = set()
    for s in segs:
        used_inner.add(s["material_id"])
        used_inner.update(s.get("extra_material_refs", []))
    used_other = set()
    for t in info["tracks"]:
        if t is tr:
            continue
        for s in t["segments"]:
            used_other.add(s["material_id"])
            used_other.update(s.get("extra_material_refs", []))
    sub_mats = copy.deepcopy(tpl["sub_materials"])
    for mid in used_inner:
        typ, m = idx.get(mid, (None, None))
        if typ is None:
            continue
        sub_mats.setdefault(typ, []).append(copy.deepcopy(m))
        if mid not in used_other:
            info["materials"][typ] = [x for x in info["materials"][typ] if x.get("id") != mid]

    sub_id = new_id()
    sub = copy.deepcopy(tpl["sub"])
    sub.update({"id": sub_id, "name": name, "duration": total,
                "canvas_config": copy.deepcopy(info.get("canvas_config")),
                "tracks": [{**copy.deepcopy(tr), "id": new_id()}], "materials": sub_mats})

    # 2) 外层：一个视频素材（空路径）+ drafts 条目 + 一段
    canvas = info.get("canvas_config") or {}
    vid = copy.deepcopy(tpl["video"])
    vid.update({"id": new_id(), "duration": total, "material_name": name,
                "width": canvas.get("width", vid.get("width")), "height": canvas.get("height", vid.get("height"))})
    ph = re.match(r"(##_draftpath_placeholder_[^#]+_##)", tpl["drafts"]["draft_file_path"]).group(1)
    dr = copy.deepcopy(tpl["drafts"])
    dr.update({"id": new_id(), "combination_id": new_id(), "draft": sub,
               "draft_file_path": f"{ph}/subdraft/{sub_id}/draft_content.json",
               "draft_cover_path": f"{ph}/subdraft/{sub_id}/draft_cover.jpg",
               "draft_config_path": f"{ph}/subdraft/{sub_id}/sub_draft_config.json"})
    # 外层片段的附属素材：照第一段复制一套新的（变速改回 1 倍）
    refs = []
    for r in segs[0].get("extra_material_refs", []):
        typ, m = idx.get(r, (None, None))
        if typ is None or typ == "drafts":
            continue
        m2 = copy.deepcopy(m)
        m2["id"] = new_id()
        if typ == "speeds":
            m2["speed"] = 1.0
            m2.pop("curve_speed", None)
        if typ == "sound_channel_mappings":   # 声道已在里面的片段上填充过，外层不再配
            m2["audio_channel_mapping"], m2["is_config_open"] = 0, False
        info["materials"].setdefault(typ, []).append(m2)
        refs.append(m2["id"])
    info["materials"]["videos"].append(vid)
    info["materials"].setdefault("drafts", []).append(dr)
    outer = copy.deepcopy(segs[0])
    v = db2vol(outer_db)
    outer.update({"id": new_id(), "material_id": vid["id"], "extra_material_refs": [dr["id"]] + refs,
                  "source_timerange": {"start": 0, "duration": total}, "target_timerange": {"start": 0, "duration": total},
                  "volume": v, "last_nonzero_volume": v, "speed": 1.0,
                  "clip": {"scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}, "flip": {}},
                  "common_keyframes": [], "keyframe_refs": []})
    tr["segments"] = [outer]

    # 3) 子草稿文件（剪映自己建复合片段时也会写这三个）
    sd = dst / "subdraft" / sub_id
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "draft_content.json").write_text(json.dumps(sub, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    cfg = dict(tpl["sub_config"])
    now = int(time.time())
    cfg.update({"id": sub_id, "project_id": sub_id, "name": name, "rough_cut_duration": total,
                "create_time": now, "import_time_ms": now * 1000})
    (sd / "sub_draft_config.json").write_text(json.dumps(cfg, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if (dst / "draft_cover.jpg").exists():
        shutil.copy2(dst / "draft_cover.jpg", sd / "draft_cover.jpg")


def apply(dst: Path, info, target=TARGET, dry=False):
    """量响度并改 info（就地）。返回一行报告。"""
    if is_compound(info):
        return "已经是复合片段，跳过", False
    mapping, l, r = pick_mapping(info)
    lufs, peak = measure(info, mapping)
    if lufs is None:
        return "量不出响度（找不到源素材？），跳过", False
    inner, outer, short = plan(lufs, target)
    ch = {1: "左声道填充至右侧", 2: "右声道填充至左侧"}.get(mapping)
    msg = (f"声道 左{l:.1f}/右{r:.1f} dB → {ch}；" if ch else "") + f"原始 {lufs:.1f} LUFS → 片段 {inner:+.1f} dB"
    if outer > 0:
        msg += f" + 复合片段 {outer:+.1f} dB（合计 {inner + outer:+.1f}）"
    msg += f" → 约 {lufs + inner + outer:.1f} LUFS"
    if short:
        msg += " ⚠ 已拉到上限仍不够"
    if not dry:
        if mapping:
            set_mapping(info, mapping)
        set_volume(main_video_track(info)["segments"], inner)
        if outer > 0:
            make_compound(info, dst, outer)
    return msg, True


def write_back(dst: Path, info):
    main_file = resolve(str(dst))
    R.ensure_editor_closed(dst)
    backup = dst / f".jy_backup_{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(exist_ok=True)
    shutil.copy2(main_file, backup / main_file.name)
    R.write_main_files(dst, info)
    (dst / ".jy_last_write").write_text(f"{main_file.stat().st_size}:{main_file.stat().st_mtime_ns}")
    return backup.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drafts", nargs="+")
    ap.add_argument("--target", type=float, default=TARGET)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    from jy_tighten import user_touched  # noqa: E402
    for name in a.drafts:
        main_f = resolve(name)
        dst = main_f.parent
        if not a.dry_run and user_touched(dst) and not a.force:
            print(f"⛔ {dst.name} 在上次写入后被剪映保存过（用户可能改过），跳过。确认可以改再加 --force。")
            continue
        info = json.loads(decrypt_text(main_f.read_text(encoding="utf-8-sig")))
        msg, changed = apply(dst, info, a.target, a.dry_run)
        if changed and not a.dry_run:
            bak = write_back(dst, info)
            msg += f"（备份 {bak}）"
        print(f"{'·' if a.dry_run else '✅'} {dst.name}：{msg}")


if __name__ == "__main__":
    main()
