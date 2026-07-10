# 分层视频证据质检与放行流程

本文档接续 `docs/hierarchical_evidence_pipeline.md`，描述从三层模型输出到可用于 benchmark 的参考证据之间的完整质量控制流程。

质检遵循以下顺序：

```text
30 秒 micro 抽取
  -> micro 结构校验
  -> 120 秒 window 聚合
  -> window 结构校验
  -> 完整 session 聚合
  -> session 校验和候选规范化
  -> 百炼完整视频分组复核
  -> 争议候选局部片段复核
  -> 人工裁决
  -> reference_ready.jsonl
```

百炼复核只用于隐藏参考标注构建，不向被评测智能体提供，也不规定 baseline 如何处理视频记忆。

## 1. 环境和输入

集群安装：

```bash
cd /workspace/video-memory-benchmark
git pull --ff-only
python3 -m pip install -r requirements/cluster.txt
```

运行百炼任务前设置 API Key：

```bash
export DASHSCOPE_API_KEY='在百炼控制台生成的密钥'
```

密钥只放在环境变量中，不写入命令脚本、JSONL、任务记录或 Git。

P30 示例路径：

```bash
ROOT=outputs/epic_kitchens_100
QC=$ROOT/p30_qc
MICRO_CSV=data/cos_urls/p30_all_videos_sessions_30s_urls.csv
PROXY_CSV=data/cluster_inputs/p30_all_videos_proxy_540p16_urls.csv

mkdir -p "$QC"/{validation,hierarchical,bailian_source,bailian_local,review_clips,final}
```

`PROXY_CSV` 必须是一条原始视频对应一行的完整 `540p16` 代理视频清单，至少包含 `video_id` 或 `source_video_id` 以及 `signed_url`。不要把包含 594 条局部视频的 `MICRO_CSV` 当作完整代理清单。

百炼 Batch 任务可能排队，完整代理视频和临时片段的签名地址建议至少有效 14 天。

## 2. 在每个聚合层前执行结构校验

### 2.1 校验 micro 输出

```bash
python3 scripts/validate_hierarchical_evidence.py micro \
  --input-dir "$ROOT/p30_micro_30s" \
  --metadata "$MICRO_CSV" \
  --output-dir "$QC/validation/micro"
```

聚合 window 时使用校验通过目录：

```bash
python3 scripts/build_hierarchical_evidence_inputs.py windows \
  --micro-url-csv "$MICRO_CSV" \
  --micro-output-dir "$QC/validation/micro/accepted" \
  --window-sec 120 \
  --output-jsonl "$QC/hierarchical/window_inputs_30s_120s.jsonl"
```

运行 window 聚合：

```bash
python3 scripts/qwen_text_jsonl_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --input-jsonl "$QC/hierarchical/window_inputs_30s_120s.jsonl" \
  --prompt-file prompts/video_window_aggregation_schema_zh.txt \
  --output-dir "$QC/windows_120s" \
  --max-tokens 8192 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

### 2.2 校验 window 输出

```bash
python3 scripts/validate_hierarchical_evidence.py window \
  --input-dir "$QC/windows_120s" \
  --metadata "$QC/hierarchical/window_inputs_30s_120s.jsonl" \
  --output-dir "$QC/validation/window"
```

使用校验通过的 window 构建 session 输入：

```bash
python3 scripts/build_hierarchical_evidence_inputs.py sessions \
  --window-input-jsonl "$QC/hierarchical/window_inputs_30s_120s.jsonl" \
  --window-output-dir "$QC/validation/window/accepted" \
  --output-jsonl "$QC/hierarchical/session_inputs_30s_120s.jsonl"
```

运行完整 session 聚合：

```bash
python3 scripts/qwen_text_jsonl_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --input-jsonl "$QC/hierarchical/session_inputs_30s_120s.jsonl" \
  --prompt-file prompts/video_session_aggregation_schema_zh.txt \
  --output-dir "$QC/sessions_full" \
  --max-tokens 16384 \
  --temperature 0 \
  --extra-body-json '{"chat_template_kwargs":{"enable_thinking":false}}'
```

### 2.3 校验 session 输出

```bash
python3 scripts/validate_hierarchical_evidence.py session \
  --input-dir "$QC/sessions_full" \
  --metadata "$QC/hierarchical/session_inputs_30s_120s.jsonl" \
  --output-dir "$QC/validation/session"
