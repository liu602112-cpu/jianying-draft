#!/usr/bin/env python3
"""从剪映时间线抽封面候选帧：时间线时刻 → 原片时刻，按清晰度挑候选，输出原画质 PNG + 拼图。
用法: jy_frames.py <draft_info.json> <输出目录> [--n 12] [--keywords 词1,词2] [--src-dir 原片目录]
"""
import argparse, json, os, re, subprocess, sys, io
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from pathlib import Path
import jy_runtime as R
from jy import decrypt_text, resolve
import jy_audio
HERE = os.path.dirname(os.path.abspath(__file__))

def run(cmd): return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8-sig", check=True).stdout

def segments(draft):
    f = resolve(draft)
    info = json.loads(decrypt_text(f.read_text(encoding="utf-8-sig")))
    idx = jy_audio._index(info)
    track = next((t for t in info["tracks"] if t["type"] == "video" and t["segments"]), None)
    segs = []
    for seg in track["segments"] if track else []:
        m = idx[seg["material_id"]][1]
        path = jy_audio.real_path(m.get("path", ""), f.parent)
        if not path:
            raise ValueError("封面抽帧暂不支持复合画面；请用原片或普通视频时间线抽帧。")
        tr, sr = seg["target_timerange"], seg.get("source_timerange") or {}
        sp = float(seg.get("speed") or next((idx[r][1].get("speed", 1.0) for r in seg.get("extra_material_refs", []) if idx.get(r, ("",))[0] == "speeds"), 1.0))
        segs.append(dict(t0=tr.get("start", 0)/1e6, t1=(tr.get("start", 0)+tr["duration"])/1e6,
                         name=path, s0=sr.get("start", 0)/1e6, sp=sp))
    return segs

def subs(draft):
    import tempfile
    with tempfile.TemporaryDirectory(prefix="jy_frames_") as tmp:
        srt = os.path.join(tmp, "subtitles.srt")
        run([sys.executable, os.path.join(HERE, "jy.py"), "srt", draft, "-o", srt])
        content = Path(srt).read_text(encoding="utf-8-sig")
    items = []
    for blk in content.strip().split("\n\n"):
        L = blk.split("\n")
        if len(L) < 3: continue
        a, b = L[1].split(" --> ")
        f = lambda s: int(s[:2])*3600+int(s[3:5])*60+float(s[6:].replace(",", "."))
        items.append((f(a), f(b), "".join(L[2:])))
    return items

def to_src(segs, t):
    for s in segs:
        if s["t0"] <= t < s["t1"]:
            return s["name"], s["s0"] + (t - s["t0"]) * s["sp"]
    return None, None

def grab(path, ts, w=None):
    vf = ["-vf", f"scale={w}:-2"] if w else []
    raw = subprocess.run([R.tool("ffmpeg"), "-v", "error", "-ss", f"{ts:.3f}", "-i", path, "-frames:v", "1", *vf, "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(raw)).convert("RGB")

def sharp(img):
    a = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = a.shape; a = a[int(h*.1):int(h*.6), int(w*.15):int(w*.85)]  # 脸部区域
    lap = a[1:-1,1:-1]*4 - a[:-2,1:-1] - a[2:,1:-1] - a[1:-1,:-2] - a[1:-1,2:]
    return float(lap.var())

def font(sz):
    p = R.font("msyh.ttc", "simhei.ttf")
    return ImageFont.truetype(p, sz) if p and Path(p).is_file() else ImageFont.load_default()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft"); ap.add_argument("out")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--keywords", default="")
    ap.add_argument("--src-dir", default="", help="原片移动后用于按文件名查找的目录（通常自动使用草稿里的路径）")
    ap.add_argument("--min-gap", type=float, default=4.0)
    a = ap.parse_args()
    if a.n <= 0:
        ap.error("--n 必须大于 0")
    os.makedirs(a.out, exist_ok=True)
    segs, items = segments(a.draft), subs(a.draft)
    for seg in segs:
        if not Path(seg["name"]).is_file() and a.src_dir:
            seg["name"] = str(Path(a.src_dir) / Path(seg["name"]).name)
    kws = [k for k in a.keywords.split(",") if k]
    cands = []
    for (b, e, txt) in items:
        best = None
        for frac in (.25, .5, .75):
            t = b + (e - b) * frac
            name, st = to_src(segs, t)
            if name is None: continue
            sc = sharp(grab(name, st, 270))
            if not best or sc > best[0]: best = (sc, t, name, st)
        if best:
            bonus = 2.0 if any(k in txt for k in kws) else 1.0
            cands.append(dict(score=best[0]*bonus, t=best[1], name=best[2], st=best[3], txt=txt, kw=bonus > 1))
    cands.sort(key=lambda c: -c["score"])
    pick = []
    for c in cands:
        if all(abs(c["t"] - p["t"]) >= a.min_gap for p in pick): pick.append(c)
        if len(pick) >= a.n: break
    pick.sort(key=lambda c: c["t"])
    if not pick:
        sys.exit("没有可抽取的字幕画面；请检查字幕和原片路径。")
    thumbs = []
    for i, c in enumerate(pick, 1):
        img = grab(c["name"], c["st"])
        m, s = divmod(c["t"], 60)
        safe = re.sub(r'[\\/:*?"<>|\s]', "", c["txt"])[:20]
        fn = f"{i:02d}_{int(m)}m{s:04.1f}s_{safe}.png"
        img.save(os.path.join(a.out, fn)); c["file"] = fn
        thumbs.append(ImageOps.pad(img, (270, 480), color="white"))
    cols = 6; rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols*270, rows*(480+46)), "white"); d = ImageDraw.Draw(sheet); f = font(16)
    for i, (th, c) in enumerate(zip(thumbs, pick)):
        x, y = (i % cols)*270, (i // cols)*(480+46)
        sheet.paste(th, (x, y))
        d.text((x+6, y+484), f"#{i+1} {c['t']:.1f}s{' ★' if c['kw'] else ''}", fill="black", font=f)
        d.text((x+6, y+504), c["txt"][:15], fill="black", font=f)
    sheet.save(os.path.join(a.out, "_contact.jpg"), quality=85)
    Path(a.out, "_picks.json").write_text(json.dumps(pick, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(items)} 句字幕 → {len(pick)} 候选 → {a.out}")

if __name__ == "__main__": main()
