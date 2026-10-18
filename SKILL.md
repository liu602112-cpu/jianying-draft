---
name: jianying-draft
description: 读取/解密剪映（JianyingPro / VideoFusion / CapCut）草稿源文件。把加密的 draft_info.json / draft_content.json / draft_meta_info.json 解成 JSON，输出人话版时间线摘要（画布、时长、每条轨道每个片段的入出点、源素材、变速、缺失素材），导出字幕轨为 SRT 或纯文本逐字稿，并能把改好的 JSON 重新加密写回草稿。当用户提到「读剪映草稿」「解密剪映」「剪映源文件」「draft_info.json / draft_content.json」「看看这个草稿剪了什么」「把剪映字幕导出来」「剪映草稿转 SRT」「从剪映草稿拿逐字稿」「改剪映草稿再写回去」，或给出 %LOCALAPPDATA%/JianyingPro/User Data/Projects/com.lveditor.draft 下的路径或草稿名时使用。
---

## 公开发布版

这是便于复用的公开版，原始私人语料、声音参考、商业音乐和第三方媒体不随包提供。依赖、输入和输出见同目录 README.md。样例数据明确标为演示，不代表真实效果。


## Windows 适配说明

本版为学员版 v3 的 Windows/Codex 改编（2026-10-05，v3-windows.1），保留喂鱼原始署名。用户指令优先于源包流程；用户已明确选题、开头或从哪一步做起时，直接沿用，不重复要求确认。用户说“对齐”时只交流目标。

- 全局安装位置：`$CODEX_HOME/skills`；未设置时为 `%USERPROFILE%/.codex/skills`。关联脚本按两个 Skill 的相邻目录定位。
- 自动发现 Windows 剪映、CapCut 的默认草稿目录；非默认目录通过 `JY_DRAFT_ROOT` 指定。
- 入口：`scripts/run-jy.ps1`，自动选择已安装的 Python。先运行 `scripts/jy_doctor.py` 检查本机环境。
- 同时支持 `draft_content.json` 和 `draft_info.json`，保留源文件的明文或加密形式。只在副本和新草稿上工作；写真实草稿前检测剪映是否运行，不自动退出剪映。
- 使用源画布及帧率。精剪默认保留原音量和声道；用户要求响度处理时才设置 `loudness_target`。源包的 -13 LUFS 是作者示例参数，不代表本用户已确认。下文的个人预设、0926/1001案例和反馈属于源作者，不能当成当前用户经历。
- 源包字幕样式可继续使用；字体路径在读取预设时结构化转换。公开版不附带字体或音效。使用用户自己取得许可的字体；可通过 `LIVE_CLIP_FONT_CN` / `LIVE_CLIP_FONT_EN` 指定，缺失时报告并采用系统字体。
- 音效仍是可选素材；模糊特效没有本机缓存时保留压暗，不写入 Mac 缓存路径。首次正式使用要在剪映检查字体、动画和特效。
- 先完成 dry-run 和副本自检；脚本校验通过不等于剪映已打开或视频已播放验证。



> **原始项目声明：** 本技能 `jianying-draft` 由 **喂鱼** 整理并首发于
> https://github.com/sakuraoxo-clio/claude-skills 。任何 fork、二创、
> 教程、课件引用或再分发版本，须保留本署名与原始仓库链接，并注明所做
> 修改；不得暗示修改版是完全原创或原作者官方发布。
>
> **Original project notice:** This skill `jianying-draft` is an original work
> by **喂鱼 (Weiyu)**, first published at
> https://github.com/sakuraoxo-clio/claude-skills . Forks, derivative
> works, tutorials, and redistributed versions must retain this
> attribution and the original repository link, state their
> modifications, and must not imply that modified versions are original
> or official releases by the original author.
>
> 作者 ID：**喂鱼**（全平台同名） · 公众号：**喂鱼**


# 剪映草稿读取

通过 `scripts/run-jy.ps1` 调用，入口会选择本机 Python 并使用 Skill 自带依赖。下方 `python` 命令展示参数；实际执行时优先使用此入口，例如 `& $Runner jy summary <草稿>`。依赖安装与外部工具配置见 README.md。

`<草稿>` 参数可以是**草稿名**（剪映里显示的名字，如 `0926直播`）、**草稿目录**，或**具体文件**。明文和加密都能读，脚本会自己判断。

