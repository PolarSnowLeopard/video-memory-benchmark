#!/usr/bin/env python3
"""Send a COS-hosted video URL to an OpenAI-compatible Qwen VL endpoint."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from openai import OpenAI


DEFAULT_PROMPT = """请观看这段第一人称厨房视频，输出 JSON：
{
  "summary": "一句话概括",
  "main_actions": ["按时间顺序列出关键动作"],
  "objects": ["出现并被交互的关键物体"],
  "state_changes": [
    {"object": "物体", "before": "之前状态", "after": "之后状态", "evidence": "可见证据"}
  ],
  "memory_candidates": ["适合跨会话记忆问答的事实"]
}
只输出 JSON，不要输出额外解释。"""


def read_signed_url(csv_path: Path, video_id: str) -> str:
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = row["key"]
            name = Path(key).name
            if name.startswith(video_id):
                return row["signed_url"]
    raise SystemExit(f"Video id not found in {csv_path}: {video_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, e.g. http://host:8000/v1")
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--model", required=True)
    parser.add_argument("--signed-url-csv", required=True)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--output-json", required=True)
    args = parser.parse_args()

    video_url = read_signed_url(Path(args.signed_url_csv), args.video_id)
    client = OpenAI(api_key=args.api_key, base_url=args.base_url, timeout=3600)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": args.prompt},
            ],
        }
    ]
    response = client.chat.completions.create(
        model=args.model,
        messages=messages,
        max_tokens=args.max_tokens,
        temperature=0,
        extra_body={"mm_processor_kwargs": {"fps": args.fps, "do_sample_frames": True}},
    )
    result = response.model_dump()
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
