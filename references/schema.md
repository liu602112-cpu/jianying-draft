# 剪映草稿 JSON 字段地图

基于剪映 Mac 版 11.x（`new_version` 189.0.0）实测。**所有时间单位都是微秒**（1 秒 = 1,000,000）。

## 草稿目录

| 文件 | 内容 |
|---|---|
| `draft_info.json` | 主文件：轨道、片段、素材（新版名字） |
| `draft_content.json` | 主文件（旧版名字，结构相同） |
| `draft_meta_info.json` | 草稿元信息：`draft_name`、`tm_duration`、`draft_fold_path`、`draft_materials`（导入素材列表） |
| `Timelines/project.json` + `Timelines/<uuid>/` | 多时间线，每条时间线一份 draft_info |
| `*.bak` | 剪映自动备份 |
| `Resources/` | 草稿内嵌资源（贴纸、音效等下载缓存） |
| 草稿根目录 `root_meta_info.json` | 所有草稿的索引 |

## 顶层

```
id, version, new_version, duration(总时长 μs)
canvas_config {width, height, ratio}
config        工程设置
tracks[]      轨道
materials{}   素材库，按类型分数组
keyframes{}   关键帧
```

## tracks[]

```
type        video | audio | text | effect | filter | sticker ...
name        轨道名（用户改过才有，如「重点包装」）
flag        0 主轨；1 等其它值表示副轨/字幕轨属性
segments[]
  id
  material_id              → 指向 materials.<某类型>[].id
  target_timerange {start, duration}   在时间线上的位置
  source_timerange {start, duration}   从源素材截取的区间（文字轨没有）
  speed                    变速倍数
  volume, visible
  clip {scale{x,y}, transform{x,y}, flip, ...}   画面变换（transform 坐标中心为 0）
  extra_material_refs[]    → 指向 speeds / canvases / material_animations / sound_channel_mappings 等附属素材
  common_keyframes[]       关键帧
```

## materials{} 常用类型

| key | 说明 | 关键字段 |
|---|---|---|
| `videos` | 视频/图片素材 | `path`（本机绝对路径）、`material_name`、`duration`、`width`、`height`、`type`(video/photo) |
| `audios` | 音频 | `path`、`name`、`duration` |
| `texts` | 文字/字幕 | `content`（**JSON 字符串**，内含 `text` 和 `styles`）、`recognize_text`（自动识别原文）、`words{start_time[],end_time[],text[]}`（逐字时间，毫秒，相对片段起点）、`type`(subtitle/text)、`font_path`、`font_size`、`text_color` |
| `material_animations` | 入场/出场/循环动画 | `animations[]{name, type, duration}` |
| `speeds` | 变速 | `speed`、`curve_speed` |
| `canvases` | 背景画布 | `type`、`color`、`blur` |
| `effects` / `video_effects` | 特效 | `name`、`effect_id` |
| `stickers` | 贴纸 | `name`、`path` |
| `beats` | 踩点 | |
| `audio_fades` | 淡入淡出 | `fade_in_duration`、`fade_out_duration` |

## 常用查询片段

```python
import json; d = json.load(open("out.json"))
idx = {m["id"]: (k, m) for k, v in d["materials"].items() if isinstance(v, list) for m in v if isinstance(m, dict) and "id" in m}

# 某片段对应的素材
kind, mat = idx[seg["material_id"]]

# 字幕文本
json.loads(mat["content"])["text"]

# 某片段挂的动画
[idx[r] for r in seg["extra_material_refs"] if idx.get(r, ("",))[0] == "material_animations"]
```

部分草稿里素材 `path` 以 `##_draftpath_placeholder_` 开头，表示相对草稿目录的路径（`summary` 的缺失检查会跳过它们）。
