#!/usr/bin/env python3
"""
剪映草稿读取工具（解密 / 摘要 / 导字幕 / 回写加密）

用法：
  python3 jy.py list [-n 20]                     列出本机最近的剪映草稿
  python3 jy.py decrypt <草稿> [-o out.json]      解密成格式化 JSON
  python3 jy.py summary <草稿> [--texts 30]       人话摘要：画布/时长/轨道/片段/素材缺失
  python3 jy.py srt <草稿> [-o out.srt] [--txt]   字幕轨导出 SRT（--txt 导纯文本逐字稿）
  python3 jy.py encrypt <json> -o <草稿文件>       把改好的 JSON 加密写回（自动备份原文件）

<草稿> 可以是：草稿名（如 0926直播）/ 草稿目录 / 具体文件（draft_info.json、draft_content.json、
draft_meta_info.json 等，明文或加密均可）。
"""

import argparse
import base64
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

import jy_runtime as R

try:
    from Crypto.Cipher import AES
except ImportError:
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        sys.exit("缺少依赖：python3 -m pip install --user pycryptodome")

DRAFT_ROOTS = R.draft_roots()
MAIN_FILES = ["draft_content.json", "draft_info.json"]  # 新版 / 旧版主文件名
US = 1_000_000  # 剪映时间单位：微秒

# ---------------------------------------------------------------- 加解密
# 剪映 6+：密文 = Base64(AES-GCM(ct || tag16))，再把 48 字符 keyIv 按下列偏移分 12 块（每块 4 字符）插进 Base64 串。
# key = keyIv[:32] 的 ASCII，iv = keyIv[32:48] 的 ASCII。
_OFFSETS = [0, 7, 20, 33, 40, 47, 59, 66, 76, 89, 99, 127]


def _extract_key_iv(s: str):
    chars = list(s)
    key_iv = []
    for off in reversed(_OFFSETS):
        key_iv[:0] = chars[off:off + 4]
        del chars[off:off + 4]
    return "".join(key_iv), "".join(chars)


def _insert_key_iv(b64: str, key_iv: str):
    chars = list(b64)
    for i, off in enumerate(_OFFSETS):
        chars[off:off] = key_iv[i * 4:i * 4 + 4]
    return "".join(chars)


def decrypt_text(raw: str) -> str:
    s = raw.strip().lstrip("﻿")
    if s[:1] in ("{", "["):
        return s  # 明文草稿（旧版剪映 / CapCut）
    key_iv, body = _extract_key_iv("".join(s.split()))
    data = base64.b64decode(body)
    cipher = AES.new(key_iv[:32].encode(), AES.MODE_GCM, nonce=key_iv[32:48].encode())
    return cipher.decrypt_and_verify(data[:-16], data[-16:]).decode("utf-8")


def encrypt_text(plain: str) -> str:
    table = "0123456789abcdefghijABCDEFGHIJ"
    key_iv = "".join(random.choice(table) for _ in range(48))
    cipher = AES.new(key_iv[:32].encode(), AES.MODE_GCM, nonce=key_iv[32:].encode())
    ct, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
    return _insert_key_iv(base64.b64encode(ct + tag).decode(), key_iv)


# ---------------------------------------------------------------- 定位草稿
def resolve(target: str) -> Path:
    p = Path(target).expanduser()
    if not p.exists():
        for root in DRAFT_ROOTS:
            if (root / target).exists():
                p = root / target
                break
        else:
            sys.exit(f"找不到草稿：{target}（可用 `jy.py list` 查看草稿名）")
    if p.is_dir():
        for name in MAIN_FILES:
            if (p / name).exists():
                return p / name
        sys.exit(f"{p} 下没有 {' / '.join(MAIN_FILES)}")
    return p


def load(target: str):
    path = resolve(target)
    try:
        return path, json.loads(decrypt_text(path.read_text(encoding="utf-8")))
    except Exception as e:
        sys.exit(f"解密失败：{path}\n  {type(e).__name__}: {e}\n"
                 f"  可能是新加密格式，当前剪映版本暂不支持")


# ---------------------------------------------------------------- 工具函数
def fmt(us, srt=False):
    us = int(us or 0)
    h, rem = divmod(us, 3600 * US)
    m, rem = divmod(rem, 60 * US)
    s, rem = divmod(rem, US)
    ms = rem // 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def text_of(mat: dict) -> str:
    c = mat.get("content", "")
    try:
        t = json.loads(c).get("text", "")
    except Exception:
        t = c
        if t.startswith("<") and "[" in t:  # 旧版 <font ...>[文字]</font>
            t = t[t.find("[") + 1:t.rfind("]")]
    return (t or mat.get("recognize_text", "")).strip()


def material_index(d: dict) -> dict:
    idx = {}
    for kind, items in d.get("materials", {}).items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and "id" in m:
                    idx[m["id"]] = (kind, m)
    return idx


def text_segments(d: dict, track_no=None):
    idx = material_index(d)
    out = []
    tracks = [t for t in d.get("tracks", []) if t.get("type") == "text"]
    for i, tr in enumerate(tracks):
        if track_no is not None and i != track_no:
            continue
        for seg in tr.get("segments", []):
            kind, mat = idx.get(seg.get("material_id"), (None, {}))
            if kind != "texts":
                continue
            tr_ = seg.get("target_timerange", {})
            start = tr_.get("start", 0)
            out.append((start, start + tr_.get("duration", 0), text_of(mat), i))
    return sorted(out)


