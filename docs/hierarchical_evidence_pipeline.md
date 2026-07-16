# 分层视频证据抽取试跑流程

本文档记录当前第一版“30 秒 micro-clip -> 2 分钟 window -> 完整 session”的隐藏参考证据构建流程。该流程只用于 benchmark 数据标注、证据链生成和人工审核；被评测 agent 不会看到这些中间结果。

## 层级定义

| 层级 | 默认长度 | 输入 | 输出 |
|---|---:|---|---|
| micro-clip | 30 秒 | 视频片段 | 局部地点、物体、事件、状态观察、状态变化 |
| local window | 120 秒 | 4 个左右 micro-clip JSON | 局部事件链、窗口结束状态、候选事实 |
| source/session | 完整原始视频 | 多个 window JSON | 该次会话的状态更新和跨会话候选证据 |

短视频会自然退化为不足 120 秒的 window 或不足完整窗口的 session，不需要特殊处理。

## 1. 准备 30 秒 micro-clip

以下以 P30 为例。假设集群上已经有：

```text
/workspace/video-memory-benchmark/data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv
```

执行：

```bash
cd /workspace/video-memory-benchmark
git pull --ff-only
python3 -m pip install -r requirements/cluster.txt

python3 scripts/prepare_video_sessions_for_inference.py \
  --video-url-csv data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv \
  --data-root data \
  --source-cache-root data/proxy_from_cos \
  --download-missing-source \
  --session-duration-sec 30 \
  --min-tail-sec 10 \
  --local-url-base http://127.0.0.1:18080 \
  --cut-mode reencode \
  --reencode-crf 23 \
  --fail-fast
```

固定时长的模型输入必须使用精确重编码。直接码流拷贝受视频关键帧和 GOP
边界影响，可能把相邻片段的十几秒内容带入当前片段。脚本默认使用
`reencode`，并在状态表和 URL 表中记录 `actual_duration_sec`、
`duration_error_sec` 和 `duration_validated`；实际时长误差超过 0.25 秒时不会
进入推理清单。历史状态表没有 `duration_validated=True` 的片段不会被当作已完成。

输出：

```text
data/cos_urls/p30_all_videos_sessions_30s_urls.csv
data/sessions/P30/sessions_30s/*.mp4
data/processed/epic_pipeline_runs/p30_all_videos_sessions_30s_status.csv
```

如果旧片段是用 `--cut-mode copy` 生成的，重新生成时显式增加：

```bash
--overwrite-sessions --rerun-completed
```

在另一个窗口启动本地 HTTP 服务：

```bash
cd /workspace/video-memory-benchmark
python3 -m http.server 18080 --bind 0.0.0.0 --directory data/sessions
```

## 2. 运行 30 秒 micro-clip VLM 抽取

先烟测 12 个片段：

```bash
cd /workspace/video-memory-benchmark

python3 scripts/qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv data/cos_urls/p30_all_videos_sessions_30s_urls.csv \
  --prompt-file prompts/video_micro_evidence_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_micro_30s \
  --fps 1 \
  --max-tokens 4096 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}' \
  --limit 12
```

确认状态：

```bash
python3 - <<'PY'
import csv
from collections import Counter
p='outputs/epic_kitchens_100/p30_micro_30s/batch_status.csv'
rows=list(csv.DictReader(open(p, newline='', encoding='utf-8')))
print('rows', len(rows))
print(Counter(r['status'] for r in rows))
for r in rows:
    if r['status'] != 'ok':
        print(r['record_id'], r['status'], r.get('finish_reason'), r.get('error'))
PY
```

烟测正常后跑全量：

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

P30 预计约 600 个 30 秒 micro-clip。

## 3. 构建 2 分钟 window 输入

