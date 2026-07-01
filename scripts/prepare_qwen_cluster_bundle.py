#!/usr/bin/env python3
"""Prepare a small bundle for probing COS-hosted videos from a cluster."""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/tmp/qwen_cluster_bundle"


RUN_SH = """#!/usr/bin/env bash
set -euo pipefail

# Set this if your cluster resolves Hugging Face through a mirror.
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

# Change these for your vLLM/SGLang endpoint.
BASE_URL="${BASE_URL:-http://127.0.0.1:8000/v1}"
MODEL="${MODEL:-Qwen/Qwen3-VL-32B-Instruct}"
VIDEO_ID="${VIDEO_ID:-P04_106}"

python qwen_video_probe.py \\
  --base-url "$BASE_URL" \\
  --model "$MODEL" \\
  --signed-url-csv p04_proxy_cos_urls.csv \\
  --video-id "$VIDEO_ID" \\
  --fps "${FPS:-1}" \\
  --max-tokens "${MAX_TOKENS:-4096}" \\
  --prompt "$(cat video_event_schema_zh.txt)" \\
  --output-json "outputs/${VIDEO_ID}.json"
"""


README = """# Qwen 集群视频输入试跑包

这个目录只包含跑通视频输入链路需要的最小文件：

- `qwen_video_probe.py`：OpenAI 兼容接口调用脚本。
- `qwen_video_batch.py`：按 COS URL 表批量调用视频模型。
- `extract_qwen_json.py`：从原始响应中抽取并校验干净 JSON。
- `p04_proxy_cos_urls.csv`：540p 代理视频的 COS 签名 URL。
- `video_event_schema_zh.txt`：第一轮结构化事件表提示词。
- `run_probe.sh`：示例调用脚本。

用法：

```bash
chmod +x run_probe.sh
BASE_URL=http://127.0.0.1:8000/v1 \\
MODEL=Qwen/Qwen3-VL-32B-Instruct \\
VIDEO_ID=P04_106 \\
FPS=1 \\
MAX_TOKENS=4096 \\
./run_probe.sh
```

抽取干净 JSON：

```bash
python extract_qwen_json.py outputs/P04_106.json --output outputs/P04_106.clean.json
```

批量调用：

```bash
python qwen_video_batch.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model qwen35-a3b \
  --signed-url-csv p04_proxy_cos_urls.csv \
  --prompt-file video_event_schema_zh.txt \
  --output-dir outputs \
  --fps 1 \
  --max-tokens 4096
```

如果你用 `Qwen/Qwen3.5-35B-A3B`，先把 `MODEL` 改掉。若服务端不接受 `video_url`，先用 Qwen3-VL 跑通视频链路，再回到 3.5 A3B 做 image/frame 输入兼容。
"""


def copy_file(src: Path, dst: Path) -> None:
    if not src.exists():
        raise SystemExit(f"Missing required file: {src}")
    shutil.copy2(src, dst)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "outputs").mkdir(exist_ok=True)
    copy_file(ROOT / "scripts/qwen_video_probe.py", OUT / "qwen_video_probe.py")
    copy_file(ROOT / "scripts/qwen_video_batch.py", OUT / "qwen_video_batch.py")
    copy_file(ROOT / "scripts/extract_qwen_json.py", OUT / "extract_qwen_json.py")
    copy_file(ROOT / "data/tmp/cos_urls/p04_proxy_cos_urls.csv", OUT / "p04_proxy_cos_urls.csv")
    copy_file(ROOT / "prompts/video_event_schema_zh.txt", OUT / "video_event_schema_zh.txt")
    (OUT / "run_probe.sh").write_text(RUN_SH, encoding="utf-8")
    (OUT / "README.md").write_text(README, encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
