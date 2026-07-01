#!/usr/bin/env python3
"""Extract and validate the assistant JSON content from a Qwen API response."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_text(text: str) -> str:
    text = text.strip()
    match = FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    raise ValueError("No JSON object found in assistant content")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("response_json", help="Raw OpenAI-compatible API response JSON")
    parser.add_argument("--output", required=True, help="Clean extracted JSON path")
    args = parser.parse_args()

    response = json.loads(Path(args.response_json).read_text(encoding="utf-8"))
    content = response["choices"][0]["message"]["content"]
    clean_text = extract_json_text(content)
    payload = json.loads(clean_text)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
