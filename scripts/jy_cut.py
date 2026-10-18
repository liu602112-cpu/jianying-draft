#!/usr/bin/env python3
"""
按时间段把一个剪映草稿切成多个新草稿（原草稿不动）。

用法：
  python3 jy_cut.py spec.json [--dry-run]

spec.json：
{
  "source": "0926直播",                       // 草稿名或草稿目录
  "clips": [
    {"name": "0926切1_标题", "ranges": [[718.35, 721.733], [662.2, 783.0]]},
    ...
  ]
}
ranges 用源草稿时间线上的秒数，按列表顺序首尾拼接（可以用来做「金句前置」）。
非文字轨（视频/音频/贴纸…）按区间裁剪；文字轨只保留完整落在区间内的条目，
所以区间端点最好对齐字幕的起止时间。

写入前请先关闭剪映。每个新草稿会：复制原目录（不含 .backup）→ 换新 draft_id / 时间线 id →
写入裁剪后的 draft_info → 登记到 root_meta_info.json（自动备份）。
"""

import argparse
import copy
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
from jy import decrypt_text, encrypt_text, resolve  # noqa: E402

US = 1_000_000
MIN_SEG = 34_000  # 小于 1 帧的碎片丢掉


def new_id():
    return str(uuid.uuid4()).upper()


def is_encrypted(p: Path):
    return p.read_text(encoding="utf-8-sig").lstrip("﻿ \n")[:1] not in ("{", "[")


def build(d: dict, ranges):
    """返回裁剪后的新 draft_info dict。"""
    lookup = {}
    for kind, items in d["materials"].items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and "id" in m:
                    lookup[m["id"]] = (kind, m)
    seg_refs = {r for t in d["tracks"] for s in t["segments"]
                for r in [s["material_id"], *s.get("extra_material_refs", [])]}

    out = copy.deepcopy({k: v for k, v in d.items() if k not in ("tracks", "materials")})
    mats = {k: ([m for m in v if not (isinstance(m, dict) and m.get("id") in seg_refs)]
                if isinstance(v, list) else copy.deepcopy(v))
            for k, v in d["materials"].items()}

    def clone_mat(mid):
        kind, m = lookup[mid]
        c = copy.deepcopy(m)
        c["id"] = new_id()
        mats[kind].append(c)
        return c["id"]

    def clone_seg(seg, start, dur, src_offset=None):
        s = copy.deepcopy(seg)
        s["id"] = new_id()
        s["material_id"] = clone_mat(seg["material_id"])
        s["extra_material_refs"] = [clone_mat(r) for r in seg.get("extra_material_refs", [])]
        s["target_timerange"] = {"start": start, "duration": dur}
        if src_offset is not None and s.get("source_timerange"):
            sp = seg.get("speed") or 1.0  # 变速片段：源文件上的偏移和时长都要乘倍速
            s["source_timerange"] = {"start": s["source_timerange"].get("start", 0) + round(src_offset * sp),
                                     "duration": round(dur * sp)}
        return s

    tracks = []
    for t in d["tracks"]:
        nt = copy.deepcopy({k: v for k, v in t.items() if k != "segments"})
        nt["id"] = new_id()
        nt["segments"] = []
        cursor = 0
        for a, b in ranges:
            for seg in t["segments"]:
                tr = seg["target_timerange"]
                s, e = tr.get("start", 0), tr.get("start", 0) + tr["duration"]
                if t["type"] == "text":
                    if s >= a - 10_000 and e <= b + 10_000:
                        ns = max(s, a)
                        nt["segments"].append(clone_seg(seg, cursor + ns - a, min(e, b) - ns))
                else:
                    ns, ne = max(s, a), min(e, b)
                    if ne - ns >= MIN_SEG:
                        nt["segments"].append(clone_seg(seg, cursor + ns - a, ne - ns, src_offset=ns - s))
            cursor += b - a
        nt["segments"].sort(key=lambda x: x["target_timerange"]["start"])
        tracks.append(nt)

    out["tracks"] = tracks
    out["materials"] = mats
    out["duration"] = sum(b - a for a, b in ranges)
    if isinstance(out.get("extra_info"), dict):
        out["extra_info"]["subtitle_fragment_info_list"] = []  # 旧时间线上的识别缓存，位置已失效
    return out


