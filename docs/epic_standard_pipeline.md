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
/home/lighthouse/video-memory-benchmark/
  GitHub 代码仓库

/home/lighthouse/video-benchmark/data/
  大文件和中间结果，不进 GitHub
  external/epic-kitchens-download-scripts-100/
    EPIC 官方下载脚本
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

## 2. 准备 vpn 仓库和数据目录

当前推荐让 `vpn` 直接拉 GitHub 仓库，代码和大文件分开：

```bash
ssh vpn
cd /home/lighthouse

test -d video-memory-benchmark/.git || \
  git clone git@github.com:PolarSnowLeopard/video-memory-benchmark.git video-memory-benchmark

cd /home/lighthouse/video-memory-benchmark
git pull
```

数据目录继续使用：

```text
/home/lighthouse/video-benchmark/data
```

确认 `vpn` 上有 EPIC 官方下载脚本：

```bash
test -f /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100/epic_downloader.py && echo ok
```

确认 COS 配置存在：

```bash
test -f ~/.cos.conf && echo ok
```

如果要生成 `P02` 或其他参与者的清单，可以直接在 `vpn` 仓库里生成：

```bash
cd /home/lighthouse/video-memory-benchmark

python3 scripts/build_epic_batch_manifest.py \
  --participants P02 \
  --uses direct_session,cut_into_sessions \
  --output data/processed/epic_kitchens_100/manifests/p02_phase1.csv
```

## 2b. 备选：从本地同步脚本和清单到 vpn

如果 `vpn` 不能访问 GitHub，再用本地 `scp` 同步：

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

当前推荐使用 GitHub 仓库中的脚本。进入 tmux 后运行：

```bash
ssh vpn
tmux new -s epic-p04

cd /home/lighthouse/video-memory-benchmark

python3 scripts/run_epic_vpn_batch.py \
  --manifest data/processed/epic_kitchens_100/manifests/p04_phase1.csv \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30 \
  --delete-raw-after-upload
```

这个脚本对每个视频执行：

1. 下载原始 MP4 到 `data/raw/EPIC-KITCHENS/Pxx/videos/`。
2. 校验原始视频 MD5。
3. 转码成 `960x540 / 16fps / H.264 CRF 28 / AAC 64kbps`。
4. 上传代理视频到 COS：`video-benchmark/epic-kitchens/Pxx/proxy_540p16/`。
5. 写入 URL 表：`data/cos_urls/p04_phase1_proxy_540p16_urls.csv`。
6. 写入状态表：`data/processed/epic_pipeline_runs/p04_phase1_status.csv`。
7. 如果传入 `--delete-raw-after-upload`，上传成功并写入 URL 表后删除原始 MP4。

默认不会删除代理视频，因为全量代理视频预计只有数十 GB，便于后续复查和重新生成 URL 表。如果希望 `vpn` 只作为临时加工节点，可以额外传入：

```bash
--delete-proxy-after-upload
```

断开 tmux：

```bash
Ctrl-b d
```

查看进度：

```bash
ssh vpn 'tail -n 20 /home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/p04_phase1_status.csv'
```

### 两个参与者并行

如果单路下载没有打满公网带宽，可以用两个 tmux 会话分别跑不同参与者。这样每个参与者有独立状态表和 URL 表，避免多个进程写同一个 CSV。

窗口 1 跑 `P04`：

```bash
tmux new -s epic-p04

cd /home/lighthouse/video-memory-benchmark

python3 scripts/run_epic_vpn_batch.py \
  --manifest data/processed/epic_kitchens_100/manifests/p04_phase1.csv \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30 \
  --delete-raw-after-upload
```

窗口 2 跑 `P02`：

```bash
tmux new -s epic-p02

cd /home/lighthouse/video-memory-benchmark

python3 scripts/run_epic_vpn_batch.py \
  --manifest data/processed/epic_kitchens_100/manifests/p02_phase1.csv \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30 \
  --delete-raw-after-upload
```

查看两个进度：

```bash
tail -n 20 /home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/p04_phase1_status.csv
tail -n 20 /home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/p02_phase1_status.csv
```

### 五路参与者队列

如果要持续跑很多参与者，推荐使用队列脚本，让它最多同时启动 5 个参与者任务。某个参与者跑完后，脚本会自动启动下一个参与者。