```bash
cd /workspace/video-memory-benchmark
mkdir -p outputs/epic_kitchens_100/p30_hierarchical

python3 scripts/build_hierarchical_evidence_inputs.py windows \
  --micro-url-csv data/cos_urls/p30_all_videos_sessions_30s_urls.csv \
  --micro-output-dir outputs/epic_kitchens_100/p30_micro_30s \
  --window-sec 120 \
  --output-jsonl outputs/epic_kitchens_100/p30_hierarchical/window_inputs_30s_120s.jsonl
```

如果 micro-clip 尚未全部跑完，但想先看部分结果，可以加：

```bash
--allow-missing
```

## 4. 运行 2 分钟 window 聚合

先烟测 5 个 window：

```bash
python3 scripts/qwen_text_jsonl_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --input-jsonl outputs/epic_kitchens_100/p30_hierarchical/window_inputs_30s_120s.jsonl \
  --prompt-file prompts/video_window_aggregation_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_windows_120s \
  --max-tokens 8192 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}' \
  --limit 5
```

烟测正常后跑全量：

```bash
python3 scripts/qwen_text_jsonl_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --input-jsonl outputs/epic_kitchens_100/p30_hierarchical/window_inputs_30s_120s.jsonl \
  --prompt-file prompts/video_window_aggregation_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_windows_120s \
  --max-tokens 8192 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

## 5. 构建完整 session 输入

```bash
python3 scripts/build_hierarchical_evidence_inputs.py sessions \
  --window-input-jsonl outputs/epic_kitchens_100/p30_hierarchical/window_inputs_30s_120s.jsonl \
  --window-output-dir outputs/epic_kitchens_100/p30_windows_120s \
  --output-jsonl outputs/epic_kitchens_100/p30_hierarchical/session_inputs_30s_120s.jsonl
```

P30 会得到 25 条 session 输入，对应 25 个原始视频。

## 6. 运行完整 session 聚合

```bash
python3 scripts/qwen_text_jsonl_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --input-jsonl outputs/epic_kitchens_100/p30_hierarchical/session_inputs_30s_120s.jsonl \
  --prompt-file prompts/video_session_aggregation_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_sessions_full \
  --max-tokens 12288 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

长 session 可能触发 `finish_reason=length`。P30 试跑中 `P30_107` 的 session 聚合需要把输出上限提高到 `16384` 后重跑：

```bash
python3 scripts/qwen_text_jsonl_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --input-jsonl outputs/epic_kitchens_100/p30_hierarchical/session_inputs_30s_120s.jsonl \
  --prompt-file prompts/video_session_aggregation_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_sessions_full \
  --record-ids P30_107 \
  --max-tokens 16384 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}' \
  --overwrite
```

## 7. 结果目录

```text
outputs/epic_kitchens_100/p30_micro_30s/
  P30_01_s000.json
  P30_01_s000.clean.json
  batch_status.csv

outputs/epic_kitchens_100/p30_hierarchical/
  window_inputs_30s_120s.jsonl
  session_inputs_30s_120s.jsonl

outputs/epic_kitchens_100/p30_windows_120s/
  P30_01_w000.json
  P30_01_w000.clean.json
  batch_status.csv

outputs/epic_kitchens_100/p30_sessions_full/
  P30_01.json
  P30_01.clean.json
  batch_status.csv
```

P30 完整试跑的规模：

```text
30 秒 micro-clip：594 条
120 秒 window：156 条
完整 source/session：25 条
```

实际状态中可能出现 `skipped`，通常表示对应 clean JSON 已存在、脚本跳过重算，不等于失败。

## 8. 多参与者统一抽取

当代理视频清单已经按参与者放在同一目录时，可以用编排器顺序执行完整三层流程：

```bash
python3 scripts/run_hierarchical_extraction_participants.py \
  --manifest-dir data/cluster_inputs/epic37_proxy_manifests \
  --participants all \
  --expected-participants 37 \
  --data-root data/epic_kitchens_100_precise_v2 \
  --output-root outputs/epic_kitchens_100_precise_v2 \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b
```