```powershell
$SkillRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME "skills" } else { Join-Path $env:USERPROFILE ".codex/skills" }
$Runner = Join-Path $SkillRoot "jianying-draft/scripts/run-jy.ps1"
& $Runner jy list -n 20                    # 最近改过的草稿（按修改时间）
& $Runner jy summary 0926直播               # 人话摘要，先跑这个
& $Runner jy srt 0926直播 -o out.srt        # 字幕轨 → SRT（--txt 出纯文本逐字稿，--track N 只导某条文字轨）
& $Runner jy decrypt 0926直播 -o out.json   # 完整 JSON，要深挖字段时用
& $Runner jy encrypt out.json -o <草稿目录>/draft_info.json   # 改完写回
```

新增 v3 工具：`jy_audio.py` 解析复合片段音频和占位符路径；`jy_words.py` 补字幕逐字时间；`jy_frames.py` 抽封面候选帧。

其他脚本（同目录）：`jy_cut.py` 切条 · `jy_tighten.py` 精剪（套字幕预设，保留源音量及声道）· `jy_merge.py` 合成多时间线 · `jy_style.py` 补套字幕预设 · `jy_loud.py` 补做声道和音量 · `jy_check.py` 写入后识别回听。

## 工作流程

1. **用户说的草稿不明确** → 先 `list` 看最近的，按名字/时间匹配；有多个候选就问。
2. **想知道草稿里有什么** → `summary`。它已经把微秒换成时间码、把片段和素材对上号，并列出本机找不到的素材路径。多数问题（剪了几段、用了哪些素材、从源视频哪一段截的、字幕说了什么）看摘要就够，不要把整个 JSON 读进上下文——主文件动辄 1MB+。
3. **要字幕/口播文字** → `srt`（带时间码）或 `srt --txt`（纯文本，适合拿去写文案、做拆解）。
4. **要具体字段**（字体、动画、关键帧、特效、音量等）→ `decrypt` 到草稿目录外的文件（默认写到当前目录，临时用放 scratchpad），再用 `python -c` / `jq` 定点查询。字段地图见 [references/schema.md](references/schema.md)。
5. **要改草稿** → 见下方「写回」。

## 写回与原稿保护

1. 先在工作目录准备可审阅的副本和 dry-run。写入剪映实际草稿库前需保存并退出剪映，工具会检查运行状态；不自动关闭应用。
2. `decrypt` → 改 JSON → `encrypt -o 原文件路径`。脚本会自动把原文件备份为 `*.时间戳.bak`，并在写完后回读校验能解密。
3. 新版剪映草稿目录里还有 `Timelines/<uuid>/draft_info.json`（多时间线）和 `draft_info.json.bak`；剪映打开时可能优先读它们。改完如果剪映里没生效，检查这两处。
4. 旧版剪映 / CapCut 草稿本来是明文的，写回加 `--plain`。
5. 改动 id 引用（segment.material_id、extra_material_refs）时要保证引用的素材在 materials 里存在，否则剪映会打不开草稿。

## 切条：一个草稿拆成多个新草稿

`scripts/jy_cut.py spec.json [--dry-run]`，原草稿不动，每条生成一个独立的新草稿并登记进剪映。

```json
{"source": "0926直播",
 "clips": [{"name": "0926切1_标题", "ranges": [[718.35, 721.733], [662.2, 783.0]]}]}
```

- `ranges` 是源草稿时间线上的秒数，按列表顺序首尾拼接。把金句区间放在第一个，就能做成「金句前置」。
- 文字轨只保留**完整落在区间内**的字幕，所以区间端点要对齐字幕的起止时间。先用 `decrypt` 把字幕连同毫秒时间列出来，再定切点。
- 一定先跑 `--dry-run` 核对每条的首尾字幕，再正式生成。必须先关闭剪映。
- 脚本会复制草稿目录（不含 `.backup`），给所有片段和素材换新 id，替换 `draft_id` 和时间线 id，清掉失效的字幕识别缓存，并备份后改写 `root_meta_info.json`。
- 录制文件名里带时间（如大疆的 `DJI_日期时间_编号`）时，要检查区间有没有跨过两段录制之间的断档，跨过了内容就接不上。

## 精剪：按字删气口 / 口误 / 重复，改错别字

> **推荐用断句模式**：clip 里写 `"script": "切1.txt"`，先用 `--dump 切1` 导出逐字骨架，在骨架上删字、改字、按语义换行、标上逗号和句号。这样字幕会按意思重新断行，句号处的停顿（0.32 秒）会比逗号（0.18 秒）略长，停顿长度按原片音频的真实静音来算。写法和规则见 live-clip skill 的 `references/editing-rules.md`。下面的 `edits` 写法仍然可以用，但字幕只能沿用原来的分行。