def write_draft(src_dir: Path, info: dict, name: str, raw_info: str, output_root=None):
    root = Path(output_root).resolve() if output_root else src_dir.parent.resolve()
    dst = R.safe_child(root, name)
    R.ensure_editor_closed(dst)
    if dst.exists():
        sys.exit(f"已存在同名草稿：{dst}")
    source_main = resolve(str(src_dir))
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns(".backup", ".jy_backup_*"))
    old_tl, new_tl = info["id"], new_id()
    info["id"] = new_tl
    tl_dir = dst / "Timelines" / old_tl
    if tl_dir.exists():
        tl_dir.rename(dst / "Timelines" / new_tl)
    R.write_main_files(dst, info, source_main.name, raw_info)
    now_us = int(time.time() * US)
    proj_id = new_id()
    for fn in ("Timelines/project.json", "Timelines/project.json.bak", "timeline_layout.json"):
        file = dst / fn
        if not file.exists():
            continue
        text = file.read_text(encoding="utf-8-sig").replace(old_tl, new_tl)
        if fn.startswith("Timelines/project"):
            project = json.loads(text)
            project.update(id=proj_id, create_time=now_us, update_time=now_us)
            for timeline in project.get("timelines", []):
                timeline["create_time"] = timeline["update_time"] = now_us
            text = json.dumps(project, ensure_ascii=False, separators=(",", ":"))
        file.write_text(text, encoding="utf-8")
    meta_file = dst / "draft_meta_info.json"
    meta_raw = meta_file.read_text(encoding="utf-8-sig")
    meta = json.loads(decrypt_text(meta_raw))
    draft_id = new_id()
    meta.update(draft_id=draft_id, draft_name=name, draft_fold_path=dst.as_posix(),
                tm_duration=info["duration"], tm_draft_create=now_us, tm_draft_modified=now_us)
    plain = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    meta_file.write_text(plain if meta_raw.lstrip().startswith(("{", "[")) else encrypt_text(plain), encoding="utf-8")
    back = json.loads(decrypt_text(resolve(str(dst)).read_text(encoding="utf-8-sig")))
    assert back["duration"] == info["duration"] and back["id"] == new_tl
    return dst, draft_id, meta


def register(root: Path, entries):
    R.ensure_editor_closed(root)
    f = root / "root_meta_info.json"
    if not f.exists():
        print("⚠️ 没有 root_meta_info.json，跳过登记（剪映可能需要手动导入）")
        return
    bak = f.with_name(f.name + time.strftime(".%Y%m%d-%H%M%S.bak"))
    shutil.copy2(f, bak)
    try:
        rm = json.loads(f.read_text(encoding="utf-8-sig"))
    except ValueError:
        print("⚠️ root_meta_info.json 为空或损坏（新版剪映可能不再用它），跳过登记")
        return
    store = rm.setdefault("all_draft_store", [])
    for src_name, dst, draft_id, meta in entries:
        tpl = next((x for x in store if x.get("draft_name") == src_name), None) or (store[0] if store else {})
        e = copy.deepcopy(tpl)
        e.update(draft_id=draft_id, draft_name=meta["draft_name"], draft_fold_path=str(dst),
                 draft_cover=str(dst / "draft_cover.jpg"), draft_json_file=resolve(str(dst)).as_posix(),
                 tm_duration=meta["tm_duration"], tm_draft_create=meta["tm_draft_create"],
                 tm_draft_modified=meta["tm_draft_modified"], draft_root_path=str(root))
        store.insert(0, e)
        if isinstance(rm.get("draft_ids"), int):
            rm["draft_ids"] += 1
    f.write_text(json.dumps(rm, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"已登记 {len(entries)} 个草稿到 root_meta_info.json（备份：{bak.name}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--dry-run", action="store_true", help="只打印每条的片段和字幕，不写文件")
    a = ap.parse_args()
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8-sig"))
    src_file = resolve(spec["source"])
    src_dir = src_file.parent
    raw = src_file.read_text(encoding="utf-8-sig")
    d = json.loads(decrypt_text(raw))

    made = []
    for c in spec["clips"]:
        ranges = [(int(x * US), int(y * US)) for x, y in c["ranges"]]
        info = build(d, ranges)
        texts = [json.loads(m["content"]).get("text", "") for t in info["tracks"] if t["type"] == "text"
                 for s in t["segments"] for m in info["materials"]["texts"] if m["id"] == s["material_id"]]
        nseg = " ".join(f"{t['type']}×{len(t['segments'])}" for t in info["tracks"] if t["segments"])
        print(f"\n▶ {c['name']}  {info['duration'] / US:.1f}s  片段 {nseg}")
        print("  首：" + " / ".join(texts[:3]))
        print("  尾：" + " / ".join(texts[-3:]))
        if not a.dry_run:
            dst, draft_id, meta = write_draft(src_dir, info, c["name"], raw)
            made.append((src_dir.name, dst, draft_id, meta))
            print(f"  ✅ {dst}")
    if made:
        register(src_dir.parent, made)


if __name__ == "__main__":
    main()
