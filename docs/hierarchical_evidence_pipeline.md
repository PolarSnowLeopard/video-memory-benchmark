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
  --local-url-base http://127.0.0.1:18080
```

输出：

```text
data/cos_urls/p30_all_videos_sessions_30s_urls.csv
data/sessions/P30/sessions_30s/*.mp4
data/processed/epic_pipeline_runs/p30_all_videos_sessions_30s_status.csv
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
  --temperature 0
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
  --temperature 0
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
  --temperature 0
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

## 8. 失败重跑

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