编排器对每个参与者依次执行：30 秒切分、micro 抽取与校验、120 秒 window 聚合与校验、完整 source/session 聚合与校验。每层失败结果最多自动重试 3 次；重试只请求缺失记录，最后一次会提高输出 token 上限。数量或结构校验仍不完整时立即停止并报告缺失编号。成功的 `*.clean.json` 会被复用，因此相同命令可直接断点续跑，不会覆盖已有成功结果。
运行时终端会显示参与者、micro、window 和 session 各层的进度条、
累计耗时和预计剩余时间。预计值按本次运行中已完成任务的平均耗时外推，
开始阶段可能波动较大。

多节点执行时，每个节点独立启动一套 vLLM 服务，然后用参与者分片分摊任务：

```bash
# 节点 i，i 取 0..N-1
python3 scripts/run_hierarchical_extraction_participants.py \
  --manifest-dir data/cluster_inputs/epic37_proxy_manifests \
  --participants all \
  --expected-participants 37 \
  --num-shards N \
  --shard-index i \
  --data-root data/epic_kitchens_100_precise_v2 \
  --output-root outputs/epic_kitchens_100_precise_v2 \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b
```

分片按排序后的参与者轮询分配，不会重复处理。多分片会分别写入
`participant_pipeline_status_shard_XX_of_NN.csv`，避免共享文件系统上状态表相互覆盖。

编排器固定使用 `reencode / CRF 23` 生成精确片段。2026-07-14 之前由旧版
`copy` 模式生成的 micro 输出不能与新片段混用；恢复旧任务时应同时使用新的
`--data-root` 和 `--output-root`，完整隔离切片、状态表、推理 URL 和三层 JSON，
保留旧目录用于审计。编排器会验证本地 HTTP 服务实际指向当前 data root；若端口
仍由旧目录的服务占用会立即停止，此时应关闭旧服务或改用新的 `--http-port`。

清洗阶段首先使用标准 JSON 解析。仅当模型正常结束（`finish_reason=stop`）但存在可恢复的语法错误时，才使用 `json-repair` 生成 clean JSON，并写出同名 `*.repair.json` 审计文件，记录原始响应和修复结果的哈希。因长度上限截断的输出不会被自动修复。已有 `raw_only` 响应会先尝试清洗，成功后不再重复调用模型。

micro 校验通过并构建 window 输入后，默认删除该参与者在集群上的代理视频缓存和 30 秒片段，以限制磁盘峰值；原始代理视频仍保留在 COS。调试时可加 `--keep-local-video` 保留本地视频。编排器会自行启动运行期间所需的本地 HTTP 服务；如果已经手动启动，可加 `--external-http-server`。
精确切片模式不会上传临时 30 秒片段，因此启动新版全量抽取前无需删除 COS 中的旧片段。
不得删除 `proxy_540p16` 源代理视频；旧 `sessions_30s` 只能在审计和新旧对比结束后单独清理。

切片前会将下载后的代理视频时长与源数据元信息比较，默认最大误差为 1 秒；
还会在预期结尾前 5 秒实际解码一帧，防止 MP4 容器头声明了完整时长但媒体数据已被截断。
容器时长不符或媒体尾部缺失的本地缓存会被删除并重新下载一次；
新下载的文件仍未通过时会停止，不会将截断视频误当作完整 session。
下载默认尝试 3 次，并校验 HTTP `Content-Length`。

任一层的 clean JSON 如被硬校验拒绝，编排器会将原始输出移入
`validation_retries/retry_XX/`，将具体校验错误追加到重试提示词，并且只覆盖重抽
被拒绝的记录。其他已通过记录不会重算。

### 8.1 修复被截断的源代理视频

在 VPN 上按原参与者清单定点重新下载原视频、转码和覆盖同一 COS 对象：