注意：不要让队列和手动 tmux 任务同时处理同一个参与者，否则会重复下载同一批视频。如果 `P04` 或 `P02` 已经在单独窗口里跑，要么等它们结束，要么在队列的 `--participants` 里排除它们。

先跑筛选后的候选视频：

```bash
tmux new -s epic-queue-candidates

cd /home/lighthouse/video-memory-benchmark
git pull

python3 scripts/run_epic_vpn_participant_queue.py \
  --participants all-candidates \
  --selection candidates \
  --max-workers 5 \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30 \
  --run-name epic_candidates_5w
```

如果要跑 EPIC-KITCHENS-100 全部 700 个视频，用：

```bash
tmux new -s epic-queue-all

cd /home/lighthouse/video-memory-benchmark
git pull

python3 scripts/run_epic_vpn_participant_queue.py \
  --participants all \
  --selection all-videos \
  --max-workers 5 \
  --data-root /home/lighthouse/video-benchmark/data \
  --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
  --python python3 \
  --cos-prefix video-benchmark/epic-kitchens \
  --url-expire-days 30 \
  --run-name epic100_all_5w
```

队列脚本默认会在代理视频上传成功后删除原始 MP4，并保留 `540p16` 代理视频。如果需要保留原片，额外加：

```bash
--keep-raw
```

五路并行时，每个转码进程默认使用 2 个 `ffmpeg` 线程。8 核机器上这是比较稳妥的起点。如果 CPU 长期很低，可以改成：

```bash
--ffmpeg-threads 3
```

队列状态：

```bash
tail -n 20 /home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/epic100_all_5w_status.csv
```

单个参与者日志：

```bash
tail -f /home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/epic100_all_5w_logs/P04.log
```

单个参与者的批处理状态和 URL 表仍然会分开写，例如：

```text
/home/lighthouse/video-benchmark/data/processed/epic_pipeline_runs/p04_all_videos_status.csv
/home/lighthouse/video-benchmark/data/cos_urls/p04_all_videos_proxy_540p16_urls.csv
```

重新运行队列时，已经同时满足“状态为 `ok`”和“URL 表已有链接”的视频会跳过，不会因为原片已删除而重新下载。

## 4. 把 URL 表交给集群

等 `vpn` 端至少完成几个视频后，把 URL 表上传到 COS，生成可下载链接：

```bash
ssh vpn

python3 /home/lighthouse/video-memory-benchmark/scripts/upload_epic_to_cos.py \
  --prefix video-benchmark/cluster_inputs \
  --url-expire-days 30 \
  --output-csv /home/lighthouse/video-benchmark/data/cos_urls/p04_phase1_url_csv_download.csv \
  /home/lighthouse/video-benchmark/data/cos_urls/p04_phase1_proxy_540p16_urls.csv
```

`p04_phase1_url_csv_download.csv` 里的 `signed_url` 就是集群下载 URL 表的链接。

如果同时跑了 `P02`，对应再上传一次：

```bash
python3 /home/lighthouse/video-memory-benchmark/scripts/upload_epic_to_cos.py \
  --prefix video-benchmark/cluster_inputs \
  --url-expire-days 30 \
  --output-csv /home/lighthouse/video-benchmark/data/cos_urls/p02_phase1_url_csv_download.csv \
  /home/lighthouse/video-benchmark/data/cos_urls/p02_phase1_proxy_540p16_urls.csv
```

如果用五路队列跑全量，可以先合并所有参与者 URL 表，再上传给集群：

```bash
cd /home/lighthouse/video-benchmark/data/cos_urls

out=epic100_all_videos_proxy_540p16_urls.csv
first=1
rm -f "$out"

for f in p*_all_videos_proxy_540p16_urls.csv; do
  if [ "$first" = 1 ]; then
    cat "$f" > "$out"
    first=0
  else
    tail -n +2 "$f" >> "$out"
  fi
done

python3 /home/lighthouse/video-memory-benchmark/scripts/upload_epic_to_cos.py \
  --prefix video-benchmark/cluster_inputs \
  --url-expire-days 30 \
  --output-csv /home/lighthouse/video-benchmark/data/cos_urls/epic100_all_videos_url_csv_download.csv \
  /home/lighthouse/video-benchmark/data/cos_urls/epic100_all_videos_proxy_540p16_urls.csv
```

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
  --max-tokens 8192
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
  --max-tokens 12288 \
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
