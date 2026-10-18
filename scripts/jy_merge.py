#!/usr/bin/env python3
"""
把多个单时间线草稿合并成一个多时间线草稿：每个源草稿变成新草稿里的一条时间线，时间线名自定。
源草稿不动。新草稿以第一个源草稿为底复制，第一条时间线作为主时间线。

用法：
  python3 jy_merge.py 合集名 草稿1 草稿2 ... [--names 名1 名2 ...] [--dry-run]

结构（剪映 6+ 多时间线）：
  Timelines/project.json        登记所有时间线（id / name），main_timeline_id 指主时间线
  Timelines/<id>/draft_info.json 每条时间线自己的主文件，文件内 "id" 必须等于目录名
  timeline_layout.json          时间线标签页的顺序与当前激活的时间线
  根目录 draft_info.json         主时间线的副本
写入前关闭剪映。
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
from jy import decrypt_text, encrypt_text, resolve  # noqa: E402
from jy_cut import new_id, register, is_encrypted  # noqa: E402

US = 1_000_000
TL_FILES = ("draft_info.json", "draft_info.json.bak", "template-2.tmp")


def load(p: Path):
    raw = p.read_text(encoding="utf-8-sig")
    return json.loads(decrypt_text(raw) if is_encrypted(p) else raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("drafts", nargs="+")
    ap.add_argument("--names", nargs="*", help="每条时间线的名字，默认用源草稿名")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true", help="同名合集已存在时，先挪到 .jy_backup_合集名_时间戳 再重建")
    a = ap.parse_args()

    srcs = [resolve(x).parent.resolve() for x in a.drafts]  # 绝对路径：登记和 draft_fold_path 都要用
    names = a.names or [s.name for s in srcs]
    if len(names) != len(srcs):
        sys.exit("--names 数量要和草稿数量一致")
    root = srcs[0].parent
    dst = R.safe_child(root, a.name)
    if dst.exists() and not a.replace:
        sys.exit(f"已存在同名草稿：{dst}（重建加 --replace）")

    main_name = resolve(str(srcs[0])).name
    infos = [load(resolve(str(s))) for s in srcs]
    for s, n, i in zip(srcs, names, infos):
        print(f"  {n:<40} {i['duration'] / US:7.1f}s  ← {s.name}")
    if a.dry_run:
        return

    R.ensure_editor_closed(dst)
    if dst.exists():
        bak = root / f".jy_backup_{a.name}_{time.strftime('%Y%m%d-%H%M%S')}"
        dst.rename(bak)
        rf = root / "root_meta_info.json"
        if rf.exists():  # 去掉旧合集的登记，避免草稿列表里出现两条
            rm = json.loads(rf.read_text(encoding="utf-8-sig"))
            rm["all_draft_store"] = [x for x in rm.get("all_draft_store", []) if x.get("draft_fold_path") != str(dst)]
            rf.write_text(json.dumps(rm, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"旧合集已挪到 {bak.name}")
    shutil.copytree(srcs[0], dst, ignore=shutil.ignore_patterns(".backup", ".jy_backup_*"))
    tl_root = dst / "Timelines"
    tl_root.mkdir(exist_ok=True)
    for p in tl_root.iterdir():          # 清掉底稿自带的时间线目录，下面按新 id 重建
        if p.is_dir():
            if not p.resolve().is_relative_to(dst.resolve()):
                raise ValueError("拒绝清理草稿目录外的路径")
            shutil.rmtree(p)

    now_us = int(time.time() * US)
    tls = []
    for s, n, info in zip(srcs, names, infos):
        tid = new_id()
        info["id"] = tid
        src_tl = s / "Timelines" / load(resolve(str(s)))["id"]
        tdir = tl_root / tid
        if src_tl.is_dir():
            shutil.copytree(src_tl, tdir)
        else:
            tdir.mkdir()
        R.write_main_files(tdir, info, main_name=main_name,
                           raw=resolve(str(s)).read_text(encoding="utf-8-sig"))
        tls.append({"create_time": now_us, "id": tid, "is_marked_delete": False, "name": n, "update_time": now_us})

    main_id = tls[0]["id"]
    R.write_main_files(dst, infos[0], main_name=main_name,
                       raw=resolve(str(srcs[0])).read_text(encoding="utf-8-sig"))

    pf = tl_root / "project.json"
    pj = json.loads(pf.read_text(encoding="utf-8-sig")) if pf.exists() else {"config": {}, "version": 0}
    pj.update(id=new_id(), create_time=now_us, update_time=now_us, main_timeline_id=main_id, timelines=tls)
    s = json.dumps(pj, ensure_ascii=False, separators=(",", ":"))
    pf.write_text(s, encoding="utf-8")
    (tl_root / "project.json.bak").write_text(s, encoding="utf-8")

    layout = {"activeTimeline": main_id,
              "dockItems": [{"dockIndex": 0, "ratio": 1, "timelineIds": [t["id"] for t in tls],
                             "timelineNames": [t["name"] for t in tls]}],
              "layoutOrientation": 1}
    (dst / "timeline_layout.json").write_text(json.dumps(layout, ensure_ascii=False, separators=(",", ":")),
                                              encoding="utf-8-sig")

    meta_f = dst / "draft_meta_info.json"
    draft_id = new_id()
    enc_meta = is_encrypted(meta_f)
    meta = load(meta_f)
    meta.update(draft_id=draft_id, draft_name=a.name, draft_fold_path=str(dst),
                tm_duration=infos[0]["duration"], tm_draft_create=now_us, tm_draft_modified=now_us)
    plain = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    meta_f.write_text(encrypt_text(plain) if enc_meta else plain, encoding="utf-8")

    for t in tls:  # 自检：每条时间线能解密、id 与目录一致
        assert load(tl_root / t["id"] / main_name)["id"] == t["id"]
    register(root, [(srcs[0].name, dst, draft_id, meta)])
    print(f"✅ {dst}  共 {len(tls)} 条时间线")


if __name__ == "__main__":
    main()
