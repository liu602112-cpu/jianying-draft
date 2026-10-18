#!/usr/bin/env python3
"""
按字精剪：删气口 / 口误 / 重复字，改字幕错别字，压缩停顿，生成（或覆盖）剪映草稿。

用法：
  python3 jy_tighten.py spec.json [--dry-run]

spec.json：
{
  "source": "0926直播",                      // 源草稿（只读）
  "max_gap": 0.30,                          // 相邻保留字之间超过该秒数的停顿会被压缩
  "clips": [
    {"name": "0926切1_标题", "ranges": [[718.35, 721.733], [662.2, 783.0]]}   // 已存在则覆盖（先备份），否则新建
  ],
  "edits": {                                 // 按字幕在源时间线上的起始秒（两位小数）索引，所有 clip 共用
    "662.33": "[但是]|[内容]|[就]|明年",       // 逐字写出该句的分词，数量必须与原句一致
    "706.77": "-",                            // 整句删除（音频+字幕）
    "705.57": "开|一个|20|差{200刀}|的"         // tok{新字} 只改字幕显示、保留音频；{} 为空表示不显示
  }
}
分词标记：[tok] 删除该字（剪掉音频并去掉字幕）；tok{new} 改字；␣ 表示空格分词。
先用 --dry-run 打印每条精剪后的字幕与时长，核对无误再写入。写入前关闭剪映。

—— 断句模式（推荐，直播切片用）——
clip 里给 "script": "切1.txt"（相对 spec 的路径）时，这条不再用 edits，而是按稿子重新断字幕、按标点定停顿：
  1) python3 jy_tighten.py spec.json --dump 切1      导出这条的逐字骨架（每行一句原字幕，=== 分隔 range）
  2) 在骨架上改：删字 [tok]、改字 tok{new}、整行删 在行首加 ~；按语义重新换行（一行 = 一条字幕），
     行尾或词后写标点：「，」= 半句停顿（短），「。？！」= 整句停顿（稍长）；行尾不写标点按「，」算。
     字可以跨行挪动（例：「网站|点」「一次|生图」→「网站，」「点|一次|生图」），但字的先后顺序不能变。
  3) --dry-run 看结果 → 正式写入。
字幕显示：行尾「，。」去掉，「？！」保留；行内「，。」显示成空格。
停顿按音频真实静音量：句内 > max_gap 压到 word_pause；逗号压到 comma_pause；句号压到 sent_pause（均可在 spec 里改）。
"edge_pre"/"edge_post"（秒，默认 1.0）：range 首字往前找开口、尾字往后等收尾的最远距离；区间外紧挨着别的话时调小（如 0.25/0.3）。
"loudness_target"：默认 false，保留源音量和声道；当前用户要求响度处理时才设定 LUFS 目标（见 jy_loud.py）。
"subtitle_style"：默认 true，字幕套用个人预设 presets/subtitle_style.json（思源黑体加粗 8 号、黑阴影、30% 黑底框）；false 保留源字幕样式，也可给一个预设文件路径。
"words_file"：外部逐字时间 JSON（jy_words.py 生成），只补没有逐字时间的字幕。
"glossary": {"deepseek": "DeepSeek"} 按分词统一写法（不区分大小写，只改显示）。
"""

import argparse
import array
import copy
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import jy_runtime as R
from jy import decrypt_text, encrypt_text, resolve  # noqa: E402
from jy_cut import new_id, write_draft, register, is_encrypted  # noqa: E402
from jy_style import apply_to_material, load_preset  # noqa: E402
import jy_loud  # noqa: E402
import jy_audio  # noqa: E402

US = 1_000_000
FPS = 30
PRE, POST = 0.06, 0.10          # 保留字前后的最小留白（秒）
SEARCH_PRE, SEARCH_POST = 0.12, 0.16   # 切点在此窗口内找音量最低点
SR, WIN = 8000, 80              # 能量分析：8kHz，10ms 窗
EDGE = 0.08                     # 删字边界处：识别时间不准，前后各放宽这么多秒找静点
FADE = 33_333                   # 每个切口加 1 帧音频淡入/淡出，消除咔声
PAUSE = {"word": 0.12, "comma": 0.18, "sent": 0.32, "max_gap": 0.30, "silence_db": -17,
         "edge_pre": 1.0, "edge_post": 1.0}   # range 首字往前找开口 / 尾字往后等收尾的最远距离（秒）
# 「说完」判定：能量回落到本地底噪 + SETTLE_DB 以内并保持 SETTLE_HOLD 秒，才算这句话的尾音收干净了。
# 停顿长度从「收干净」之后开始算，不会切掉尾音。
SETTLE_DB, SETTLE_HOLD, FLOOR_WIN = 8.0, 0.12, 3.0
LONG_GAP, LONG_TAIL, LONG_HEAD = 0.9, 0.35, 0.25
MAX_HEAD, MAX_TAIL = 0.6, 0.8
QUIET_MIN, QUIET_KEEP, QUIET_REL_DB = 0.7, 0.2, 28.0
STYLE = None    # 个人字幕预设（presets/subtitle_style.json），main 里加载；spec "subtitle_style": false 关闭
MAX_LINE = 13   # 竖屏字幕一行最多约 13 个汉字宽（英文按半个），超过的 dry-run 会标 ⚠
PUNCT_SENT, PUNCT_COMMA = set("。？！?!…"), set("，、,；;：:")
RANK = {None: 0, "comma": 1, "sent": 2}


def frame(us):
    return int(round(us * FPS / US))


def fus(f):
    return int(f * US / FPS)


# ---------------------------------------------------------------- 音频能量
_energy_cache = {}


