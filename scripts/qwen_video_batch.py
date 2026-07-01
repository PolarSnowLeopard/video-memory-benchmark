#!/usr/bin/env python3
"""Batch-call an OpenAI-compatible VLM endpoint for COS-hosted videos."""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

STATUS_FIELDS = [
    "updated_at",
    "status",
    "error",
    "video_id",
    "raw_output",
    "clean_output",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def upsert_csv(path: Path, row: dict[str, str], fieldnames: list[str], key_field: str) -> None:
    rows = read_rows(path) if path.exists() else []
    rows = [r for r in rows if r.get(key_field) != row.get(key_field)]
    rows.append({k: row.get(k, "") for k in fieldnames})
    write_csv(path, rows, fieldnames)


def video_id_from_row(row: dict[str, str]) -> str:
    if row.get("video_id"):
        return row["video_id"]
    key = row.get("key") or row.get("local_path") or ""
    stem = Path(key).stem
    for suffix in ("_540p16", "_540p", "_proxy"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


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


def clean_response(raw_path: Path, clean_path: Path) -> None:
    response = json.loads(raw_path.read_text(encoding="utf-8"))
    content = response["choices"][0]["message"].get("content")
    if not content:
        raise ValueError("Assistant content is empty")
    payload = json.loads(extract_json_text(content))
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def usage_fields(response: dict) -> dict[str, str]:
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": str(usage.get("prompt_tokens", "")),
        "completion_tokens": str(usage.get("completion_tokens", "")),
        "total_tokens": str(usage.get("total_tokens", "")),
    }


def call_model(client: OpenAI, args: argparse.Namespace, signed_url: str, prompt: str) -> dict:
    response = client.chat.completions.create(
        model=args.model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": signed_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        extra_body={"mm_processor_kwargs": {"fps": args.fps, "do_sample_frames": True}},
    )
    return response.model_dump()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signed-url-csv", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-file", default="video_event_schema_zh.txt")
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--video-ids", help="Comma-separated subset from URL CSV")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    args = parser.parse_args()

    url_rows = read_rows(Path(args.signed_url_csv))
    wanted = {v.strip() for v in args.video_ids.split(",") if v.strip()} if args.video_ids else None
    selected: list[dict[str, str]] = []
    for row in url_rows:
        video_id = video_id_from_row(row)
        if wanted is not None and video_id not in wanted:
            continue
        row["_video_id"] = video_id
        selected.append(row)
    if args.limit is not None:
        selected = selected[: args.limit]

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    output_dir = Path(args.output_dir)
    status_csv = output_dir / "batch_status.csv"
    client = OpenAI(api_key=args.api_key, base_url=args.base_url, timeout=3600)

    print(f"Selected videos: {len(selected)}", flush=True)
    for idx, row in enumerate(selected, start=1):
        video_id = row["_video_id"]
        raw_path = output_dir / f"{video_id}.json"
        clean_path = output_dir / f"{video_id}.clean.json"
        print(f"\n[{idx}/{len(selected)}] {video_id}", flush=True)

        if raw_path.exists() and clean_path.exists() and not args.overwrite:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "skipped",
                "error": "",
                "video_id": video_id,
                "raw_output": str(raw_path),
                "clean_output": str(clean_path),
                "finish_reason": "",
                **usage_fields({}),
            }
            upsert_csv(status_csv, status, STATUS_FIELDS, "video_id")
            print("Skipped existing clean output", flush=True)
            continue

        try:
            response = call_model(client, args, row["signed_url"], prompt)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
            finish_reason = str((response.get("choices") or [{}])[0].get("finish_reason", ""))
            clean_error = ""
            try:
                clean_response(raw_path, clean_path)
                status_value = "ok"
            except Exception as exc:
                status_value = "raw_only"
                clean_error = repr(exc)
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": status_value,
                "error": clean_error,
                "video_id": video_id,
                "raw_output": str(raw_path),
                "clean_output": str(clean_path if clean_path.exists() else ""),
                "finish_reason": finish_reason,
                **usage_fields(response),
            }
        except Exception as exc:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": repr(exc),
                "video_id": video_id,
                "raw_output": str(raw_path),
                "clean_output": "",
                "finish_reason": "",
                **usage_fields({}),
            }
            print(f"ERROR {video_id}: {exc!r}", flush=True)
        upsert_csv(status_csv, status, STATUS_FIELDS, "video_id")
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)


if __name__ == "__main__":
    main()
