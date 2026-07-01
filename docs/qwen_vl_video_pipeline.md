# Qwen 视频输入试跑流程

## 当前可用视频

腾讯云 COS 中已有 P04 的三类文件：

- 原始视频：`video-benchmark/epic-kitchens/P04/raw/`
- 540p 代理视频：`video-benchmark/epic-kitchens/P04/proxy_540p/`
- 联系表：`video-benchmark/epic-kitchens/P04/contact_sheets/`

本地签名链接清单在：

```text
data/tmp/cos_urls/p04_proxy_cos_urls.csv
data/tmp/cos_urls/p04_raw_cos_urls.csv
data/tmp/cos_urls/p04_contact_cos_urls.csv
data/tmp/cos_urls/p04_viewer.html
```

`data/tmp` 已被忽略，不进入 git。

## 模型名说明

官方仓库里，标准模型名是：

```text
Qwen/Qwen3.5-35B-A3B
```

这不是 `Qwen3.5-35B-VL` 这个名字，但它是带视觉编码器的多模态模型。

如果目标是最稳地跑通 `video_url` 输入链路，建议优先用：

```text
Qwen/Qwen3-VL-32B-Instruct
```

或者资源更小的：

```text
Qwen/Qwen3-VL-8B-Instruct
```

原因是 Qwen3-VL 官方文档明确给了 `video_url` 输入和 vLLM 服务示例。

对你现在的情况，建议拆成两步：

1. 用 `Qwen/Qwen3-VL-8B-Instruct` 或 `Qwen/Qwen3-VL-32B-Instruct` 先验证 COS URL 到 vLLM 的视频输入链路。
2. 再换成 `Qwen/Qwen3.5-35B-A3B`。如果它的服务端 `video_url` 兼容性和 Qwen3-VL 不一致，就退回到“抽帧为多图输入”的方式，这对后续构建证据链反而更可控。

## Hugging Face 镜像

集群上可以直接设置：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

如果你们已有内部模型缓存或镜像，把 `MODEL` 换成本地路径即可。

## 集群试跑包

本仓库提供一个小脚本生成最小试跑目录：

```bash
python scripts/prepare_qwen_cluster_bundle.py
```

生成目录：

```text
data/tmp/qwen_cluster_bundle/
```

把这个目录传到集群后：

```bash
chmod +x run_probe.sh
BASE_URL=http://127.0.0.1:8000/v1 \
MODEL=Qwen/Qwen3-VL-32B-Instruct \
VIDEO_ID=P04_106 \
FPS=1 \
./run_probe.sh
```

这会读取 `p04_proxy_cos_urls.csv` 里的 COS 签名 URL，将 540p 代理视频发给 OpenAI 兼容接口，并把结果写到：

```text
outputs/P04_106.json
```

## vLLM 启动示例

Qwen3-VL 官方建议 vLLM 版本不低于 0.11.0。

```bash
vllm serve Qwen/Qwen3-VL-32B-Instruct \
  --tensor-parallel-size 4 \
  --media-io-kwargs '{"video": {"num_frames": -1}}' \
  --host 0.0.0.0 \
  --port 8000
```

如果你用单节点 8 卡 H20，第一轮建议：

```bash
export HF_ENDPOINT=https://hf-mirror.com

vllm serve Qwen/Qwen3-VL-32B-Instruct \
  --tensor-parallel-size 8 \
  --media-io-kwargs '{"video": {"num_frames": -1}}' \
  --host 0.0.0.0 \
  --port 8000
```

如果只是链路验证，先跑 8B 可以更快定位“URL、视频解码、请求格式、输出解析”的问题。

如果显存不足，先用 8B 跑通流程：

```bash
vllm serve Qwen/Qwen3-VL-8B-Instruct \
  --tensor-parallel-size 1 \
  --media-io-kwargs '{"video": {"num_frames": -1}}' \
  --host 0.0.0.0 \
  --port 8000
```

## SGLang 启动示例

如果要试 `Qwen/Qwen3.5-35B-A3B`，官方 Hugging Face 页给的服务方式偏向 SGLang：

```bash
python -m sglang.launch_server \
  --model-path Qwen/Qwen3.5-35B-A3B \
  --host 0.0.0.0 \
  --port 8000 \
  --tp-size 8 \
  --mem-fraction-static 0.8 \
  --context-length 262144 \
  --reasoning-parser qwen3
```

先不要把完整 8 分钟视频一次性高帧率输入。建议从 540p 代理视频、`fps=1` 开始。

## 请求格式重点

请求脚本使用 OpenAI 兼容 Chat Completions：

```json
{
  "role": "user",
  "content": [
    {"type": "video_url", "video_url": {"url": "..."}},
    {"type": "text", "text": "请输出结构化事件表"}
  ]
}
```

第一轮固定 `fps=1`。对于 4 到 8 分钟视频，大致是 250 到 500 帧级别，已经足够判断流程是否通。后续正式构建证据链时，不建议直接升高全片帧率，而是按 30 到 60 秒窗口分段处理。

## 调用示例

部署服务后，在本地或集群登录节点运行：

```bash
python scripts/qwen_video_probe.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen3-VL-32B-Instruct \
  --signed-url-csv data/tmp/cos_urls/p04_proxy_cos_urls.csv \
  --video-id P04_106 \
  --fps 1 \
  --output-json data/tmp/qwen_outputs/P04_106.json
```

如果是在集群节点上调用，需要先把 `p04_proxy_cos_urls.csv` 和 `qwen_video_probe.py` 复制过去。

## 第一轮标注建议

第一轮不要让模型直接生成 benchmark 问题。先让模型输出结构化事件表：

- 场景摘要；
- 关键动作；
- 被交互物体；
- 物体位置变化；
- 食材或工具状态变化；
- 可作为跨会话记忆的问题候选。

等事件表稳定后，再由第二步脚本生成跨会话问题和证据链。
