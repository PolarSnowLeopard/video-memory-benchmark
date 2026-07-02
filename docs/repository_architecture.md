# 仓库组织建议

这个仓库最终应覆盖三件事：

1. 数据构建：从公开视频数据集生成可评估的多会话视频记忆样本。
2. 评估框架：定义输入协议、记忆更新协议、问题格式、证据链和评分规则。
3. 评估实验：对不同模型和 agent memory 方法跑同一套 benchmark，并记录结果。

当前完成的是第一部分中的 EPIC-KITCHENS-100 数据处理子流程。

## 设计原则

### Manifest 驱动

任何批处理都从一个 CSV manifest 开始。manifest 是轻量、可提交、可复现的，不依赖某台机器的本地文件状态。

当前 manifest 字段包括：

- `participant_id`
- `video_id`
- `video_sequence`
- `duration_sec`
- `fps`
- `source_subset`
- `source_split`
- `labelled_actions`
- `suggested_use`

后续可以扩展：

- session 切片起止时间；
- 真实世界日期时间；
- 是否同一厨房；
- 是否进入人工审核；
- 证据链构建状态。

### 数据文件和派生文件分离

仓库只提交轻量文件：

- manifest；
- 统计 CSV；
- schema；
- prompt；
- 报告；
- 代码。

不提交重文件：

- 原始视频；
- 代理视频；
- 抽帧；
- 模型大批量输出；
- 对象存储签名 URL；
- 密钥。

### 分布式运行但单机可复现

现在的实际运行环境是：

- 本地电脑：生成 manifest、写脚本、看报告；
- `vpn`：下载 EPIC 视频、转码、上传 COS；
- 集群：启动 VLM 服务，批量处理视频 URL。

但脚本本身不绑定这三台机器。单机复现时，只要同一台机器满足下面条件，也可以按同一顺序跑完：

- 能访问 EPIC-KITCHENS 视频下载地址；
- 有 `ffmpeg`；
- 有 Python；
- 配好 COS 或替换成其他对象存储；
- 能访问一个 OpenAI 兼容 VLM 服务。

## 推荐顶层结构

```text
configs/
  epic_kitchens_100/
    pipeline.env.example

data/
  processed/
    epic_kitchens_100/
      manifests/
      video_summary.csv
      participant_summary.csv
  external/        # ignored
  raw/             # ignored
  proxy/           # ignored
  tmp/             # ignored

docs/
  epic_standard_pipeline.md
  qwen_vl_video_pipeline.md
  repository_architecture.md

prompts/
  video_event_schema_zh.txt

reports/
  epic_kitchens_100/

scripts/
  analyze_epic_kitchens_100.py
  build_epic_batch_manifest.py
  run_epic_vpn_batch.py
  qwen_video_batch.py
  qwen_video_probe.py
  extract_qwen_json.py
  upload_epic_to_cos.py
```

未来可以增加：

```text
schemas/
  video_event.schema.json
  memory_candidate.schema.json
  benchmark_question.schema.json

src/video_memory_benchmark/
  datasets/
  pipelines/
  evaluation/
  metrics/

experiments/
  qwen35_a3b/
  gpt_like_model/
  baseline_full_context/
  baseline_retrieval_memory/
```

## EPIC-KITCHENS-100 子流程

### 阶段 0：元信息分析

输入：

- EPIC-KITCHENS-100 官方标注仓库。

输出：

- `data/processed/epic_kitchens_100/video_summary.csv`
- `data/processed/epic_kitchens_100/participant_summary.csv`
- `data/processed/epic_kitchens_100/candidate_videos.csv`
- `reports/epic_kitchens_100/*`

脚本：

```bash
python scripts/analyze_epic_kitchens_100.py
```

### 阶段 1：生成 manifest

输入：

- `candidate_videos.csv`
- `video_summary.csv`

输出：

- `data/processed/epic_kitchens_100/manifests/*.csv`

脚本：

```bash
python scripts/build_epic_batch_manifest.py \
  --participants P04 \
  --uses direct_session,cut_into_sessions \
  --output data/processed/epic_kitchens_100/manifests/p04_phase1.csv
```

### 阶段 2：视频处理

输入：

- manifest

输出：

- 原始 MP4；
- `540p16` 代理视频；
- COS URL 表；
- 状态表。

脚本：

```bash
python scripts/run_epic_vpn_batch.py \
  --manifest data/processed/epic_kitchens_100/manifests/p04_phase1.csv \
  --data-root data \
  --downloader-dir data/external/epic-kitchens-download-scripts-100 \
  --cos-prefix video-benchmark/epic-kitchens
```

### 阶段 3：VLM 中间特征抽取

输入：

- COS URL 表；
- `prompts/video_event_schema_zh.txt`

输出：

- 原始 VLM 响应；
- clean JSON；
- batch status。

脚本：

```bash
python scripts/qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv p04_phase1_proxy_540p16_urls.csv \
  --prompt-file prompts/video_event_schema_zh.txt \
  --output-dir outputs/p04_phase1 \
  --fps 1 \
  --max-tokens 8192
```

### 阶段 4：Benchmark 构建

还未实现，建议下一步定义：

- 单视频状态图增量 schema；
- `memory_candidate` schema；
- `cross_session_memory` schema；
- `benchmark_question` schema；
- 证据链格式；
- 评分规则。

这一阶段不应直接信任 VLM 输出，而应把 VLM 输出作为候选，再结合官方动作标注、跨 session 聚合和人工抽检。

## 复现实验记录

每次批量处理都应保留：

- manifest 文件；
- prompt 版本；
- 模型名；
- VLM 服务参数；
- 输入 fps；
- 输出 token 上限；
- 转码参数；
- 状态表；
- clean JSON 产物路径。

这样后续比较不同模型或不同提示词时，才能判断差异来自模型能力，而不是输入数据或预处理参数变化。
