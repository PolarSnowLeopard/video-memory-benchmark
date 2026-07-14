# Ego4D 接入流程

本文档定义 Ego4D 接入本仓库的第一阶段：先审计 `ego4d.json`，再按 `video_uid` 下载小规模 `video_540ss` 试验集。当前阶段不下载全量视频，也不把缺失的跨视频时间顺序补成推测值。

本 benchmark 的目标是通用第一人称长期视频记忆，不限定为烹饪。当前的
`Cooking + audio` 15 视频集合只是工程回归集：它用于和 EPIC-KITCHENS 对照，验证下载、转码、
切片、分层抽取和质检链路，不是最终数据范围的缩小版。根据本地 Ego4D 2.1 元信息，去掉过短、
过长、高脱敏影响且不具备同参与者多视频条件的记录后，通用候选池为 6,389 个视频、517 个参与者。
后续应从这个池中做场景覆盖和同参与者多会话选择，而不是扩展为全部烹饪视频。

官方参考：

- [Ego4D 元信息结构](https://ego4d-data.org/docs/data/metadata/)
- [Ego4D 下载命令行工具](https://ego4d-data.org/docs/CLI/)
- [Ego4D 视频形式和编码说明](https://ego4d-data.org/docs/data/videos/)
- [Ego4D 官方代码仓库](https://github.com/facebookresearch/Ego4d)

## 当前结论

Ego4D 可以复用现有的 `30 秒局部证据 -> 120 秒窗口 -> 完整视频` 分层抽取思想，但不能直接照搬 EPIC-KITCHENS 的跨会话定义。

关键映射如下：

| benchmark 概念 | Ego4D 字段或单位 | 当前可信程度 |
| --- | --- | --- |
| 同一佩戴者 | `fb_participant_id` | 可用于同一参与者分组 |
| 一段连续会话 | `video_uid` 对应的规范视频 | 视频内部时间轴可信 |
| 视频内部原始片段 | `video_components` | 只用于解释规范视频如何拼成 |
| 场景类别 | `scenarios` | 可用于筛选，不等于具体地点 |
| 具体地点 | `physical_setting_name` | 仅部分视频存在 |
| 音频 | `video_metadata.audio_duration_sec` | 可统计覆盖率 |
| 脱敏影响 | `redacted_intervals` | 可统计区间并集 |
| 同时拍摄关系 | `concurrent_video_sets` | 表示并发，不表示先后 |
| 多条规范视频的现实时间顺序 | 无标准字段 | 未知，禁止推断 |

`origin_video_id` 是采集单位自行分配的编号，跨采集单位没有统一格式。`video_uid`、对象存储路径、文件名字典序和不同视频中的 `component_idx` 都不能作为跨视频时间顺序。

因此，未经额外时间证据时：

- 同一规范视频内部可以做事件、状态变化和长视频记忆标注；
- 同一参与者的多条视频可以做无序一致性、身份关联和检索类候选构造；
- 不能做“第几天”“后来”“从会话 A 演进到会话 B”一类有方向的跨会话问题；
- 把一条长视频固定切成多段，只是模拟会话边界，不能声称是现实中的多次打开和关闭智能体。

## 1. 取得访问权限

Ego4D 官方代码仓库的开源许可不等于数据访问许可。下载元信息、标注和视频前，需要完成 Ego4D 数据许可流程，并按官方说明把 AWS 凭据配置到本机或服务器。

建议在有外网和数据盘的服务器上使用独立虚拟环境，不依赖本地公司的 Conda：

```bash
python3 -m venv ~/.venvs/ego4d
source ~/.venvs/ego4d/bin/activate
python -m pip install --upgrade pip ego4d cos-python-sdk-v5
```

使用独立 AWS 配置名：

```bash
aws configure --profile ego4d
```

不要把 AWS 凭据、官方视频、签名地址或带许可限制的标注提交到 GitHub。

## 2. 只获取元信息和标注

官方命令行工具要求指定一个数据项。先选择体积远小于视频的 `annotations`，同时获取顶层 `ego4d.json`：

```bash
export EGO4D_ROOT=/home/lighthouse/video-benchmark/data/ego4d

ego4d \
  --output_directory "$EGO4D_ROOT" \
  --datasets annotations \
  --metadata \
  --version v2_1 \
  --aws_profile_name ego4d \
  --yes
```

不同版本的官方命令行工具可能把元信息放在根目录或版本目录。不要硬编码位置，用下面的命令确认：

```text
/home/lighthouse/video-benchmark/data/ego4d/ego4d.json
/home/lighthouse/video-benchmark/data/ego4d/v2/ego4d.json
```

```bash
find "$EGO4D_ROOT" -name ego4d.json -type f -print
```

这一步不会下载 `full_scale` 或 `video_540ss` 视频。

## 3. 运行元信息审计

脚本只使用 Python 标准库：

```bash
cd /home/lighthouse/video-memory-benchmark
git pull

export EGO4D_ROOT=/home/lighthouse/video-benchmark/data/ego4d
export EGO4D_METADATA="$(find "$EGO4D_ROOT" -name ego4d.json -type f -print -quit)"
test -n "$EGO4D_METADATA"

python3 scripts/analyze_ego4d_metadata.py \
  --metadata-json "$EGO4D_METADATA" \
  --output-dir data/processed/ego4d
```

默认试验候选条件：

- 单条规范视频时长在 5 分钟到 2 小时之间；
- 脱敏比例不超过 20%；
- 参与者编号已知；
- 同一参与者至少有 3 条通过单视频条件、且去除并发视角重复后仍独立的视频；
- 不强制要求音频；
- 不限制场景；
- 最终自动选择最多 5 个参与者、每人 3 条视频。
- 同一参与者在同一个有效并发拍摄集合中的多个视频最多选择一条。

这些阈值只用于低成本试验选样，不是最终 benchmark 采样标准。可以显式调整：

```bash
python3 scripts/analyze_ego4d_metadata.py \
  --metadata-json "$EGO4D_METADATA" \
  --output-dir data/processed/ego4d_cooking_audio \
  --scenario Cooking \
  --require-audio \
  --min-duration-sec 300 \
  --max-duration-sec 3600 \
  --max-redaction-ratio 0.10 \
  --min-videos-per-participant 3 \
  --pilot-participants 5 \
  --pilot-videos-per-participant 3
```

`--scenario` 是大小写不敏感的精确匹配，可以重复传入多个值。应先看 `scenario_summary.csv` 中的官方场景名称，再决定筛选条件。

## 4. 审计产物

脚本输出到指定目录：

| 文件 | 用途 |
| --- | --- |
| `metadata_report.json` | 总视频时长、时长分位数、参与者、音频、脱敏、并发集合和候选统计 |
| `video_summary.csv` | 每条规范视频的统一元信息 |
| `participant_summary.csv` | 按已知参与者或独立未知视频分组的统计 |
| `scenario_summary.csv` | 场景对应的视频数、参与者数、时长和音频覆盖 |
| `temporal_order_audit.csv` | 跨视频顺序与任务资格的硬门禁 |
| `candidate_videos.csv` | 全部视频及透明排除原因 |
| `pilot_manifest.csv` | 自动选择的小规模下载试验清单 |
| `pilot_video_uids.txt` | 可直接传给官方命令行工具的 UID 列表 |

快速检查：

```bash
python3 -m json.tool data/processed/ego4d/metadata_report.json | less
head -n 20 data/processed/ego4d/scenario_summary.csv
head -n 20 data/processed/ego4d/temporal_order_audit.csv
wc -l data/processed/ego4d/pilot_video_uids.txt
```

`pilot_manifest.csv` 中的 `pilot_selection_rank` 只用于可复现选样，`pilot_selection_rank_is_temporal=false`，且 `cross_video_session_order` 始终为空。

## 5. 只下载试验视频

取得许可后，按 UID 下载短边 540 像素的规范视频，不下载约 5TB 的全分辨率集合：

```bash
export EGO4D_ROOT=/home/lighthouse/video-benchmark/data/ego4d

ego4d \
  --output_directory "$EGO4D_ROOT" \
  --datasets video_540ss \
  --video_uid_file /home/lighthouse/video-memory-benchmark/data/processed/ego4d_cooking_audio/pilot_video_uids.txt \
  --version v2_1 \
  --aws_profile_name ego4d \
  --no-metadata \
  --yes
```

官方的规范视频为固定 30 帧，视频编码通常为 VP9，音频为 AAC；`video_540ss` 只额外把短边缩放到 540 像素。为了与当前集群链路保持一致，后续代理层仍建议统一转成：

```text
H.264 / 短边 540 / 16fps / AAC 64kbps / 保留画面方向
```

不要再从 `full_scale` 下载同一批视频后重复缩放。

`vpn` 上的 `/home/lighthouse/video-benchmark/data` 应软连接到数据盘。运行前必须确认：

```bash
readlink -f /home/lighthouse/video-benchmark/data
df -hT /home/lighthouse/video-benchmark/data
```

完整 `full_scale` 和完整 `video_540ss` 都不作为本项目的本地常驻数据。正式处理采用与 EPIC-KITCHENS-100 相同的有界流水线：

1. 只按当前批次的 `video_uid` 下载；
2. 转码并上传代理视频；
3. 校验对象存储结果和 URL 清单；
4. 删除本批下载的视频和本地代理；
5. 再启动下一批。

元信息和标注可以常驻数据盘，视频文件必须按批次及时清理。

## 6. 转码、校验并上传代理视频

`run_ego4d_vpn_batch.py` 直接消费元信息审计生成的清单。每条记录依次执行：

1. 缺少源视频时，只按当前 `video_uid` 调用官方下载工具；
2. 用 `ffprobe` 确认源视频可解码；
3. 转为 H.264、短边 540、16 帧、AAC 64kbps；
4. 再次校验编码、分辨率、帧率、时长和音频；
5. 上传对象存储，并用对象大小核对上传结果；
6. 先持久化状态表和签名地址清单，再按参数删除本地源视频与代理视频。

试点批次的完整命令：

```bash
cd /home/lighthouse/video-memory-benchmark
source /home/lighthouse/.venvs/ego4d/bin/activate

python3 scripts/run_ego4d_vpn_batch.py \
  --manifest data/processed/ego4d_cooking_audio/pilot_manifest.csv \
  --ego4d-root /home/lighthouse/video-benchmark/data/ego4d \
  --ego4d-video-dir /home/lighthouse/video-benchmark/data/ego4d/v2/video_540ss \
  --data-root /home/lighthouse/video-benchmark/data \
  --aws-profile ego4d \
  --cos-config ~/.cos.conf \
  --cos-prefix video-benchmark/ego4d \
  --run-name ego4d_cooking_audio_pilot \
  --ffmpeg-threads 4 \
  --delete-raw-after-upload \
  --delete-proxy-after-upload \
  --fail-fast
```

输出位于数据盘：

```text
/home/lighthouse/video-benchmark/data/processed/ego4d_pipeline_runs/ego4d_cooking_audio_pilot_status.csv
/home/lighthouse/video-benchmark/data/cos_urls/ego4d_cooking_audio_pilot_proxy_540p16_urls.csv
```

脚本可以安全续跑：只有状态为 `ok` 且 URL 清单中已有签名地址的记录才会跳过。任一步骤失败时不会清理该条记录的本地视频。正式全量处理仍应使用元信息脚本生成的有界清单分批运行，不能把全部 `video_540ss` 一次下载到本地。

## 7. 接入现有分层抽取

下载试验视频后的适配边界是：

```text
Ego4D video_540ss
  -> 数据集无关的代理转码与 COS URL 清单
  -> 30 秒 micro-clip
  -> 120 秒 window
  -> 完整 canonical video
  -> 结构校验和独立质检
```

现有的切片、视频调用、文本聚合、结构校验和百炼质检框架可以继续使用。
标准代理清单保留 `video_uid` 作为 `video_id`，并保留规范化参与者编号。生产提示词使用通用地点、
人物、物体、文档、设备、车辆、工具、任务和状态枚举；烹饪动作只是其中一类。正式扩展时仍需按场景分层
抽样，检查动作、实体和状态枚举的覆盖率。

集群侧切成 30 秒片段时必须精确重编码，不能直接码流拷贝：

```bash
python3 scripts/prepare_video_sessions_for_inference.py \
  --video-url-csv data/cluster_inputs/ego4d_cooking_audio_pilot_proxy_540p16_urls.csv \
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

切片脚本会用实际容器时长校验计划时长，默认允许 0.25 秒封装误差。没有通过
时长校验的记录不会被视为可续跑的完成项。micro 结构校验器还会拒绝任何超过
该片段 `duration_sec` 的相对时间，防止相邻片段证据进入后续聚合。

全量抽取前，不要只取 URL 表前若干行。用下面的脚本从每个来源视频确定性地选择
中部一个片段，先做跨来源烟测：

```bash
SMOKE_IDS=$(python3 scripts/select_session_smoke_ids.py \
  --session-csv data/cos_urls/ego4d_cooking_audio_pilot_sessions_30s_urls.csv)

python3 scripts/qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv data/cos_urls/ego4d_cooking_audio_pilot_sessions_30s_urls.csv \
  --prompt-file prompts/video_micro_evidence_schema_zh.txt \
  --output-dir outputs/ego4d/cooking_audio_pilot_micro_30s_stratified_smoke \
  --record-ids "$SMOKE_IDS" \
  --fps 1 \
  --max-tokens 4096 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

Ego4D 的叙述、情景记忆、手物交互等官方标注可作为独立质检信号，但不应直接复制成最终 benchmark 答案。VLM 提取仍是候选证据，最终参考证据要经过结构校验、独立视觉复核和人工抽检。

## 8. 试点全量分层抽取

跨来源分层烟测通过后，使用新输出根目录运行试点全量。不要把旧提示词生成的
12 条复测结果或 15 条烟测结果复制到新目录。当前试点的预期规模为 345 个 30 秒片段、
91 个 120 秒窗口和 15 个完整来源视频。

首先确保本地视频服务仍在为 `data/sessions` 目录提供服务，然后运行 30 秒层：

```bash
cd /workspace/video-memory-benchmark
git pull
set -euo pipefail

BASE_URL=http://127.0.0.1:8000/v1
MODEL=qwen35-a3b
SESSION_CSV=data/cos_urls/ego4d_cooking_audio_pilot_sessions_30s_urls.csv
RUN_ROOT=outputs/ego4d/cooking_audio_pilot_v2
MICRO_DIR=$RUN_ROOT/micro_30s
WINDOW_DIR=$RUN_ROOT/windows_120s
SESSION_DIR=$RUN_ROOT/sessions_full
QC_ROOT=$RUN_ROOT/qc
WINDOW_INPUT=$QC_ROOT/hierarchical/window_inputs_30s_120s.jsonl
SESSION_INPUT=$QC_ROOT/hierarchical/session_inputs_30s_120s.jsonl

mkdir -p "$MICRO_DIR" "$WINDOW_DIR" "$SESSION_DIR" "$QC_ROOT/hierarchical"

python3 scripts/qwen_video_batch.py \
  --base-url "$BASE_URL" \
  --model "$MODEL" \
  --signed-url-csv "$SESSION_CSV" \
  --prompt-file prompts/video_micro_evidence_schema_zh.txt \
  --output-dir "$MICRO_DIR" \
  --fps 1 \
  --max-tokens 8192 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'

python3 scripts/validate_hierarchical_evidence.py micro \
  --input-dir "$MICRO_DIR" \
  --metadata "$SESSION_CSV" \
  --output-dir "$QC_ROOT/validation/micro"

python3 - "$QC_ROOT/validation/micro/report.json" 345 <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
expected = int(sys.argv[2])
actual = {key: report.get(key) for key in ("records", "accepted", "rejected")}
print(actual)
assert actual == {"records": expected, "accepted": expected, "rejected": 0}, actual
PY
```

同一命令可以安全续跑：已有的 `clean.json` 会被跳过，只会重试未生成可解析结果的记录。
微片段校验副本会规范时间顺序、已知英文状态值和便携锅具类别，原始输出保持不变。

微片段全部接受后，运行 120 秒窗口层：

```bash
python3 scripts/build_hierarchical_evidence_inputs.py windows \
  --micro-url-csv "$SESSION_CSV" \
  --micro-output-dir "$QC_ROOT/validation/micro/accepted" \
  --window-sec 120 \
  --output-jsonl "$WINDOW_INPUT"

python3 scripts/qwen_text_jsonl_batch.py \
  --base-url "$BASE_URL" \
  --model "$MODEL" \
  --input-jsonl "$WINDOW_INPUT" \
  --prompt-file prompts/video_window_aggregation_schema_zh.txt \
  --output-dir "$WINDOW_DIR" \
  --max-tokens 8192 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'

python3 scripts/validate_hierarchical_evidence.py window \
  --input-dir "$WINDOW_DIR" \
  --metadata "$WINDOW_INPUT" \
  --output-dir "$QC_ROOT/validation/window"

python3 - "$QC_ROOT/validation/window/report.json" 91 <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
expected = int(sys.argv[2])
actual = {key: report.get(key) for key in ("records", "accepted", "rejected")}
print(actual)
assert actual == {"records": expected, "accepted": expected, "rejected": 0}, actual
PY
```

窗口全部接受后，运行完整来源视频层：

```bash
python3 scripts/build_hierarchical_evidence_inputs.py sessions \
  --window-input-jsonl "$WINDOW_INPUT" \
  --window-output-dir "$QC_ROOT/validation/window/accepted" \
  --output-jsonl "$SESSION_INPUT"

python3 scripts/qwen_text_jsonl_batch.py \
  --base-url "$BASE_URL" \
  --model "$MODEL" \
  --input-jsonl "$SESSION_INPUT" \
  --prompt-file prompts/video_session_aggregation_schema_zh.txt \
  --output-dir "$SESSION_DIR" \
  --max-tokens 16384 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'

python3 scripts/validate_hierarchical_evidence.py session \
  --input-dir "$SESSION_DIR" \
  --metadata "$SESSION_INPUT" \
  --output-dir "$QC_ROOT/validation/session"

python3 - "$QC_ROOT/validation/session/report.json" 15 <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
expected = int(sys.argv[2])
actual = {key: report.get(key) for key in ("records", "accepted", "rejected")}
print(actual)
assert actual == {"records": expected, "accepted": expected, "rejected": 0}, actual
PY
```

两次层间构建都会移除上游模型自报的 `confidence` 和 `trackability`，避免未校准的“全部高置信”
向上层传播。画面证据、不确定性和结构校验摘要仍会保留。

## 9. 跨会话演进的放行条件

如果后续获得可靠的额外时间信息，必须单独保存带来源的顺序表，至少包含：

```text
participant_id,video_uid,session_order,ordering_basis,ordering_source,verified_by
```

只有同一参与者的每条入选视频都具有可审计的现实时间依据时，才能把该组的 `temporal_evolution_eligible` 改为 `true`。人工观看后根据剧情“猜顺序”、按文件名排序或按 `origin_video_id` 排序都不满足这一条件。