```bash
cd /home/lighthouse/video-memory-benchmark
git pull --ff-only

for video_id in P02_06 P03_09 P04_05 P21_03 P22_03 P26_123 P28_17; do
  participant="${video_id%%_*}"
  participant_lower="$(printf '%s' "$participant" | tr '[:upper:]' '[:lower:]')"

  python3 scripts/run_epic_vpn_batch.py \
    --manifest "data/processed/epic_kitchens_100/manifests/queue/${participant_lower}_all_videos.csv" \
    --video-ids "$video_id" \
    --data-root /home/lighthouse/video-benchmark/data \
    --downloader-dir /home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100 \
    --python python3 \
    --cos-prefix video-benchmark/epic-kitchens \
    --url-expire-days 30 \
    --ffmpeg-threads 2 \
    --overwrite-proxy \
    --rerun-completed \
    --delete-raw-after-upload \
    --delete-proxy-after-upload \
    --fail-fast || exit 1
done
```

转码使用临时文件，只有 ffmpeg 成功后才原子替换正式代理文件；上传前校验代理
时长，上传后通过 COS HEAD 校验远端大小。覆盖使用原 COS key，因此未过期的
旧签名 URL 仍可访问新对象。

## 9. 结果检查

统计三层状态：

```bash
python3 - <<'PY'
import csv
from collections import Counter

for p in [
    'outputs/epic_kitchens_100/p30_micro_30s/batch_status.csv',
    'outputs/epic_kitchens_100/p30_windows_120s/batch_status.csv',
    'outputs/epic_kitchens_100/p30_sessions_full/batch_status.csv',
]:
    rows=list(csv.DictReader(open(p, newline='', encoding='utf-8')))
    print('\n', p)
    print('rows:', len(rows))
    print('status:', Counter(r['status'] for r in rows))
    print('finish_reason:', Counter(r.get('finish_reason', '') for r in rows))
    for r in rows:
        if r['status'] not in {'ok', 'skipped'}:
            print(r['record_id'], r['status'], r.get('finish_reason'), r.get('error'))
PY
```

检查 clean JSON 数量：

```bash
find outputs/epic_kitchens_100/p30_micro_30s -name '*.clean.json' | wc -l
find outputs/epic_kitchens_100/p30_windows_120s -name '*.clean.json' | wc -l
find outputs/epic_kitchens_100/p30_sessions_full -name '*.clean.json' | wc -l
```

## 10. 失败重跑

查看失败项：

```bash
python3 - <<'PY'
import csv
from collections import Counter
for p in [
    'outputs/epic_kitchens_100/p30_micro_30s/batch_status.csv',
    'outputs/epic_kitchens_100/p30_windows_120s/batch_status.csv',
    'outputs/epic_kitchens_100/p30_sessions_full/batch_status.csv',
]:
    rows=list(csv.DictReader(open(p, newline='', encoding='utf-8')))
    print('\n', p)
    print(Counter(r['status'] for r in rows))
    for r in rows:
        if r['status'] != 'ok':
            print(r['record_id'], r['status'], r.get('finish_reason'), r.get('error'))
PY
```

重跑指定记录：

```bash
python3 scripts/qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv data/cos_urls/p30_all_videos_sessions_30s_urls.csv \
  --prompt-file prompts/video_micro_evidence_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_micro_30s \
  --record-ids P30_05_s000,P30_05_s001 \
  --fps 1 \
  --max-tokens 4096 \
  --temperature 0 \
  --overwrite
```

如果 30 秒片段仍出现 `finish_reason=length` 且错误为 `No JSON object found in assistant content`，优先怀疑模型把输出预算花在长推理或格式漂移上。可以先打印原始响应确认：

```bash
python3 - <<'PY'
import json
from pathlib import Path

out = Path('outputs/epic_kitchens_100/p30_micro_30s')
for rid in ['P30_02_s001', 'P30_02_s007']:
    p = out / f'{rid}.json'
    r = json.loads(p.read_text(encoding='utf-8'))
    choice = r['choices'][0]
    msg = choice.get('message') or {}
    print('\n', rid)
    print('finish_reason:', choice.get('finish_reason'))
    print('usage:', r.get('usage'))
    for key in ['content', 'reasoning']:
        value = msg.get(key) or ''
        print(key, 'len=', len(value))
        print('head:', repr(value[:500]))
        print('tail:', repr(value[-500:]))
PY
```

