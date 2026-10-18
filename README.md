# 剪映草稿读取与修改

读取剪映草稿的轨道、字幕和素材，导出逐字稿并在副本中修改。

- 输入：剪映草稿和明确的修改范围
- 输出：时间线摘要、SRT、缺失素材报告和修改后副本

## 使用

把本目录复制到支持 Skill 的工具的技能目录，然后用自然语言描述任务。使用说明在 [SKILL.md](SKILL.md)。

## 公开版范围

保留方法、脚本与模板。私人 CSV、完整逐字稿、账号凭据、克隆声音、原始课程和第三方媒体不随包发布。示例数据用于演示，不代表真实传播效果。

基于喂鱼方案的 Windows/Codex 改编，见 [NOTICE.md](NOTICE.md)。两个剪映 Skill 需要并排安装。Python 依赖见 ../jianying-draft/requirements.txt；FFmpeg、ffprobe、whisper.cpp 与模型需用户按需要另行配置。
