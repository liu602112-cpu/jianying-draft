#!/usr/bin/env python3
"""
给剪映草稿的字幕统一套用个人字幕预设（presets/subtitle_style.json）。

用法：
  python3 jy_style.py <草稿名或目录> [...] [--preset 文件] [--dry-run] [--force]

只改字幕的样式（字体、加粗、字号、阴影、底框、位置），不改文字和时间。
写入前要关闭剪映；草稿在我们上次写入后被剪映保存过（用户改过）就拒绝，除非 --force。
jy_tighten.py 生成字幕时也会默认套用同一份预设（spec 里 "subtitle_style": false 可关闭）。
"""

import argparse
import copy
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
from jy import decrypt_text, encrypt_text, resolve  # noqa: E402

PRESET = Path(__file__).parent.parent / "presets" / "subtitle_style.json"


def load_preset(path=None):
    return R.adapt_paths(json.loads(Path(path or PRESET).read_text(encoding="utf-8-sig")))


def apply_to_material(m, preset):
    """改一条文本素材：顶层样式字段 + content 里的单段样式（range 按文字长度）。文字不变。"""
    m.update(copy.deepcopy(preset["fields"]))
    c = json.loads(m["content"])
    text = c.get("text", "")
    st = copy.deepcopy(preset["style"])
    st["range"] = [0, len(text.encode("utf-16-le")) // 2]
    c["styles"] = [st]
    m["content"] = json.dumps(c, ensure_ascii=False, separators=(",", ":"))


def apply_to_draft(info, preset):
    """套用到草稿里所有字幕轨（type=subtitle 的文本素材）。返回改动条数。"""
    subs = {m["id"]: m for m in info["materials"].get("texts", []) if m.get("type") == "subtitle"}
    n = 0
    for t in info["tracks"]:
        if t["type"] != "text":
            continue
        for seg in t["segments"]:
            m = subs.get(seg["material_id"])
            if m is None:
                continue
            apply_to_material(m, preset)
            if preset.get("clip"):
                seg["clip"] = copy.deepcopy(preset["clip"])
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drafts", nargs="+")
    ap.add_argument("--preset")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    preset = load_preset(a.preset)
    from jy_tighten import user_touched  # noqa: E402
    for name in a.drafts:
        main_f = resolve(name)
        dst = main_f.parent
        if user_touched(dst) and not a.force:
            print(f"⛔ {dst.name} 在上次写入后被剪映保存过（用户可能改过），跳过。确认可以改再加 --force。")
            continue
        info = json.loads(decrypt_text(main_f.read_text(encoding="utf-8-sig")))
        n = apply_to_draft(info, preset)
        if a.dry_run:
            print(f"· {dst.name}：{n} 条字幕将套用预设")
            continue
        bak = dst / f".jy_backup_{time.strftime('%Y%m%d-%H%M%S')}"
        bak.mkdir(exist_ok=True)
        shutil.copy2(main_f, bak / main_f.name)
        R.write_main_files(dst, info)
        (dst / ".jy_last_write").write_text(f"{main_f.stat().st_size}:{main_f.stat().st_mtime_ns}")
        print(f"✅ {dst.name}：{n} 条字幕已套用预设（原文件备份在 {bak.name}）")


if __name__ == "__main__":
    main()