# ---------------------------------------------------------------- 子命令
def cmd_list(a):
    rows = []
    for root in DRAFT_ROOTS:
        if root.exists():
            for p in root.iterdir():
                if p.is_dir() and any((p / n).exists() for n in MAIN_FILES):
                    rows.append((p.stat().st_mtime, p))
    rows.sort(reverse=True)
    for mt, p in rows[:a.n]:
        print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))}  {p.name}")
    if rows:
        print(f"\n共 {len(rows)} 个草稿，目录：{rows[0][1].parent}")


def cmd_decrypt(a):
    path, d = load(a.target)
    out = Path(a.output) if a.output else Path.cwd() / f"{path.parent.name}_{path.stem}.json"
    out.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ {path} → {out}  ({out.stat().st_size // 1024} KB)")


def cmd_summary(a):
    path, d = load(a.target)
    idx = material_index(d)
    cv = d.get("canvas_config", {})
    print(f"# {path.parent.name}  ({path.name})")
    fps = f" · fps {d['fps']}" if d.get("fps") else ""
    print(f"画布 {cv.get('width')}x{cv.get('height')}{fps} · 总时长 {fmt(d.get('duration'))}"
          f" · 版本 {d.get('new_version') or d.get('version')}")
    mats = {k: len(v) for k, v in d.get("materials", {}).items() if isinstance(v, list) and v}
    print("素材计数：" + "、".join(f"{k} {v}" for k, v in mats.items()))

    missing = set()
    for kind in ("videos", "audios"):
        for m in d.get("materials", {}).get(kind, []):
            fp = m.get("path", "")
            if fp and not fp.startswith("##") and not os.path.exists(fp):
                missing.add(fp)

    for ti, tr in enumerate(d.get("tracks", [])):
        segs = tr.get("segments", [])
        ttype = tr.get("type")
        print(f"\n## 轨道 {ti} · {ttype} · {len(segs)} 段" + (f" · {tr['name']}" if tr.get("name") else ""))
        if ttype == "text":
            for seg in segs[:a.texts]:
                kind, mat = idx.get(seg.get("material_id"), (None, {}))
                t = seg.get("target_timerange", {})
                print(f"  {fmt(t.get('start'))}  {text_of(mat) if kind == 'texts' else '(' + str(kind) + ')'}")
            if len(segs) > a.texts:
                print(f"  …… 其余 {len(segs) - a.texts} 条（用 srt 子命令导全量）")
            continue
        for seg in segs:
            kind, mat = idx.get(seg.get("material_id"), (None, {}))
            t = seg.get("target_timerange", {})
            s = seg.get("source_timerange") or {}
            start, dur = t.get("start", 0), t.get("duration", 0)
            name = mat.get("material_name") or mat.get("name") or Path(mat.get("path", "")).name or kind
            src = f" 源 {fmt(s.get('start'))}+{dur_s(s.get('duration'))}" if s else ""
            spd = f" ×{seg['speed']}" if seg.get("speed", 1) != 1 else ""
            print(f"  {fmt(start)} → {fmt(start + dur)}  [{kind}] {name}{src}{spd}")

    if missing:
        print(f"\n⚠️ 本机找不到的素材 {len(missing)} 个：")
        for fp in sorted(missing):
            print(f"  {fp}")


def dur_s(us):
    return f"{(us or 0) / US:.2f}s"


def cmd_srt(a):
    path, d = load(a.target)
    segs = text_segments(d, a.track)
    if not segs:
        sys.exit("草稿里没有文字/字幕轨")
    if a.txt:
        body = "\n".join(t for _, _, t, _ in segs if t)
    else:
        body = "\n".join(f"{i}\n{fmt(s, True)} --> {fmt(e, True)}\n{t}\n"
                         for i, (s, e, t, _) in enumerate(segs, 1))
    if a.output:
        Path(a.output).write_text(body, encoding="utf-8")
        print(f"✅ {len(segs)} 条 → {a.output}")
    else:
        print(body)


def cmd_encrypt(a):
    src = Path(a.json).expanduser()
    plain = json.dumps(json.loads(src.read_text(encoding="utf-8")), ensure_ascii=False, separators=(",", ":"))
    dst = Path(a.output).expanduser()
    R.ensure_editor_closed(dst)
    if dst.exists():
        bak = dst.with_name(dst.name + time.strftime(".%Y%m%d-%H%M%S.bak"))
        shutil.copy2(dst, bak)
        print(f"已备份原文件 → {bak}")
    enc = plain if a.plain else encrypt_text(plain)
    dst.write_text(enc, encoding="utf-8")
    assert json.loads(decrypt_text(dst.read_text(encoding="utf-8"))) == json.loads(plain)
    print(f"✅ 已写回并校验可解密：{dst}（写回前请先关闭剪映，否则会被覆盖）")


def main():
    ap = argparse.ArgumentParser(description="剪映草稿读取工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("-n", type=int, default=20); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("decrypt"); p.add_argument("target"); p.add_argument("-o", "--output"); p.set_defaults(fn=cmd_decrypt)
    p = sub.add_parser("summary"); p.add_argument("target"); p.add_argument("--texts", type=int, default=30); p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("srt"); p.add_argument("target"); p.add_argument("-o", "--output")
    p.add_argument("--txt", action="store_true", help="导纯文本逐字稿"); p.add_argument("--track", type=int, help="只导第 N 条文字轨（从 0 起）")
    p.set_defaults(fn=cmd_srt)
    p = sub.add_parser("encrypt"); p.add_argument("json"); p.add_argument("-o", "--output", required=True)
    p.add_argument("--plain", action="store_true", help="写明文不加密（旧版剪映 / CapCut）"); p.set_defaults(fn=cmd_encrypt)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
