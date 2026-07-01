# EPIC-KITCHENS 标准流水线

目标：尽快持续产出 VLM 初步结构化 JSON，而不是等完整数据全部下载完。流水线按 manifest 逐个视频处理，任何一步失败都写状态表，重新运行会跳过已完成结果。

## 目录约定

本地仓库：

```text
data/processed/epic_kitchens_100/manifests/
  批处理视频清单
scripts/
  本地、vpn、集群共用脚本
prompts/video_event_schema_zh.txt
  第一阶段事件抽取提示词
```

`vpn` 服务器：

```text
/home/lighthouse/video-benchmark/
  scripts/
  manifests/
  data/
    raw/EPIC-KITCHENS/Pxx/videos/*.MP4
    proxy/Pxx/*_540p16.mp4
    cos_urls/*_proxy_540p16_urls.csv
    processed/epic_pipeline_runs/*_status.csv
```

集群：

```text
/workspace/qwen_video_probe/
  qwen_video_batch.py
  qwen_video_probe.py
  extract_qwen_json.py
  video_event_schema_zh.txt
  outputs/
```

## 1. 本地生成视频清单

先跑 P04，建议包含 `direct_session` 和 `cut_into_sessions`。后续要扩到多个参与者，只需要改 `--participants`。

```bash
PY=/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3

$PY scripts/build_epic_batch_manifest.py \
  --participants P04 \
  --uses direct_session,cut_into_sessions \
  --output data/processed/epic_kitchens_100/manifests/p04_phase1.csv
```

如果想先只跑几个视频验证：

```bash
$PY scripts/build_epic_batch_manifest.py \
  --video-ids P04_24,P04_29,P04_106 \
  --output data/processed/epic_kitchens_100/manifests/p04_smoke.csv
```

## 2. 同步脚本和清单到 vpn

```bash
ssh vpn 'mkdir -p /home/lighthouse/video-benchmark/scripts /home/lighthouse/video-benchmark/manifests'

scp \
  scripts/run_epic_vpn_batch.py \
  scripts/upload_epic_to_cos.py \
  vpn:/home/lighthouse/video-benchmark/scripts/

scp \
  data/processed/epic_kitchens_100/manifests/p04_phase1.csv \
  vpn:/home/lighthouse/video-benchmark/manifests/
```

确认 `vpn` 上有下载脚本仓库：

```bash
ssh vpn 'test -f /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100/epic_downloader.py && echo ok'
```

如果不存在，需要先在 `vpn` 上 clone EPIC 下载脚本到该目录。

## 3. 在 vpn 后台跑下载、转码、上传

进入 tmux 后运行：

```bash
ssh vpn
tmux new -s epic-p04

python3 /home/lighthouse/video-benchmark/scripts/run_epic_vpn_batch.py \
  --manifest /home/lighthouse/video-benchmark/manifests/p04_phase1.csv \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30
```

这个脚本对每个视频执行：

1. 下载原始 MP4 到 `data/raw/EPIC-KITCHENS/Pxx/videos/`。
2. 校验原始视频 MD5。
3. 转码成 `960x540 / 16fps / H.264 CRF 28 / AAC 64kbps`。
4. 上传代理视频到 COS：`video-benchmark/epic-kitchens/Pxx/proxy_540p16/`。
5. 写入 URL 表：`data/cos_urls/p04_phase1_proxy_540p16_urls.csv`。
6. 写入状态表：`data/processed/epic_pipeline_runs/p04_phase1_status.csv`。

断开 tmux：

```bash
Ctrl-b d
```

查看进度：

```bash
ssh vpn 'tail -n 20 /home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/p04_phase1_status.csv'
```

## 4. 把 URL 表交给集群

等 `vpn` 端至少完成几个视频后，把 URL 表上传到 COS，生成可下载链接：

```bash
ssh vpn

python3 /home/lighthouse/video-benchmark/scripts/upload_epic_to_cos.py \
  --prefix video-benchmark/cluster_inputs \
  --url-expire-days 30 \
  --output-csv /home/lighthouse/video-benchmark/data/cos_urls/p04_phase1_url_csv_download.csv \
  /home/lighthouse/video-benchmark/data/cos_urls/p04_phase1_proxy_540p16_urls.csv
```

`p04_phase1_url_csv_download.csv` 里的 `signed_url` 就是集群下载 URL 表的链接。

## 5. 集群批量调用 VLM

集群上先启动 VLM 服务，然后进入试跑包目录：

```bash
cd /workspace/qwen_video_probe/qwen_cluster_bundle
mkdir -p outputs
```

下载第 4 步生成的 URL 表：

```bash
curl -L -o p04_phase1_proxy_540p16_urls.csv '<第4步 signed_url>'
```

批量调用：

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

输出：

```text
outputs/p04_phase1/P04_xx.json
outputs/p04_phase1/P04_xx.clean.json
outputs/p04_phase1/batch_status.csv
```

如果出现 `raw_only` 且 `finish_reason=length`，优先重跑这些视频：

```bash
python qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv p04_phase1_proxy_540p16_urls.csv \
  --prompt-file video_event_schema_zh.txt \
  --output-dir outputs/p04_phase1 \
  --video-ids P04_29 \
  --fps 1 \
  --max-tokens 8192 \
  --overwrite
```

## 扩量策略

第一阶段只跑 P04，拿到 10 个左右 session 的 JSON，先验证同一人/同一厨房跨 session 记忆任务是否成立。

第二阶段扩到：

```text
P04,P02,P22,P01,P03
```

生成清单：

```bash
$PY scripts/build_epic_batch_manifest.py \
  --participants P04,P02,P22,P01,P03 \
  --uses direct_session,cut_into_sessions \
  --output data/processed/epic_kitchens_100/manifests/phase2_top5_participants.csv
```

如果 `vpn` 下载很慢，不要等全量。每完成一批 URL 表，就把已经完成的视频送到集群跑 VLM，持续滚动产出中间 JSON。