`scripts/jy_tighten.py spec.json [--dry-run] [--only 名字片段]`，永远从源草稿重新生成。目标草稿已存在就覆盖（原主文件备份到草稿目录下的 `.jy_backup_时间戳/`），不存在就新建并登记。

spec 在 `jy_cut` 的基础上加了 `edits` 和 `max_gap`，完整格式见脚本开头的注释：

```json
"edits": {
  "662.33": "[但是]|[内容]|[就]|明年",     // [字] 删掉这个字：音频和字幕一起删
  "706.77": "-",                            // 整句删掉
  "705.57": "开|一个|20{200}|差{刀}|的"      // 字{新字} 只改字幕，音频保留；{} 表示不显示这个字
}
```

- 键是这句字幕在**源时间线**上的起始秒（两位小数）。每句要**按原分词逐个写出来**，分词数对不上脚本会报错。先把每句带逐字时间的分词列出来（读 `materials.texts[].words`），再写编辑表。
- 字和字之间超过 `max_gap`（默认 0.3 秒）的停顿会压到约 0.12 秒。停顿从「尾音收干净」（音量回落到底噪 +8dB 以内并保持 120ms）之后才开始算，不会切掉尾音；切点在字边界附近找音量最低的位置（用 ffmpeg 读原片音频），每个切口再加 1 帧音频淡入淡出，消除咔声。
- 变速素材（比如直播草稿整体 1.2 倍速）已处理：源文件上的偏移和时长都乘倍速。
- 字幕起止按音量算：开口就出现，尾音收干净才消失，不用识别出来的字时间。`--dry-run` 会给超过 13 个字宽（竖屏一行放不下，会折行或截断）的行标 ⚠。
- **覆盖保护**：用户在剪映里改过的草稿或时间线不能被重新生成覆盖。写入前先看文件修改时间：比我们上次写入的时间新，说明用户动过，必须先问。
- 自检：`--dry-run` 只看字幕。写入后**必须**跑 `python scripts/jy_check.py <草稿…>`：按切点拼音频，用 whisper.cpp 识别，再和字幕对照，列出识别多出的（漏音）和缺失的（切字）。复合片段也能读。
- **区间两头漏音**：每个 range 的首字会往前找开口、尾字会往后等收尾，默认最远 1 秒。如果区间外紧挨着别的话（比如「打个比方 / 我们之前的IP」中间只隔了 0.12 秒），这些话会被带进来。在 spec 里设 `"edge_pre": 0.25, "edge_post": 0.3` 可以收紧（1001 拍摄素材验证过）。写入后一定要识别回听，看每个区间的首尾。
- 同一句在多个区间里出现时（比如金句前置），各自按所在区间计算时间，所有 clip 共用同一套 `edits`。
- 剪映的语音识别偶尔会出现逐字时间超出整句的情况（约 1.5%），脚本会按比例压回句内。
- 写编辑表的经验：
  - **优先删**：呃、嗯、啊、诶，以及句首的「就」「那么」「就是」。
  - **整句删**：结巴重复（我我、会会）和说到一半重来的句子。
  - **跨行断词**（比如「它之 / 前稀缺」）用 `{}` 把字挪到同一行。
  - **英文**：统一大小写（Prompt Engineering、DeepSeek）。
  - **人名、产品名**：先根据上下文推断，比如「小宁说」应为「小Lin说」；拿不准的列出来问用户。

## 字幕样式：默认用个人预设
源作者示例字幕预设存在 `presets/subtitle_style.json`：思源黑体加粗、字号 8、白字、黑色阴影、30% 黑色底框，位置 y=-0.56。来源是源作者「1001切9」案例，不代表当前用户预设。
- `jy_tighten.py` 生成的字幕**默认套用**这份预设。spec 里写 `"subtitle_style": false` 保留源字幕样式，也可以填别的预设文件路径。
- 给已有草稿补套：`python scripts/jy_style.py <草稿名> [...] --dry-run`，确认后去掉 `--dry-run`。只改字幕样式，不改文字和时间。用户保存过的草稿会被跳过；确认可以改再加 `--force`，改动是在现有文件上做的，用户其他的修改会保留。
- 用户换了新预设：在剪映里给一条字幕套上，然后从那条字幕重新提取（顶层样式字段放 `fields`，`content.styles[0]` 去掉 range 放 `style`，片段 `clip` 放 `clip`）。

