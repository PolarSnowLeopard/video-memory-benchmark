# Qwen A3B P30 原始视频级结果分析

生成时间：2026-07-02

## 数据范围

- 参与者：P30
- 输入粒度：EPIC 原始视频级别，未做 3 分钟或 5 分钟标准 session 切分
- 输入数量：25 个视频
- 模型：Qwen3.5 35B A3B
- 采样：`fps=1`
- 输出上限：`max_tokens=4096`

## 结论

这批结果不能视为 24 条成功、1 条失败。按当前清洗脚本和 JSON 可用性统计：

| 状态 | 数量 | 说明 |
|---|---:|---|
| `ok` | 1 | 仅 `P30_09` 生成了可解析的完整 JSON |
| `raw_only` | 23 | 模型返回了原始响应，但 `finish_reason=length`，JSON 被截断 |
| `error` | 1 | `P30_05` 服务端 500 |

主要问题不是视频下载或对象存储，而是推理粒度和输出 schema 太重：

1. 原始视频时长差异很大，最长超过 54 分钟。
2. `fps=1` 对长视频会产生数千帧级输入。
3. 当前提示词要求 12 个顶层字段，单个视频输出容易超过 4096 token。
4. 若模型先输出较长 reasoning，再输出 JSON，最终 JSON 更容易被截断。

## 失败模式

### 输出截断

23 个样本的 `finish_reason` 都是 `length`，`completion_tokens` 都撞到 4096。常见表现：

- JSON 写到 `scene_graph`、`timeline_events`、`state_changes` 等字段中途停止。
- `json.loads` 报错，通常是缺少逗号、括号或字符串结尾。
- 部分样本 `message.content` 为空，但 `message.reasoning` 很长，说明模型把输出预算花在 reasoning 上。

### 服务端 500

`P30_05`：

- 时长：54.37 分钟
- 代理视频大小：341 MB
- `fps=1` 约 3262 帧
- 原始视频大小：约 12.3 GB

该样本极可能是视频过长导致服务端解码或多模态预处理失败，不应作为单次 VLM 输入。

## 可用信息

虽然多数 JSON 不可解析，原始响应里仍能看到一些粗粒度摘要。例如：

| 视频 | 状态 | 主要内容摘要 |
|---|---|---|
| P30_01 | raw_only | 打开冰箱冷冻抽屉，取出包装袋和肉类，放到操作台 |
| P30_02 | raw_only | 加热鹰嘴豆泥、烤面包、取黄瓜和胡萝卜并装盘 |
| P30_03 | raw_only | 清洗餐具和台面、准备早餐、处理洗衣 |
| P30_08 | raw_only | 大量洗碗、取物、煮意面、热酱和香肠、整理台面 |
| P30_09 | ok | 清洗餐具、处理食材、使用手机及收纳物品 |
| P30_105 | raw_only | 将衣物放入洗衣机，添加洗衣液并洗手 |
| P30_112 | raw_only | 从烤箱取出披萨，切割分装，清洗砧板和刀具 |

这些 raw 输出适合用于人工快速了解 P30 活动范围，但不适合直接进入结构化证据链。

## 唯一完整样本：P30_09

`P30_09.clean.json` 包含完整 schema：

- 3 个地点
- 1 个 actor
- 3 个 objects
- 3 个 task segments
- 3 个 timeline events
- 2 个 state observations
- 2 个 state changes
- 1 个 cross-session memory candidate

示例候选记忆：

> 本视频中，电水壶最终位于水槽旁；后续可验证该物体是否保持类似位置。

这个样本说明 schema 方向是可行的，但对完整原始视频而言过重。它更适合用在短 session 上。

## 对正式流水线的影响

这批结果支持将正式处理粒度改为标准 session：

- 推荐默认：5 分钟非重叠切分
- 备选更细粒度：3 分钟非重叠切分
- 最后一段小于 60 秒时合并到前一段；大于等于 60 秒时保留为短 session

5 分钟切分后：

- 单次输入约 300 帧，远低于 `P30_05` 的 3262 帧
- 全量 100 小时约 1200 个 session
- session 粒度更适合作为 agent 跨会话长期记忆 benchmark 的基本单位

## 建议的短期重跑策略

在切分流水线完成前，如果还要复用原始视频级 P30 输出，建议只做诊断性重跑：

1. 对 `raw_only` 样本先把 `max_tokens` 提到 8192。
2. 对长视频降低到 `fps=0.25` 或 `fps=0.1`。
3. 对 `P30_05`、`P30_107`、`P30_111` 这类超长视频，不建议整段重跑，直接切 session。

更推荐把工程精力放到标准 session 切分，而不是继续补救原始视频级输出。

## 提示词和脚本修改建议

1. 在 `qwen_video_batch.py` 中以 `session_id` 而不是 `video_id` 作为主键，避免同一个原始视频切多个 session 后输出覆盖。
2. 增加 session manifest，字段至少包括：
   - `session_id`
   - `participant_id`
   - `source_video_id`
   - `session_index`
   - `start_sec`
   - `end_sec`
   - `duration_sec`
   - `signed_url`
3. 给 VLM 抽取提示词做轻量版，减少顶层字段和列表长度。
4. 禁止或降低 reasoning 输出，优先保证 `message.content` 中直接返回完整 JSON。
5. 对每个 session 限制实体和事件数量，例如：
   - 最多 5 个 places
   - 最多 12 个 objects
   - 最多 8 个 events
   - 最多 6 个 state changes
   - 最多 5 个 cross-session memory candidates

## 建议结论

P30 原始视频级试跑的价值主要是诊断，不是可直接使用的标注结果。它证明了：

1. 下载、转码、上传、集群推理链路已经跑通。
2. 原始 EPIC 视频粒度太粗，正式 benchmark 必须定义标准 session。
3. 当前 schema 需要轻量化，否则即使短视频也容易撞输出上限。
4. Qwen 35B A3B 可以作为第一阶段证据抽取器，但需要配合切分、输出限制和质量审计。