```

输出：

```text
$QC/validation/session/accepted/*.clean.json
$QC/validation/session/issues.csv
$QC/validation/session/report.json
```

数量上限超出只产生警告，不会丢掉完整会话。单个候选的窗口或实体引用错误会将该候选标记为 `schema_failed`，但同会话其他候选仍可继续质检。

如果已经完成旧版 P30 三层抽取，可以直接对原有 session 输出执行本节命令，不必重新跑 594 个 micro-clip。确认质检方案后，全量参与者应按 2.1 到 2.3 的顺序运行。

## 3. 生成百炼完整视频质检任务

一条完整代理视频只生成一条请求，并在同一请求中复核该 session 的全部有效候选。

```bash
python3 scripts/build_bailian_qc_batch.py source \
  --session-records "$QC/validation/session/accepted" \
  --session-input-jsonl "$QC/hierarchical/session_inputs_30s_120s.jsonl" \
  --proxy-url-csv "$PROXY_CSV" \
  --prompt-file prompts/video_candidate_verification_schema_zh.txt \
  --output-jsonl "$QC/bailian_source/requests.jsonl" \
  --manifest-jsonl "$QC/bailian_source/request_manifest.jsonl" \
  --model qwen3.7-plus \
  --fps 0.5 \
  --max-tokens 8192
```

P30 正常应生成 25 条请求。候选数量以校验报告为准；P30 当前试跑为 206 条候选，其中 1 条 `schema_failed`，因此提交 205 条候选。

先检查请求文件，不要直接提交：

```bash
wc -l "$QC/bailian_source/requests.jsonl"
wc -l "$QC/bailian_source/request_manifest.jsonl"
```

## 4. 提交、查询并下载百炼 Batch

提交：

```bash
python3 scripts/bailian_batch_job.py submit \
  --input-jsonl "$QC/bailian_source/requests.jsonl" \
  --job-record "$QC/bailian_source/job.json" \
  --name p30-video-evidence-qc \
  --description 'P30 session candidate visual verification'
```

查询：

```bash
python3 scripts/bailian_batch_job.py status \
  --job-record "$QC/bailian_source/job.json"
```

任务完成后下载：

```bash
python3 scripts/bailian_batch_job.py download \
  --job-record "$QC/bailian_source/job.json" \
  --output-jsonl "$QC/bailian_source/results.jsonl" \
  --error-jsonl "$QC/bailian_source/errors.jsonl"
```

任务记录保存输入哈希、远端文件 ID、Batch ID、状态和结果文件 ID，不保存 API Key。

## 5. 合并第一轮质检结果

```bash
python3 scripts/merge_bailian_qc_results.py source \
  --session-records "$QC/validation/session/accepted" \
  --manifest-jsonl "$QC/bailian_source/request_manifest.jsonl" \
  --batch-output-jsonl "$QC/bailian_source/results.jsonl" \
  --output-dir "$QC/source_qc"
```

主要输出：

```text
$QC/source_qc/merged/*.qc.json
$QC/source_qc/local_review_queue.jsonl
$QC/source_qc/retry_queue.jsonl
$QC/source_qc/human_review.csv
$QC/source_qc/quality_report.json
```

状态含义：

- `verification_passed`：完整视频复核支持，且没有阻断质量标记；
- `verification_disputed`：完整视频复核反驳，必须看局部片段后才能最终拒绝；
- `verification_uncertain`：证据不足或证据时间与支持区间不相交；
- `human_review_required`：长期措辞、不确定性、模型修正等情况需要人工判断；
- `schema_failed`：候选自身引用关系损坏，没有发送给百炼。

## 6. 为争议候选生成临时局部片段

这一步在能访问 `~/.cos.conf` 的机器上执行。脚本下载需要的完整代理视频，按 30 秒网格去重，只切出 `local_review_queue.jsonl` 涉及的区间。

```bash
python3 -m pip install -r requirements/vpn.txt

python3 scripts/prepare_qc_review_clips.py \
  --review-queue "$QC/source_qc/local_review_queue.jsonl" \
  --proxy-url-csv "$PROXY_CSV" \
  --source-cache-root data/proxy_from_cos_qc \
  --output-root data/qc_review_clips \
  --output-url-csv "$QC/review_clips/clip_urls.csv" \
  --mapping-jsonl "$QC/review_clips/candidate_clip_mapping.jsonl" \
  --cleanup-csv "$QC/review_clips/cos_cleanup.csv" \
  --clip-sec 30 \
  --upload \
  --cos-config ~/.cos.conf \
  --cos-prefix video-benchmark/qc-temp/p30 \
  --url-expire-days 14 \
  --delete-local-after-upload \
  --delete-source-after
```

同一个 30 秒区间被多个候选引用时只上传一次。`cos_cleanup.csv` 是远端临时对象清理清单；建议为 `video-benchmark/qc-temp/` 配置对象存储生命周期，任务完成后按清单核对删除。

## 7. 提交局部视频二次复核

生成请求：

```bash
python3 scripts/build_bailian_qc_batch.py local \
  --review-mapping-jsonl "$QC/review_clips/candidate_clip_mapping.jsonl" \
  --clip-url-csv "$QC/review_clips/clip_urls.csv" \
  --prompt-file prompts/video_candidate_local_verification_schema_zh.txt \
  --output-jsonl "$QC/bailian_local/requests.jsonl" \
  --manifest-jsonl "$QC/bailian_local/request_manifest.jsonl" \
  --model qwen3.7-plus \
  --fps 1 \
  --max-tokens 4096
```

提交、查询和下载命令与第 4 节相同，只需换成 `bailian_local` 路径和独立任务名。

合并局部结果：

```bash
python3 scripts/merge_bailian_qc_results.py local \
  --first-pass-dir "$QC/source_qc/merged" \
  --manifest-jsonl "$QC/bailian_local/request_manifest.jsonl" \
  --batch-output-jsonl "$QC/bailian_local/results.jsonl" \
  --output-dir "$QC/local_qc"
```

局部复核支持、反驳和仍不确定时分别变为：

- `local_verification_passed`；
- `local_verification_rejected`；
- `human_review_required`。

## 8. 人工审核和最终放行

填写 `$QC/local_qc/human_review.csv` 中的：

- `human_decision`：`accept_original`、`accept_corrected` 或 `reject`；
- `approved_claim`：只有 `accept_corrected` 时必填；
- `human_notes`：记录人工证据判断。

执行：

```bash
python3 scripts/finalize_reference_evidence.py \
  --qc-records "$QC/local_qc/merged" \
  --human-review-csv "$QC/local_qc/human_review.csv" \
  --output-dir "$QC/final"
```

最终输出：

```text
$QC/final/reference_ready.jsonl
$QC/final/rejected.jsonl
$QC/final/unresolved.jsonl
$QC/final/finalization_report.json
```

只有 `reference_ready.jsonl` 可以进入后续跨 session 证据图、问题和标准答案构建。`unresolved.jsonl` 不能当作负样本或错误事实直接使用。

## 9. 失败恢复和版本管理

- 原始抽取结果永不覆盖；校验、百炼结果和最终结果使用独立目录。
- 成功的模型抽取默认跳过，失败记录按 ID 重跑。
- Batch 输入文件和任务记录都有哈希；已有进行中任务默认禁止重复提交。
- 下载已有结果默认报错，只有明确传入 `--overwrite` 才覆盖。
- `retry_queue.jsonl` 非空时先重试缺失请求，再进行最终放行。
- 每次全量处理固定模型 ID、提示词提交版本、fps、token 上限和 `pipeline_version`。

## 10. P30 验收指标

在扩展到 37 个参与者前至少检查：

- 25 条 session 是否全部完成结构校验；
- 候选总数、`schema_failed` 数量和原因；
- 百炼三种 verdict 的比例；
- 局部复核触发率；
- 自动放行率、人工审核率和最终拒绝率；
- 已知 P30_113 刀具位置错误是否被拦截；
- 按候选类型分层的人工抽检精确率。

当前本地干跑结果：594/594 个 micro、156/156 个 window、25/25 个 session 均可继续；206 条候选中 205 条进入百炼请求，1 条因实体引用断裂单独阻断。即使模拟百炼把 205 条候选全部判为支持，仍只有 98 条自动放行、107 条进入人工审核、1 条保持结构失败；P30_113 的刀具位置候选带有 `affected_by_uncertainty`，不会在第一轮自动放行。
