# Video Memory Benchmark

这个仓库用于构建和评估“视频模态智能体跨会话长期记忆”benchmark。当前已经实现的是 EPIC-KITCHENS-100 子流程：从官方视频下载、低清代理视频转码、对象存储分发，到集群侧调用视觉语言模型生成结构化中间 JSON。

当前定位：

- 不是训练集仓库；目标是构建 benchmark 和评估框架。
- VLM 输出不是最终标注，而是候选事件、候选状态变化、候选记忆事实的中间层。
- 支持分布式执行：本地生成清单，`vpn` 服务器下载/转码/上传，集群调用 VLM。
- 也支持单机复现：只要同一台机器具备下载权限、ffmpeg、对象存储配置和 VLM 服务，也可以按同一套脚本顺序运行。

## 当前流水线

```text
EPIC 官方元信息
  -> 生成 manifest
  -> 下载原始 MP4
  -> 校验 MD5
  -> 转码为 540p / 16fps / 有音频代理视频
  -> 上传对象存储并生成签名 URL 表
  -> 集群 VLM 批量读取 URL
  -> 生成 raw JSON 与 clean JSON
  -> 后续证据链、问题和评估协议构建
```

## 目录结构

```text
configs/
  运行环境和批处理配置样例，不放密钥。

data/
  processed/        可提交的轻量 manifest 和统计结果。
  external/         外部仓库和数据，忽略。
  raw/              原始视频，忽略。
  proxy/            代理视频，忽略。
  tmp/              临时包、签名链接、模型输出，忽略。

docs/
  流水线文档、运行说明和设计记录。

prompts/
  VLM 抽取提示词。

reports/
  数据集分析报告和阶段性实验分析。

scripts/
  可直接运行的流水线脚本。
```

## 快速开始

本地生成 P04 第一批清单：

```bash
PY=/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3

$PY scripts/build_epic_batch_manifest.py \
  --participants P04 \
  --uses direct_session,cut_into_sessions \
  --output data/processed/epic_kitchens_100/manifests/p04_phase1.csv
```

在 `vpn` 上按 manifest 下载、转码、上传：

```bash
python3 /home/lighthouse/video-benchmark/scripts/run_epic_vpn_batch.py \
  --manifest /home/lighthouse/video-benchmark/manifests/p04_phase1.csv \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30
```

在集群侧批量调用 VLM：

```bash
python qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv p04_phase1_proxy_540p16_urls.csv \
  --prompt-file video_event_schema_zh.txt \
  --output-dir outputs/p04_phase1 \
  --fps 1 \
  --max-tokens 4096
```

更完整的命令见 [docs/epic_standard_pipeline.md](docs/epic_standard_pipeline.md)。

## 核心脚本

- `scripts/analyze_epic_kitchens_100.py`：统计 EPIC-KITCHENS-100 元信息。
- `scripts/build_epic_batch_manifest.py`：按参与者或视频编号生成批处理 manifest。
- `scripts/run_epic_vpn_batch.py`：下载原始视频、转码代理视频、上传 COS、写状态表。
- `scripts/upload_epic_to_cos.py`：上传任意文件到腾讯云 COS 并生成签名 URL 表。
- `scripts/qwen_video_batch.py`：按 URL 表批量调用 OpenAI 兼容 VLM 服务。
- `scripts/qwen_video_probe.py`：单视频调试用。
- `scripts/extract_qwen_json.py`：从原始模型响应中抽取干净 JSON。
- `scripts/prepare_qwen_cluster_bundle.py`：生成集群最小运行包。

## 数据和密钥

仓库不提交：

- 原始视频、代理视频、帧文件；
- 外部下载脚本仓库；
- COS 密钥、签名 URL 长期文件；
- 集群模型输出的大体积临时结果。

提交：

- 轻量 manifest；
- 统计结果；
- 报告；
- 脚本；
- 提示词；
- 环境配置样例。

## 当前进度

已经跑通：

- P04 三个样例视频的原始下载和 MD5 校验；
- `540p / 16fps / AAC` 代理视频转码；
- COS 上传与签名 URL 表生成；
- Qwen3.5 A3B 视频输入链路；
- raw JSON 到 clean JSON 的抽取；
- 与 EPIC 官方动作标注的初步对齐分析。

下一步：

- 启动 P04 phase1 的 21 个候选视频长跑；
- 持续产出 VLM clean JSON；
- 设计跨 session 证据链 schema；
- 由候选事件生成 benchmark 问题和评估协议。