## 音量：按响度自动定（「冒红尖儿」标准）
源作者的示例目标是 **-13 LUFS**，这是可选处理参数。当前用户未要求调音量时，保留原音量及声道；不要把示例的“冒红尖儿”或 +20 dB 当成当前用户标准。
- **先处理单边录音**：左右声道的 RMS 差 3 dB 以上，就当成单边录音，给主轨每段设剪映的「声道配置」，把响的那边填充到另一边。字段是 `sound_channel_mappings.audio_channel_mapping`：1 = 左声道填充至右侧（用户在切8 里亲手设过，已验证），2 = 右声道填充至左侧（按对称推出来的）；同时 `is_config_open` 要设成 true。量响度时也按填充后的声音来量。
- `jy_tighten.py` 默认 `"loudness_target": false`。仅在用户要求响度处理时填写目标值，例如 `-15`。
- 补套到已有草稿：`python scripts/jy_loud.py <草稿名> [...] --dry-run`，确认后去掉 `--dry-run`。
- 规则：需要的增益 = 目标值 − 原始响度。
  - 不超过 +20 dB：直接设在每个画面片段上。剪映单段音量的上限就是 +20 dB，对应 volume=10。
  - 超过 +20 dB：片段先拉满 +20，再把整条主轨包成**复合片段**，在复合片段上补剩下的。这是用户自己的做法。
  - 拉满后只差不到 1 dB，就不建复合片段。
- **量响度必须用立体声原样量**。有的素材左右声道差 6 dB，先混成单声道会偏低 3 dB 多。
- 复合片段的模板在 `presets/compound_template.json`，取自「0922直播」里用户手动建的复合片段。新建复合片段时，会把子草稿内嵌进 `materials.drafts`，同时写出 `subdraft/<id>/` 下的三个文件。

## 补逐字时间与封面候选帧（v3）

- 缺 `materials.texts[].words` 时，用 `jy_check.render(info, wav, draft_dir)` 得到时间线 16 kHz 单声道 WAV，再由 Whisper 输出 `-ojf` JSON；`& $Runner jy_words <草稿> <识别.json> -o words.json` 生成逐字时间，在精剪 spec 写 `"words_file": "words.json"`。只补缺失的逐字时间，不替换已有结果。
- `jy_words --audio <时间线.wav>` 可估计字幕漂移；`--refine 0,2` 对指定句子重新识别。短音频无法可靠估计漂移时保留字幕起止。使用本机 whisper-cli.exe 和模型，临时 WAV 采用 Windows 可重新打开的独立目录。
- `& $Runner jy_frames <草稿> <输出目录> --n 12 --keywords 词1,词2` 按时间线映射到源视频，输出原分辨率 PNG、`_contact.jpg`、UTF-8 `_picks.json`；中文路径可用。原片移动时加 `--src-dir`。目前抽帧支持普通视频片段，复合画面会明确报出限制，不能把原片帧当成剪映最终包装画面。
- `jy_audio` 只做声音分析，不能替代成片导出或实际响度测量。新版支持跨片段停顿分析、长气口压缩、复合子草稿复用。

## Windows 剪映界面验证

源包的 `安装.command`、`gui/share.sh` 和 Mac 鼠标脚本不在此 Windows 安装中执行。需要查看剪映界面时，按当前可用的 computer-use 能力操作；没有原生界面能力时交付草稿与检查结果，并说明尚未完成剪映打开、播放验证。

## 加密格式（排查用）

剪映 6+ 的草稿 = Base64(AES-256-GCM 密文 ‖ 16 字节 tag)，再把 48 个字符的 keyIv 拆成 12 块（每块 4 字符），按偏移 `[0,7,20,33,40,47,59,66,76,89,99,127]` 插进 Base64 串。解密时先抽出 keyIv，`key = keyIv[:32]`、`iv = keyIv[32:48]`，**直接取 ASCII 字节**，不做 Base64 解码。

如果解密报错（剪映升级换了格式），告诉用户当前剪映版本不受支持，把报错和剪映版本号反馈给喂鱼。


## 公开包资源说明

原作者案例、字体和音效不随包分发。案例区只演示配置结构；完整制作需要用户提供自己的草稿、媒体、字体和声音资源。原作及修改归属保留在署名和 NOTICE.md 中。