def energy(path):
    if path not in _energy_cache:
        # 长直播原片解码 + 逐窗求能量要几分钟，按 路径/大小/修改时间 缓存到磁盘
        import hashlib
        st = Path(path).stat()
        key = hashlib.md5(f"{path}|{st.st_size}|{st.st_mtime}|{SR}|{WIN}".encode()).hexdigest()
        cf = Path(__import__("os").environ.get("JY_ENERGY_CACHE", str(R.local_appdata() / "Codex/Jianying/energy"))) / f"{key}.energy"
        if cf.exists():
            en = array.array("d")
            en.frombytes(cf.read_bytes())
            _energy_cache[path] = list(en)
        else:
            raw = subprocess.run([R.tool("ffmpeg"), "-v", "quiet", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
                                  "-f", "s16le", "-"], capture_output=True, check=True).stdout
            a = array.array("h")
            a.frombytes(raw[: len(raw) // 2 * 2])
            _energy_cache[path] = [sum(x * x for x in a[i:i + WIN]) for i in range(0, len(a) - WIN, WIN)]
            cf.parent.mkdir(parents=True, exist_ok=True)
            cf.write_bytes(array.array("d", _energy_cache[path]).tobytes())
    return _energy_cache[path]


_thr_cache = {}
_floor_cache = {}
_floor_cache = {}


def _thr(path, en):
    if path not in _thr_cache:
        srt = sorted(en)
        _thr_cache[path] = srt[int(len(srt) * 0.9)] * 10 ** (PAUSE["silence_db"] / 10)
    return _thr_cache[path]


class Timeline:
    """源时间线 → 视频文件的映射，用来查任意时刻的音量。"""

    def __init__(self, d, draft_dir=None):
        # 路径占位符换成草稿目录；复合片段先把子草稿的声音混成一条 wav（见 jy_audio.py）
        idx = jy_audio._index(d)
        self.segs = []
        for t in d["tracks"]:
            if t["type"] == "video":
                for s in t["segments"]:
                    tr, sr = s["target_timerange"], s.get("source_timerange") or {}
                    path = jy_audio.seg_audio_path(d, s, draft_dir, idx) or ""
                    self.segs.append((tr.get("start", 0), tr.get("start", 0) + tr["duration"],
                                      sr.get("start", 0), path, s.get("speed") or 1.0))
                break
        # 主轨是多段拼起来的：把整条时间线的声音渲染成一个文件来分析。按段分析时，停顿正好跨在
        # 两段接缝上就读不到音量，脚本只好原样保留整段停顿（第六季结营粗剪里留下了 6 秒的空档）。
        self.tl_path = None
        if len(self.segs) > 1 and len({round(x[4], 4) for x in self.segs}) == 1:
            import hashlib
            import jy_check
            key = hashlib.md5(json.dumps([(a, b, c, pth, sp, Path(pth).stat().st_size, Path(pth).stat().st_mtime_ns) for a, b, c, pth, sp in self.segs if pth]).encode()).hexdigest()
            out = R.analysis_cache() / f"timeline_{key}.wav"
            if not out.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".tmp.wav")
                jy_check.render(d, tmp, draft_dir)
                tmp.rename(out)
            self.tl_path = str(out)

    def _locate(self, lo, hi):
        """时间线 [lo, hi] → (能量表, 起止窗口号, 窗口号→时间线 μs 的换算, 文件)。片段变速时源文件时间 = 源起点 + 时间线偏移 × 倍速。"""
        if self.tl_path:
            en = energy(self.tl_path)
            a, b = int(lo / US * SR / WIN), int(hi / US * SR / WIN)
            if a < 0 or b >= len(en):
                return None
            return en, a, b, (lambda f: int(f / SR * US)), self.tl_path
        for s, e, src, path, sp in self.segs:
            if s <= lo and hi <= e:
                try:
                    en = energy(path)
                except Exception:
                    return None
                a = int((src + (lo - s) * sp) / US * SR / WIN)
                b = int((src + (hi - s) * sp) / US * SR / WIN)
                if a < 0 or b >= len(en):
                    return None
                return en, a, b, (lambda f, s=s, src=src, sp=sp: s + int((f / SR * US - src) / sp)), path
        return None

    def silence(self, lo, hi):
        """[lo, hi] 内最长的一段静音（低于该文件 p90 能量 silence_db 分贝），返回 (起, 止) μs；没有静音返回 None。"""
        loc = self._locate(int(lo), int(hi))
        if not loc:
            return None
        en, a, b, to_tl, path = loc
        thr = _thr(path, en)
        best, run = None, None
        for i in range(a, b + 1):
            if en[i] < thr:
                run = (run[0], i) if run else (i, i)
                if not best or run[1] - run[0] > best[1] - best[0]:
                    best = run
            else:
                run = None
        if not best:
            return None
        return to_tl(best[0] * WIN), to_tl((best[1] + 1) * WIN)

    def _db(self, lo, hi):
        """时间线 [lo, hi] 的 10ms 能量（dB）、本地底噪、窗口号→时间线换算。"""
        loc = self._locate(int(lo), int(hi))
        if not loc:
            return None
        en, a, b, to_tl, path = loc
        import math
        key = (path, a // 100)
        if key not in _floor_cache:
            w = int(FLOOR_WIN * SR / WIN)
            vals = sorted(en[max(0, a - w):a + w])
            fl = 10 * math.log10(vals[len(vals) // 10] / WIN + 1e-9)
            sp = 10 * math.log10(vals[int(len(vals) * 0.9)] / WIN + 1e-9)
            # 「收干净 / 开口」的门槛 = 底噪 + SETTLE_DB；底噪高的录音（说话只比底噪高 15–25 dB）这条门槛永远到不了，
            # 停顿两头各多留半秒以上。所以门槛至少抬到「底噪往上 40%」，返回等效底噪让调用方照旧 + SETTLE_DB
            _floor_cache[key] = max(fl, fl + 0.4 * (sp - fl) - SETTLE_DB)
        db = [10 * math.log10(en[i] / WIN + 1e-9) for i in range(a, b + 1)]
        return db, _floor_cache[key], a, to_tl

    def settle(self, t, limit):
        """t 之后，声音第一次回落到底噪附近并保持 SETTLE_HOLD 的时刻（即尾音收干净）。limit 前找不到返回 None。"""
        r = self._db(t, limit + SETTLE_HOLD * US)
        if not r:
            return None
        db, fl, a, to_tl = r
        hold = int(SETTLE_HOLD * SR / WIN)
        for k in range(len(db) - hold):
            if max(db[k:k + hold]) < fl + SETTLE_DB:
                tt = to_tl((a + k) * WIN)
                return tt if tt <= limit else None
        return None

    def onset(self, t, limit):
        """t 附近开口的时刻：先往后找到出声，再往前找到其前面的安静处。limit 之前找不到返回 None。"""
        r = self._db(limit - SETTLE_HOLD * US, t + 0.3 * US)
        if not r:
            return None
        db, fl, a, to_tl = r
        hold = int(SETTLE_HOLD * SR / WIN)
        lt = self._locate(int(t), int(t))
        if not lt:
            return None
        i0 = min(len(db) - 1, lt[1] - a)   # t 对应的窗口（变速片段里时间线和文件时间不是 1:1）
        loud = next((k for k in range(max(i0 - 5, 0), len(db)) if db[k] >= fl + SETTLE_DB), None)
        if loud is None:
            return None
        for k in range(loud, hold - 1, -1):
            if max(db[k - hold:k]) < fl + SETTLE_DB:
                return to_tl((a + k) * WIN)
        return None

    def quiet_runs(self, lo, hi, min_len, rel_db=QUIET_REL_DB):
        """[lo, hi] 内按声音找静音段：比附近（±1.5s）说话声的 90 分位低 rel_db 以上、持续 ≥ min_len 秒。
        返回 [(起, 止)] μs。逐字时间是插值来的时候，句子中间的停顿只能靠这个找出来。"""
        loc = self._locate(int(max(0, lo - 1.5 * US)), int(hi + 1.5 * US)) or self._locate(int(lo), int(hi))
        if not loc:
            return []
        en, a, b, to_tl, _ = loc
        import math
        db = [10 * math.log10(en[i] / WIN + 1e-9) for i in range(a, b + 1)]
        w = int(1.5 * SR / WIN)
        ia, ib = int(lo / US * SR / WIN) - a if self.tl_path else 0, None
        if not self.tl_path:
            return []   # 只在整条时间线分析模式下做，按段模式窗口号不好换算
        ib = int(hi / US * SR / WIN) - a
        runs, st = [], None
        for i in range(max(0, ia), min(len(db), ib + 1)):
            loc_db = sorted(db[max(0, i - w):i + w])
            p90, p10 = loc_db[int(len(loc_db) * 0.9)], loc_db[len(loc_db) // 10]
            # 门槛随信噪比走：干净的出镜收音按「比说话声低 rel_db」；底噪高的录音（说话和停顿只差 20 多 dB）
            # 那条永远达不到，改按「底噪往上 40%」——第六季结营里复合片段那段手机录音就是这样，停顿一个都没压
            q = db[i] < max(p90 - rel_db, p10 + 0.4 * (p90 - p10))
            if q and st is None:
                st = i
            if (not q or i == min(len(db), ib + 1) - 1) and st is not None:
                if (i - st) * WIN / SR >= min_len:
                    runs.append((to_tl((a + st) * WIN), to_tl((a + i) * WIN)))
                st = None
        return runs

    def quietest(self, lo, hi):
        """在 [lo, hi]（μs）内找音量最低的 10ms，返回其中点；无法分析时返回 None。"""
        if hi <= lo:
            return lo
        loc = self._locate(int(lo), int(hi))
        if not loc:
            return None
        en, a, b, to_tl, _ = loc
        i = min(range(a, b + 1), key=lambda k: en[k])
        return to_tl(i * WIN + WIN / 2)


# ---------------------------------------------------------------- 字幕与编辑
def load_lines(d, words=None):
    """words：{字幕素材 id: {"start_time": [...], "end_time": [...], "text": [...]}}（毫秒，相对字幕起点）。
    剪映有时不存逐字时间（words 为空），这时用外部识别对齐出来的补上（spec "words_file"）。"""
    idx = {m["id"]: m for m in d["materials"].get("texts", [])}
    if words:
        for mid, w in words.items():
            if mid in idx and not idx[mid].get("words", {}).get("text"):
                idx[mid]["words"] = w
    lines = []
    for t in d["tracks"]:
        if t["type"] != "text":
            continue
        for s in t["segments"]:
            m = idx.get(s["material_id"])
            if not m or not m.get("words", {}).get("text"):
                continue
            st = s["target_timerange"].get("start", 0)
            w = m["words"]
            dur = s["target_timerange"]["duration"]
            last = max(w["end_time"]) * 1000 if w["end_time"] else 0
            k = dur / last if last > dur + 50_000 else 1   # 识别错位：逐字时间超出整句时按比例压回句内
            toks = [dict(t=x, s=st + int(a * 1000 * k), e=st + int(b * 1000 * k))
                    for a, b, x in zip(w["start_time"], w["end_time"], w["text"])]
            lines.append(dict(key=f"{st / US:.2f}", start=st, end=st + s["target_timerange"]["duration"],
                              seg=s, mat=m, toks=toks))
    return sorted(lines, key=lambda x: x["start"])


def apply_edit(line, edit):
    toks = line["toks"]
    for t in toks:
        t.update(keep=True, show=t["t"])
    if edit is None:
        return
    if edit.strip() == "-":
        for t in toks:
            t["keep"] = False
        return
    parts = edit.split("|")
    if len(parts) != len(toks):
        sys.exit(f"编辑分词数不符 @{line['key']}：原句 {len(toks)} 个 {'|'.join(t['t'] for t in toks)}，编辑 {len(parts)} 个 {edit}")
    for t, p in zip(toks, parts):
        if p.startswith("[") and p.endswith("]"):
            t["keep"], p = False, p[1:-1]
        show = None
        if "{" in p and p.endswith("}"):
            p, show = p[:p.index("{")], p[p.index("{") + 1:-1]
        if p.replace("␣", " ") != t["t"]:
            sys.exit(f"分词不符 @{line['key']}：{p!r} ≠ {t['t']!r}")
        if show is not None:
            t["show"] = show.replace("␣", " ")


def plus_join(seq):
    """两个英文/数字词之间的「加 / 加上」字幕显示成「+」（AI加IP → AI+IP，IP加上IP操盘手 → IP+IP操盘手），音频不动。
    只看保留的字，且用户在稿子里没手动改过显示（show 仍等于原字）。"""
    kept = [t for t in seq if t["keep"]]
    for i, t in enumerate(kept):
        if t["t"] not in ("加", "加上") or t["show"] != t["t"] or i == 0:
            continue
        j = i + 1
        shang = t["t"] == "加" and j < len(kept) and kept[j]["t"] == "上" and kept[j]["show"] == "上"
        if shang:
            j += 1
        prev, nxt = kept[i - 1]["show"], (kept[j]["show"] if j < len(kept) else "")
        if prev and nxt and prev[-1].isascii() and prev[-1].isalnum() and nxt[0].isascii() and nxt[0].isalnum():
            t["show"] = "+"
            if shang:
                kept[i + 1]["show"] = ""


def line_text(toks):
    """一行字幕的显示文字：保留字的 show 拼起来；行内标点变空格，行尾「，。」去掉、「？！」保留。"""
    out = ""
    kept = [t for t in toks if t["keep"]]
    for i, t in enumerate(kept):
        out += t["show"]
        mark = t.get("mark", "")
        if mark:
            if i == len(kept) - 1:
                out += "".join(c for c in mark if c in "？！?!")
            else:
                keep_q = "".join(c for c in mark if c in "？！?!")
                out += keep_q + " "
    return " ".join(out.split())


def range_tokens(lines, ra, rb):
    ls = [ln for ln in lines if ln["start"] >= ra - 10_000 and ln["end"] <= rb + 10_000]
    return ls, [t for ln in ls for t in ln["toks"]]


def plans_from_edits(lines, ranges):
    plans = []
    for ra, rb in ranges:
        ls, seq = range_tokens(lines, ra, rb)
        for ln in ls:
            if ln["toks"] and not ln["toks"][-1].get("brk"):
                ln["toks"][-1]["brk"] = "comma"
        plans.append((seq, [{"toks": ln["toks"], "seg": ln["seg"]} for ln in ls]))
    return plans


def parse_part(part):
    """一个分词写法 → (原字, keep, show 或 None, 后缀标点)"""
    mark = ""
    while part and part[-1] in PUNCT_SENT | PUNCT_COMMA and not part.endswith(("}", "]")):
        mark = part[-1] + mark
        part = part[:-1]
    if part.startswith("[") and "]" in part:
        tail = part[part.rindex("]") + 1:]
        return part[1:part.rindex("]")].replace("␣", " "), False, None, tail + mark
    show = None
    if "{" in part and "}" in part:
        tail = part[part.rindex("}") + 1:]
        part, show = part[:part.index("{")], part[part.index("{") + 1:part.rindex("}")]
        mark = tail + mark
    return part.replace("␣", " "), True, (show.replace("␣", " ") if show is not None else None), mark


def plans_from_script(lines, ranges, script_text, glossary=None):
    """按断句稿生成 plans。稿子里的字必须和源字幕的字逐个对上（顺序、内容），换行和标点可以自由改。"""
    blocks, cur = [[]], None
    for raw in script_text.splitlines():
        line = raw.split("  #")[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.strip().startswith("==="):
            blocks.append([])
            continue
        blocks[-1].append(line.strip())
    blocks = [b for b in blocks if b]
    if len(blocks) != len(ranges):
        sys.exit(f"稿子分了 {len(blocks)} 段（=== 分隔），但 ranges 有 {len(ranges)} 段")
    gl = {k.lower(): v for k, v in (glossary or {}).items()}
    plans = []
    for (ra, rb), block in zip(ranges, blocks):
        ls, src = range_tokens(lines, ra, rb)
        tpl_of = {id(t): ln["seg"] for ln in ls for t in ln["toks"]}
        seq, sublines, k = [], [], 0
        for li, line in enumerate(block):
            drop_all = line.startswith("~")
            parts = [x.strip() for x in line.lstrip("~").split("|") if x.strip()]
            row = []
            for part in parts:
                if part and all(c in PUNCT_SENT | PUNCT_COMMA for c in part):   # 单独的标点：挂到上一个字
                    if row:
                        row[-1]["mark"] = row[-1].get("mark", "") + part
                    continue
                raw_t, keep, show, mark = parse_part(part)
                if k >= len(src):
                    sys.exit(f"稿子比源字幕多出了字：第 {li+1} 行「{line}」的 {part!r}")
                t = src[k]
                if raw_t != t["t"]:
                    ctx = "|".join(x["t"] for x in src[max(0, k - 3):k + 4])
                    sys.exit(f"第 {li+1} 行「{line}」对不上：稿子是 {raw_t!r}，源字幕这里是 {t['t']!r}（附近：{ctx}）")
                t = dict(t)
                t["keep"] = keep and not drop_all
                t["show"] = show if show is not None else gl.get(t["t"].lower(), t["t"])
                t["mark"] = mark
                t["brk"] = "sent" if set(mark) & PUNCT_SENT else ("comma" if mark else None)
                row.append(t)
                k += 1
            if row and not row[-1]["brk"]:
                row[-1]["brk"] = "comma"          # 换行本身就是一次半句停顿
            seq += row
            if row:
                sublines.append({"toks": row, "seg": tpl_of[id(src[k - len(row)])]})
        if k != len(src):
            rest = "|".join(x["t"] for x in src[k:k + 8])
            sys.exit(f"稿子少了字：range {ra / US:.2f}s 起还剩 {len(src) - k} 个没写（从「{rest}」开始）")
        plus_join(seq)
        plans.append((seq, sublines))
    return plans


def dump_skeleton(lines, ranges, tl):
    """导出一条的逐字骨架：每行一句原字幕，行尾注释给出前面的静音长度，方便判断句号/逗号。"""
    out = []
    for ri, (ra, rb) in enumerate(ranges):
        if ri:
            out.append("===")
        ls, _ = range_tokens(lines, ra, rb)
        prev = None
        for ln in ls:
            row = "|".join(t["t"].replace(" ", "␣") for t in ln["toks"])
            note = ""
            if prev is not None:
                run = tl.silence(max(prev["toks"][-1]["s"], prev["toks"][-1]["e"] - 0.15 * US),
                                 min(ln["toks"][0]["e"], ln["toks"][0]["s"] + 0.15 * US))
                if run:
                    note = f"  # 前停 {(run[1] - run[0]) / US:.2f}s"
            out.append(f"{row}{note}")
            prev = ln
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 构建
def plan_intervals(seq, tl, P=None):
    """seq：一个 range 内按时间排好的字（keep 删除标记、brk 其后的断点：None/comma/sent）。
    返回保留的源时间区间（帧）。停顿按真实静音压到目标长度：句内 word、逗号 comma、句号 sent。"""
    P = {**PAUSE, **(P or {})}
    out = []
    kept = [i for i, t in enumerate(seq) if t["keep"]]
    if not kept:
        return out

    def tail_end(i, limit):
        """第 i 个字说完、尾音收干净的时刻；limit 前没收干净（连读）返回 None。"""
        t = seq[i]
        return tl.settle(max(t["s"], t["e"] - 0.05 * US), limit)

    def head_start(i, limit):
        """第 i 个字开口的时刻（往前不超过 limit）。"""
        t = seq[i]
        return tl.onset(t["s"], limit)

    def cut_after(i, cls):
        """保留字 i 之后要切走的内容时：在尾音收干净后，再留一段安静（按标点给停顿的一半）。"""
        t = seq[i]
        nxt = seq[i + 1]["s"] if i + 1 < len(seq) else t["e"] + P["edge_post"] * US
        if i + 1 < len(seq):   # 识别出的字时间常偏早：以下一个字真正开口的时刻为界
            nxt = max(nxt, tl.onset(nxt, t["e"]) or nxt)
        te = tail_end(i, min(nxt + 0.05 * US, t["e"] + MAX_TAIL * US))   # 收尾最多等 MAX_TAIL，免得长停顿里的杂音被当成尾音留下
        if te is None:   # 和后面删掉的字连读：退回在交界处找最安静的点
            lo, hi = max(t["e"] - EDGE / 2 * US, t["e"] - (t["e"] - t["s"]) / 3), min(nxt + 0.05 * US, t["e"] + EDGE * US)
            q = tl.quietest(int(lo), int(hi)) if hi > lo else None
            return q if q is not None else t["e"]
        room = (head_start(i + 1, te) if i + 1 < len(seq) else None) or nxt
        want = {"comma": P["comma"], "sent": P["sent"]}.get(cls, P["word"]) / 2
        return te + max(0, min(want * US, room - te - 0.03 * US))

    def cut_before(i, cls):
        """保留字 i 之前要切走的内容时：开口前留一段安静。"""
        t = seq[i]
        prv = seq[i - 1]["e"] if i > 0 else t["s"] - P["edge_pre"] * US
        lim = max(prv - 0.05 * US, t["s"] - MAX_HEAD * US)   # 开口最多往前找 MAX_HEAD，免得一路找回长停顿开头
        hs = head_start(i, lim)
        if hs is None or hs < lim:
            lo, hi = max(prv - 0.05 * US, t["s"] - EDGE * US), min(t["s"] + EDGE / 2 * US, t["s"] + (t["e"] - t["s"]) / 3)
            q = tl.quietest(int(lo), int(hi)) if hi > lo else None
            return q if q is not None else t["s"]
        room = (tail_end(i - 1, hs) if i > 0 else None) or prv
        want = {"comma": P["comma"], "sent": P["sent"]}.get(cls, P["word"]) / 2
        return hs - max(0, min(want * US, hs - room - 0.03 * US))

    first, last = kept[0], kept[-1]
    cur_a = cut_before(first, "comma")
    for j in range(1, len(kept)):
        p, n = kept[j - 1], kept[j]
        deleted_between = any(not seq[k]["keep"] for k in range(p + 1, n))
        cls = max((seq[k].get("brk") for k in range(p, n)), key=lambda x: RANK[x])
        if deleted_between:
            out.append((cur_a, cut_after(p, cls)))
            cur_a = cut_before(n, cls)
            continue
        target = {"comma": P["comma"], "sent": P["sent"]}.get(cls)
        pe, ns = seq[p]["e"], seq[n]["s"]
        ns = max(ns, tl.onset(ns, pe) or ns)
        te = tail_end(p, min(ns + 0.05 * US, pe + MAX_TAIL * US))          # 上一个字的尾音收干净（最多等 MAX_TAIL）
        hs = head_start(n, max(te, ns - MAX_HEAD * US)) if te else None     # 下一个字开口（最多往前找 MAX_HEAD）
        if te is None or hs is None or hs <= te:    # 连读、中间没有安静：不动
            # 但字与字之间隔得很长（停顿里有呼吸、杂音，或那段录音底噪高，判断不出「收干净」）时，
            # 不能整段留着：前字后留 LONG_TAIL 收尾、后字前留 LONG_HEAD，中间去掉。
            if (ns - pe) / US > LONG_GAP:
                a_cut = pe + LONG_TAIL * US
                b_cut = ns - LONG_HEAD * US
                if b_cut - a_cut > 0.1 * US:
                    out.append((cur_a, a_cut))
                    cur_a = b_cut
            continue
        quiet = (hs - te) / US
        if target is None:
            if quiet <= P["max_gap"]:
                continue
            target = P["word"]
        if quiet <= target + 0.04:
            continue
        # 只压缩「尾音收干净 → 开口」之间的纯安静，尾音和起音都完整保留
        out.append((cur_a, te + target * 0.5 * US))
        cur_a = hs - target * 0.5 * US
    out.append((cur_a, cut_after(last, "sent")))

    # 逐字时间是插值来的（句中停顿被摊进字里），上面看不出句中停顿：按声音再找一遍长静音，压短
    refined = []
    for a0, b0 in out:
        cur = a0
        for qa, qb in tl.quiet_runs(a0, b0, QUIET_MIN):
            ca, cb = qa + QUIET_KEEP * US, qb - QUIET_KEEP * US
            if cb - ca > 0.1 * US and ca > cur:
                refined.append((cur, ca))
                cur = cb
        refined.append((cur, b0))
    out = refined

    # 对齐到帧，合并过近的区间
    fr = []
    for a, b in out:
        fa, fb = frame(a), frame(b)
        if fb <= fa:
            fb = fa + 1
        if fr and fa <= fr[-1][1] + 1:
            fr[-1] = (fr[-1][0], max(fr[-1][1], fb))
        else:
            fr.append((fa, fb))
    return fr


def build(d, plans, tl, P=None):
    """plans：每个 range 一项 (seq, sublines)。seq = 该 range 内按时间排好的字；
    sublines = 生成字幕用的行 [{"toks": [...], "seg": 模板字幕片段}]，一行一条字幕。"""
    lookup = {}
    for kind, items in d["materials"].items():
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and "id" in m:
                    lookup[m["id"]] = (kind, m)
    seg_refs = {r for t in d["tracks"] for s in t["segments"] for r in [s["material_id"], *s.get("extra_material_refs", [])]}
    out = copy.deepcopy({k: v for k, v in d.items() if k not in ("tracks", "materials")})
    mats = {k: ([m for m in v if not (isinstance(m, dict) and m.get("id") in seg_refs)] if isinstance(v, list) else copy.deepcopy(v))
            for k, v in d["materials"].items()}

    def clone_mat(mid):
        kind, m = lookup[mid]
        if kind == "drafts":   # 复合片段的子草稿可以被多个片段共用（剪映自己也这样存），不复制，否则每刀一份 0.5MB
            if not any(x.get("id") == mid for x in mats[kind]):
                mats[kind].append(copy.deepcopy(m))
            return m
        c = copy.deepcopy(m)
        c["id"] = new_id()
        mats[kind].append(c)
        return c

    # 1) 每个 range 内：选字幕行 → 规划保留区间（帧）
    pieces = []   # (源起始帧, 源结束帧, 新起始帧, range 序号)
    sel_lines = []  # (字幕行, range 序号)
    cursor = 0
    for ri, (seq, sublines) in enumerate(plans):
        for fa, fb in plan_intervals(seq, tl, P):
            pieces.append((fa, fb, cursor, ri))
            cursor += fb - fa
        sel_lines += [(ln, ri) for ln in sublines]

    def to_new(us, ri):
        """源时间 → 新时间；同一句可能在多个 range 中出现（如金句前置），只在自己的 range 里找。"""
        f = us * FPS / US
        best = None
        for fa, fb, nc, r in pieces:
            if r != ri:
                continue
            if fa - 0.5 <= f <= fb + 0.5:
                return fus(nc + min(max(f, fa), fb) - fa)
            dist = min(abs(f - fa), abs(f - fb))
            if best is None or dist < best[0]:
                best = (dist, fus(nc + (0 if f < fa else fb - fa)))
        return best[1]

    total = fus(cursor)

    # 2) 视频 / 音频等非文字轨：按保留区间裁剪
    tracks = []
    for t in d["tracks"]:
        nt = copy.deepcopy({k: v for k, v in t.items() if k != "segments"})
        nt["id"] = new_id()
        nt["segments"] = []
        tracks.append((t, nt))
    for t, nt in tracks:
        if t["type"] == "text":
            continue
        for fa, fb, nc, _ in pieces:
            a, b = fus(fa), fus(fb)
            for seg in t["segments"]:
                tr = seg["target_timerange"]
                s, e = tr.get("start", 0), tr.get("start", 0) + tr["duration"]
                ns, ne = max(s, a), min(e, b)
                if ne - ns < US / FPS:
                    continue
                c = copy.deepcopy(seg)
                c["id"] = new_id()
                c["material_id"] = clone_mat(seg["material_id"])["id"]
                c["extra_material_refs"] = [clone_mat(r)["id"] for r in seg.get("extra_material_refs", [])]
                start = fus(nc + frame(ns) - fa)
                c["target_timerange"] = {"start": start, "duration": fus(nc + frame(ne) - fa) - start}
                fade_in = FADE if ns == a and nc > 0 else 0
                fade_out = FADE if ne == b and fus(nc + fb - fa) < fus(cursor) else 0
                if (fade_in or fade_out) and t["type"] in ("video", "audio"):
                    fid = new_id()
                    mats.setdefault("audio_fades", []).append({"id": fid, "type": "audio_fade", "fade_type": 0,
                                                               "fade_in_duration": fade_in, "fade_out_duration": fade_out})
                    c["extra_material_refs"].append(fid)
                if c.get("source_timerange"):
                    src0 = (seg.get("source_timerange") or {}).get("start", 0)
                    sp = seg.get("speed") or 1.0  # 变速片段：源文件上的偏移和时长都要乘倍速
                    c["source_timerange"] = {"start": src0 + round((ns - s) * sp),
                                             "duration": round(c["target_timerange"]["duration"] * sp)}
                nt["segments"].append(c)

    # 3) 字幕：每行保留字 → 新文本 + 新逐字时间
    track_of = {id(s): nt for t, nt in tracks if t["type"] == "text" for s in t["segments"]}
    subs = []
    for ln, ri in sel_lines:
        kept = [t for t in ln["toks"] if t["keep"]]
        text = line_text(ln["toks"])
        if not kept or not text:
            continue
        # 字幕起止按声音来：开口就出现、尾音收干净才消失（识别出的字时间常偏早偏短）
        s0, e0 = kept[0]["s"], kept[-1]["e"]
        on = tl.onset(s0, s0 - 0.6 * US)
        if on is not None and on < s0:
            s0 = on
        st = tl.settle(max(kept[-1]["s"], e0 - 0.05 * US), e0 + 1.0 * US)
        if st is not None and st > e0:
            e0 = st
        subs.append((to_new(s0, ri), to_new(e0, ri), ln, kept, text, ri))
    subs.sort(key=lambda x: x[0])
    shown = []
    for i, (s, e, ln, kept, text, ri) in enumerate(subs):
        s = fus(frame(s))
        nxt = subs[i + 1][0] if i + 1 < len(subs) else total
        e = nxt if nxt - e < 0.5 * US else e + int(0.1 * US)
        e = min(fus(frame(e)), total, fus(frame(nxt)))
        if e <= s:
            e = s + fus(1)
        m = clone_mat(ln["seg"]["material_id"])
        c = json.loads(m["content"])
        c["text"] = text
        for st in c.get("styles", []):
            st["range"] = [0, len(text)]
        m["content"] = json.dumps(c, ensure_ascii=False, separators=(",", ":"))
        if m.get("base_content"):
            try:
                bc = json.loads(m["base_content"])
                bc["text"] = text
                for st in bc.get("styles", []):
                    st["range"] = [0, len(text)]
                m["base_content"] = json.dumps(bc, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass
        m["recognize_text"] = text
        ws = [(max(0, (to_new(t["s"], ri) - s) // 1000), max(0, (to_new(t["e"], ri) - s) // 1000), t["show"]) for t in kept if t["show"]]
        m["words"] = {"start_time": [a for a, _, _ in ws], "end_time": [b for _, b, _ in ws], "text": [x for _, _, x in ws]}
        seg = copy.deepcopy(ln["seg"])
        seg["id"] = new_id()
        seg["material_id"] = m["id"]
        seg["extra_material_refs"] = [clone_mat(r)["id"] for r in ln["seg"].get("extra_material_refs", [])]
        seg["target_timerange"] = {"start": s, "duration": e - s}
        if STYLE:
            apply_to_material(m, STYLE)
            if STYLE.get("clip"):
                seg["clip"] = copy.deepcopy(STYLE["clip"])
        track_of[id(ln["seg"])]["segments"].append(seg)
        shown.append((s, e, text))

    for _, nt in tracks:
        nt["segments"].sort(key=lambda x: x["target_timerange"]["start"])
    out["tracks"] = [nt for _, nt in tracks]
    out["materials"] = mats
    out["duration"] = total
    if isinstance(out.get("extra_info"), dict):
        out["extra_info"]["subtitle_fragment_info_list"] = []
    return out, shown, len(pieces)


def user_touched(dst: Path):
    """草稿在我们上次写入之后被剪映保存过（用户改过）就返回 True。上次写入的指纹记在 .jy_last_write。"""
    main, mark = resolve(str(dst)), dst / ".jy_last_write"
    if not mark.exists():
        return True   # 不是本脚本写的草稿，一律当作用户的
    return mark.read_text().strip() != f"{main.stat().st_size}:{main.stat().st_mtime_ns}"


def overwrite_draft(dst: Path, info: dict, force=False):
    """覆盖已有草稿：保留 draft_id 与时间线 id，备份原主文件。用户改过的草稿不覆盖，除非 force。"""
    main = resolve(str(dst))
    R.ensure_editor_closed(dst)
    if user_touched(dst) and not force:
        sys.exit(f"⛔ {dst.name} 在上次生成后被剪映保存过（用户可能改过），不覆盖。确认可以覆盖再加 --force。")
    old = json.loads(decrypt_text(main.read_text(encoding="utf-8-sig")))
    info["id"] = old["id"]
    bakdir = dst / f".jy_backup_{time.strftime('%Y%m%d-%H%M%S')}"
    bakdir.mkdir()
    shutil.copy2(main, bakdir / main.name)
    R.write_main_files(dst, info)
    meta_f = dst / "draft_meta_info.json"
    enc_meta = is_encrypted(meta_f)
    meta = json.loads(decrypt_text(meta_f.read_text(encoding="utf-8-sig")))
    meta["tm_duration"] = info["duration"]
    meta["tm_draft_modified"] = int(time.time() * US)
    plain = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    meta_f.write_text(encrypt_text(plain) if enc_meta else plain, encoding="utf-8")
    back = json.loads(decrypt_text(main.read_text(encoding="utf-8-sig")))
    assert back["duration"] == info["duration"]
    (dst / ".jy_last_write").write_text(f"{main.stat().st_size}:{main.stat().st_mtime_ns}")
    return meta


def update_root(root: Path, name: str, meta: dict):
    f = root / "root_meta_info.json"
    if not f.exists():
        return
    bak = f.with_name(f.name + time.strftime(".%Y%m%d-%H%M%S.bak"))
    if not bak.exists():
        shutil.copy2(f, bak)
    try:
        rm = json.loads(f.read_text(encoding="utf-8-sig"))
    except ValueError:   # 新版剪映退出时可能把它清空（草稿列表改存别处），不去碰
        return
    for e in rm.get("all_draft_store", []):
        if e.get("draft_name") == name:
            e["tm_duration"] = meta["tm_duration"]
            e["tm_draft_modified"] = meta["tm_draft_modified"]
    f.write_text(json.dumps(rm, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="只处理名字包含该字符串的 clip")
    ap.add_argument("--force", action="store_true", help="目标草稿被用户改过也覆盖（先确认！）")
    ap.add_argument("--dump", metavar="名字片段", help="导出这条 clip 的逐字骨架（断句模式的起点），不写草稿")
    a = ap.parse_args()
    spec_path = Path(a.spec)
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    for k in ("word_pause", "comma_pause", "sent_pause", "max_gap", "silence_db", "edge_pre", "edge_post"):
        if k in spec:
            PAUSE[k.replace("_pause", "")] = spec[k]
    global STYLE
    sty = spec.get("subtitle_style", True)
    if sty:
        STYLE = load_preset(None if sty is True else (spec_path.parent / sty))
    src_file = resolve(spec["source"])
    raw = src_file.read_text(encoding="utf-8-sig")
    d = json.loads(decrypt_text(raw))
    global FPS, FADE
    FPS = float(d.get("fps") or 30)
    FADE = round(US / FPS)
    wf = spec.get("words_file")
    lines = load_lines(d, json.loads((spec_path.parent / wf).read_text(encoding="utf-8-sig")) if wf else None)
    keys = {}
    for ln in lines:
        if ln["key"] in keys and spec.get("edits"):
            sys.exit(f"字幕起始时间重复，无法索引：{ln['key']}")
        keys[ln["key"]] = ln
    for k in spec.get("edits", {}):
        if k not in keys:
            sys.exit(f"edits 里的 {k} 找不到对应字幕")
    for ln in lines:
        apply_edit(ln, spec.get("edits", {}).get(ln["key"]))
    tl = Timeline(d, src_file.parent)
    # 源草稿不在剪映草稿目录时（比如从备份恢复到临时目录），用 spec 的 draft_root 指定输出位置
    root = Path(spec["draft_root"]) if spec.get("draft_root") else src_file.parent.parent
    created = []
    for c in spec["clips"]:
        if (a.only and a.only not in c["name"]) or (a.dump and a.dump not in c["name"]):
            continue
        ranges = [(int(x * US), int(y * US)) for x, y in c["ranges"]]
        if a.dump:
            out = spec_path.parent / f"{c['name']}.txt"
            if out.exists():
                sys.exit(f"{out} 已存在，不覆盖（删掉后再导出）")
            out.write_text(f"# {c['name']}  断句稿：一行一条字幕；[删] 改{{新}} 行首~整行删；行尾/词后「，」半句停，「。？！」整句停\n"
                           + dump_skeleton(lines, ranges, tl), encoding="utf-8-sig")
            print(f"骨架已导出：{out}")
            continue
        orig = sum(b - a_ for a_, b in ranges) / US
        if c.get("script"):
            sp = spec_path.parent / c["script"]
            plans = plans_from_script(lines, ranges, sp.read_text(encoding="utf-8-sig"), spec.get("glossary"))
        else:
            plans = plans_from_edits(lines, ranges)
        info, shown, ncut = build(d, plans, tl)
        print(f"\n▶ {c['name']}  {orig:.1f}s → {info['duration'] / US:.1f}s  （{ncut} 段画面，{len(shown)} 条字幕）")
        if a.dry_run:
            for s_, e_, t_ in shown:
                wd = sum(0.55 if ord(ch) < 128 else 1 for ch in t_)
                print(f"  {s_ / US:6.2f}-{e_ / US:6.2f}  {t_}" + (f"   ⚠ 超长 {wd:.0f} 字，竖屏一行放不下会折行/截断" if wd > MAX_LINE else ""))
            continue
        dst = R.safe_child(root, c["name"])
        if dst.exists():
            meta = overwrite_draft(dst, info, a.force)
            update_root(root, c["name"], meta)
            print(f"  ✅ 已覆盖 {dst}（原文件备份在 .jy_backup_*）")
        else:
            dst, draft_id, meta = write_draft(src_file.parent, info, c["name"], raw, output_root=root)
            mf = resolve(str(dst))
            (dst / ".jy_last_write").write_text(f"{mf.stat().st_size}:{mf.stat().st_mtime_ns}")
            created.append((src_file.parent.name, dst, draft_id, meta))
            print(f"  ✅ 新建 {dst}")
        lt = spec.get("loudness_target", False)
        if lt is not False:   # 按响度自动定音量（写入后在草稿文件上做，复合片段要落子草稿文件）
            mf = resolve(str(dst))
            li = json.loads(decrypt_text(mf.read_text(encoding="utf-8-sig")))
            msg, changed = jy_loud.apply(dst, li, float(lt))
            if changed:
                jy_loud.write_back(dst, li)
            print(f"  🔊 {msg}")
    if created:
        register(root, created)


if __name__ == "__main__":
    main()
