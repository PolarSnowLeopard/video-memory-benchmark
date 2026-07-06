# Video Memory Benchmark

这个仓库用于构建和评估“视频模态智能体跨会话长期记忆”benchmark。当前主线是 EPIC-KITCHENS-100 子流程：从官方视频下载、低清代理视频转码、对象存储分发，到集群侧调用视觉语言模型生成分层结构化证据 JSON。

当前定位：

- 不是训练集仓库；目标是构建 benchmark 和评估框架。
- VLM 输出不是最终 benchmark 标注，而是候选地点、物体、事件、状态变化和跨会话记忆事实的中间层。
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
  -> 集群侧下载代理视频并切成 30 秒 micro-clip
  -> VLM 抽取 micro-clip 局部证据
  -> LLM 聚合为 120 秒 local window
  -> LLM 聚合为完整 source/session 证据
  -> 查看、校验、修复和人工审核
  -> 后续问题生成、证据链和评估协议构建
```

分层证据抽取只用于隐藏参考证据构建。真正评测 agent 时，baseline 如何处理视频和构建记忆由 baseline 自己决定。

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
  VLM 抽取和分层聚合提示词。

reports/
  数据集分析报告和阶段性实验分析。

scripts/
  可直接运行的流水线脚本。
```

## 快速开始

### 1. 本地或 vpn 生成参与者 manifest

```bash
PY=/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3

$PY scripts/build_epic_batch_manifest.py \
  --participants P30 \
  --uses direct_session,cut_into_sessions \
  --output data/processed/epic_kitchens_100/manifests/p30_all_videos.csv
```

### 2. 在 vpn 上下载、转码、上传代理视频

```bash
cd /home/lighthouse/video-memory-benchmark

python3 scripts/run_epic_vpn_batch.py \
  --manifest data/processed/epic_kitchens_100/manifests/p30_all_videos.csv \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30 \
  --delete-raw-after-upload
```

全量处理推荐使用 5 路参与者队列，见 [docs/epic_standard_pipeline.md](docs/epic_standard_pipeline.md)。

### 3. 在集群侧运行分层证据抽取

集群上先把某个参与者的 `*_proxy_540p16_urls.csv` 下载到：

```text
data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv
```

然后按 30 秒切片：

```bash
python3 scripts/prepare_video_sessions_for_inference.py \
  --video-url-csv data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv \
  --data-root data \
  --source-cache-root data/proxy_from_cos \
  --download-missing-source \
  --session-duration-sec 30 \
  --min-tail-sec 10 \
  --local-url-base http://127.0.0.1:18080
```

另开窗口提供本地视频 HTTP 服务：

```bash
python3 -m http.server 18080 --bind 0.0.0.0 --directory data/sessions
```

运行 micro-clip VLM 抽取：

```bash
python3 scripts/qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv data/cos_urls/p30_all_videos_sessions_30s_urls.csv \
  --prompt-file prompts/video_micro_evidence_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_micro_30s \
  --fps 1 \
  --max-tokens 4096 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

再构建 120 秒 window 输入、运行 window 聚合、构建完整 session 输入并运行 session 聚合。完整命令见 [docs/hierarchical_evidence_pipeline.md](docs/hierarchical_evidence_pipeline.md)。

### 4. 查看一个分层结果示例

本地生成静态 HTML：

```bash
$PY scripts/build_hierarchical_example_viewer.py \
  --video-id P30_03 \
  --output data/tmp/viewers/p30_03_hierarchical_viewer.html
```

当前线上示例：

```text
http://yufanwenshu.cn:8000/epic_kitchens_100/p30_03_hierarchical_viewer.html
```

该页面内嵌了提取出的 JSON，并通过 COS 签名 URL 播放代理视频。不要把带签名视频链接的 HTML 提交到 GitHub；对外分享还需要遵守 EPIC-KITCHENS 数据许可。

## 核心脚本

- `scripts/analyze_epic_kitchens_100.py`：统计 EPIC-KITCHENS-100 元信息。
- `scripts/build_epic_batch_manifest.py`：按参与者或视频编号生成批处理 manifest。
- `scripts/run_epic_vpn_batch.py`：下载原始视频、转码代理视频、上传 COS、写状态表。
- `scripts/run_epic_vpn_participant_queue.py`：按参与者并行跑下载、转码、上传队列。
- `scripts/upload_epic_to_cos.py`：上传任意文件到腾讯云 COS 并生成签名 URL 表。
- `scripts/prepare_video_sessions_for_inference.py`：在推理前把代理视频切成标准长度 session/micro-clip。
- `scripts/qwen_video_batch.py`：按 URL 表批量调用 OpenAI 兼容 VLM 服务，输出视频证据 JSON。
- `scripts/build_hierarchical_evidence_inputs.py`：把 micro-clip JSON 聚合为 window/session 文本输入。
- `scripts/qwen_text_jsonl_batch.py`：按 JSONL 批量调用 OpenAI 兼容文本接口，输出 window/session 聚合 JSON。
- `scripts/build_hierarchical_example_viewer.py`：为单个视频生成可浏览的 HTML 分层证据示例。
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
- 与 EPIC 官方动作标注的初步对齐分析；
- P30 全量分层试跑：25 个原始视频、594 个 30 秒 micro-clip、156 个 120 秒 window、25 个完整 session；
- P30_03 HTML 分层证据查看器。

截至当前批处理进度，`vpn` 上已有 13 个参与者的 `540p16` 代理视频全部处理完成并上传 COS，合计 415 个视频。该状态来自运行时记录，不作为代码层面的固定假设。

下一步：

- 为分层 JSON 增加确定性 validator/normalizer；
- 对 P30 分层结果做抽样人工审核；
- 扩展到已完成代理视频的更多参与者；
- 设计跨 session 参考证据图 schema；
- 由候选事件生成 benchmark 问题和评估协议。
