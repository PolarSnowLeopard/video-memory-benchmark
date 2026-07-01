# EPIC-KITCHENS 本地入手流程

## 环境约束

本机默认 `python3` 会被系统杀掉，因此不要使用系统 Python、conda 或任何 conda 环境。

当前可用的是 Codex 自带 Python：

```bash
/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
```

这个路径已经验证可用。当前统计脚本只使用 Python 标准库，不依赖 pandas、conda 或额外安装包。

## 当前目录结构

```text
data/external/epic-kitchens-100-annotations/
  EPIC-KITCHENS-100 公开标注仓库，不应提交到 git

data/processed/epic_kitchens_100/
  从标注仓库生成的轻量统计表

reports/epic_kitchens_100/
  中文元信息初筛报告

scripts/analyze_epic_kitchens_100.py
  可重复运行的统计脚本
```

## 已完成

已经下载 EPIC-KITCHENS-100 的公开标注仓库，没有下载任何视频。

已经生成：

- `data/processed/epic_kitchens_100/video_summary.csv`
- `data/processed/epic_kitchens_100/participant_summary.csv`
- `data/processed/epic_kitchens_100/candidate_participants.csv`
- `data/processed/epic_kitchens_100/candidate_videos.csv`
- `data/processed/epic_kitchens_100/top_verbs.csv`
- `data/processed/epic_kitchens_100/top_nouns.csv`
- `reports/epic_kitchens_100/metadata_report.md`

## 重新运行统计

```bash
/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 scripts/analyze_epic_kitchens_100.py
```

## 第一批建议查看的参与者

按总时长、视频数、动作标注量、动词类别和名词类别综合排序，第一批建议看：

```text
P04, P02, P22, P01, P03
```

这些参与者都适合构造同一厨房、同一人的多会话记忆流。

## 第一批建议下载的视频

更具体的视频清单在：

```text
data/processed/epic_kitchens_100/candidate_videos.csv
```

其中 `suggested_use` 的含义：

- `direct_session`：视频时长和动作密度适合直接作为一个会话。
- `cut_into_sessions`：视频较长，适合切成多个 3 到 5 分钟会话。
- `short_support_session`：可辅助观察，但不应优先下载。
- `low_priority`：暂不建议作为第一批样本。

## 下一步建议

1. 先从一个参与者开始，例如 `P04` 或 `P02`。
2. 只下载 `candidate_videos.csv` 中该参与者的 5 到 10 个视频。
3. 将原始视频转成低清代理视频，例如 360p 或 540p。
4. 用代理视频人工浏览，确认是否真的存在跨会话可记忆对象、位置、动作流程和状态变化。
5. 再设计 20 到 50 个样例问题，验证 benchmark 任务定义是否成立。

后续如果要用视觉语言模型标注，不建议直接把完整长视频丢给模型。更稳的做法是先按 30 秒或 60 秒切窗口，抽关键帧和短片段，让模型生成结构化事件表，再由事件表生成跨会话问题。