然后只重跑失败记录。先提高输出上限：

```bash
python3 scripts/qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv data/cos_urls/p30_all_videos_sessions_30s_urls.csv \
  --prompt-file prompts/video_micro_evidence_schema_zh.txt \
  --output-dir outputs/epic_kitchens_100/p30_micro_30s \
  --record-ids P30_02_s001,P30_02_s007 \
  --fps 1 \
  --max-tokens 8192 \
  --temperature 0 \
  --overwrite
```

如果服务端支持 Qwen 的关闭 thinking 参数，可以在重跑时额外加：

```bash
--extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

window/session 层重跑时使用 `qwen_text_jsonl_batch.py --record-ids ... --overwrite`。

## 10. 打包结果带回本地

在集群上打包：

```bash
tar -czf /tmp/p30_hierarchical_outputs.tar.gz \
  outputs/epic_kitchens_100/p30_micro_30s \
  outputs/epic_kitchens_100/p30_windows_120s \
  outputs/epic_kitchens_100/p30_sessions_full \
  outputs/epic_kitchens_100/p30_hierarchical \
  data/cos_urls/p30_all_videos_sessions_30s_urls.csv \
  data/processed/epic_pipeline_runs/p30_all_videos_sessions_30s_status.csv
```

如果集群不能直接 `scp` 到本地，可以上传到 COS，再在本地下载并解压到：

```text
data/tmp/cluster_outputs/p30_hierarchical/extracted/
```

本地目录应包含：

```text
data/tmp/cluster_outputs/p30_hierarchical/extracted/outputs/epic_kitchens_100/p30_micro_30s/
data/tmp/cluster_outputs/p30_hierarchical/extracted/outputs/epic_kitchens_100/p30_windows_120s/
data/tmp/cluster_outputs/p30_hierarchical/extracted/outputs/epic_kitchens_100/p30_sessions_full/
data/tmp/cluster_outputs/p30_hierarchical/extracted/outputs/epic_kitchens_100/p30_hierarchical/
```

## 11. 生成 HTML 查看器

HTML 查看器用于单个视频的人工浏览：左侧播放代理视频，右侧展示完整 session、window 和 micro-clip JSON。

前提：

- 本地已经有集群打包解压后的分层输出；
- 本地有该参与者代理视频的 COS URL 表，例如：

```text
data/tmp/cluster_outputs/p30_all/data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv
```

生成 P30_03 示例：

```bash
PY=/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3

$PY scripts/build_hierarchical_example_viewer.py \
  --video-id P30_03 \
  --extracted-root data/tmp/cluster_outputs/p30_hierarchical/extracted \
  --proxy-url-csv data/tmp/cluster_outputs/p30_all/data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv \
  --output data/tmp/viewers/p30_03_hierarchical_viewer.html
```

本地可以直接打开 HTML；如果浏览器对 `file://` 下的远程视频播放有限制，用本地 HTTP 服务打开：

```bash
cd data/tmp/viewers
$PY -m http.server 8899
```

然后访问：

```text
http://127.0.0.1:8899/p30_03_hierarchical_viewer.html
```

当前示例也同步到了 `vpn` 的报告服务：

```text
远端路径：/home/lighthouse/video-memory-benchmark/reports/epic_kitchens_100/p30_03_hierarchical_viewer.html
线上地址：http://yufanwenshu.cn:8000/epic_kitchens_100/p30_03_hierarchical_viewer.html
```

该 HTML 内嵌 COS 签名视频 URL。不要提交到 GitHub；分享给他人前确认签名未过期、网络可访问，并遵守 EPIC-KITCHENS 数据许可。

## 12. 进入质检与参考证据放行

三层抽取输出不是 benchmark 最终标注，不能直接用于问题和标准答案。下一步必须执行确定性校验、百炼视觉复核、争议片段复核和人工抽检。

完整命令见：

```text
docs/hierarchical_evidence_qc_pipeline.md
```
