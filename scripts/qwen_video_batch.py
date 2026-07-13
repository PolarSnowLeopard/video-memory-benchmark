#!/usr/bin/env python3
"""Batch-call an OpenAI-compatible VLM endpoint for COS-hosted videos."""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .llm_json_cleaning import clean_chat_completion_response, extract_json_text
else:
    from llm_json_cleaning import clean_chat_completion_response, extract_json_text

STATUS_FIELDS = [
    "updated_at",
    "status",
    "error",
    "record_id",
    "session_id",
    "video_id",
    "source_video_id",
    "raw_output",
    "clean_output",
    "clean_method",
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
    if row.get("source_video_id"):
        return row["source_video_id"]
    key = row.get("key") or row.get("local_path") or ""
    stem = Path(key).stem
    for suffix in ("_540p16", "_540p", "_proxy"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def record_id_from_row(row: dict[str, str]) -> str:
    return row.get("session_id") or row.get("record_id") or video_id_from_row(row)


def source_video_id_from_row(row: dict[str, str]) -> str:
    return row.get("source_video_id") or row.get("video_id") or video_id_from_row(row)


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def parse_json_object_arg(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("JSON argument must be an object")
    return payload


def build_extra_body(args: argparse.Namespace) -> dict[str, Any]:
    extra_body = parse_json_object_arg(args.extra_body_json)
    mm_processor_kwargs = dict(extra_body.get("mm_processor_kwargs") or {})
    mm_processor_kwargs.update({"fps": args.fps, "do_sample_frames": True})
    extra_body["mm_processor_kwargs"] = mm_processor_kwargs
    return extra_body


def row_context_text(row: dict[str, str]) -> str:
    keys = [
        "session_id",
        "source_video_id",
        "video_id",
        "participant_id",
        "session_index",
        "start_sec",
        "end_sec",
        "duration_sec",
    ]
    items = [f"{key}={row[key]}" for key in keys if row.get(key)]
    if not items:
        return ""
    return (
        "\n\n本次输入元信息：\n"
        + "\n".join(f"- {item}" for item in items)
        + (
            "\n请在输出 JSON 中复制 session_id/source_video_id（如果存在）。"
            "如果提示词要求复制 start_sec/end_sec，就直接复制输入元信息中的原始秒数；"
            "其他事件、状态和证据时间默认写相对当前输入视频片段的 MM:SS。"
        )
    )


def usage_fields(response: dict) -> dict[str, str]:
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": str(usage.get("prompt_tokens", "")),
        "completion_tokens": str(usage.get("completion_tokens", "")),
        "total_tokens": str(usage.get("total_tokens", "")),
    }


def call_model(client: Any, args: argparse.Namespace, signed_url: str, prompt: str) -> dict:
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
        extra_body=build_extra_body(args),
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
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--video-ids", help="Comma-separated subset from URL CSV")
    parser.add_argument("--record-ids", help="Comma-separated record ids, usually session_id for session CSVs.")
    parser.add_argument(
        "--extra-body-json",
        help="Extra JSON object merged into the OpenAI-compatible request extra_body.",
    )
    parser.add_argument("--no-row-context", action="store_true", help="Do not append URL CSV row metadata to the prompt.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    args = parser.parse_args()

    from openai import OpenAI

    url_rows = read_rows(Path(args.signed_url_csv))
    wanted_videos = parse_list(args.video_ids)
    wanted_records = parse_list(args.record_ids)
    selected: list[dict[str, str]] = []
    for row in url_rows:
        video_id = video_id_from_row(row)
        source_video_id = source_video_id_from_row(row)
        record_id = record_id_from_row(row)
        if wanted_records is not None and record_id not in wanted_records:
            continue
        if wanted_videos is not None and video_id not in wanted_videos and source_video_id not in wanted_videos:
            continue
        row["_record_id"] = record_id
        row["_video_id"] = video_id
        row["_source_video_id"] = source_video_id
        row["_session_id"] = row.get("session_id", "")
        selected.append(row)
    if args.limit is not None:
        selected = selected[: args.limit]

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    output_dir = Path(args.output_dir)
    status_csv = output_dir / "batch_status.csv"
    client = OpenAI(api_key=args.api_key, base_url=args.base_url, timeout=3600)

    print(f"Selected videos: {len(selected)}", flush=True)
    for idx, row in enumerate(selected, start=1):
        record_id = row["_record_id"]
        video_id = row["_video_id"]
        source_video_id = row["_source_video_id"]
        session_id = row["_session_id"]
        raw_path = output_dir / f"{record_id}.json"
        clean_path = output_dir / f"{record_id}.clean.json"
        print(f"\n[{idx}/{len(selected)}] {record_id}", flush=True)

        if raw_path.exists() and clean_path.exists() and not args.overwrite:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "skipped",
                "error": "",
                "record_id": record_id,
                "session_id": session_id,
                "video_id": video_id,
                "source_video_id": source_video_id,
                "raw_output": str(raw_path),
                "clean_output": str(clean_path),
                "clean_method": "existing",
                "finish_reason": "",
                **usage_fields({}),
            }
            upsert_csv(status_csv, status, STATUS_FIELDS, "record_id")
            print("Skipped existing clean output", flush=True)
            continue

        if raw_path.exists() and not clean_path.exists() and not args.overwrite:
            try:
                response = json.loads(raw_path.read_text(encoding="utf-8"))
                clean_method = clean_chat_completion_response(raw_path, clean_path)
            except Exception as exc:
                print(f"Existing raw output is not recoverable: {exc!r}", flush=True)
            else:
                status = {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "ok",
                    "error": "",
                    "record_id": record_id,
                    "session_id": session_id,
                    "video_id": video_id,
                    "source_video_id": source_video_id,
                    "raw_output": str(raw_path),
                    "clean_output": str(clean_path),
                    "clean_method": clean_method,
                    "finish_reason": str((response.get("choices") or [{}])[0].get("finish_reason", "")),
                    **usage_fields(response),
                }
                upsert_csv(status_csv, status, STATUS_FIELDS, "record_id")
                print(f"Recovered existing raw output with {clean_method}", flush=True)
                continue

        try:
            row_prompt = prompt if args.no_row_context else prompt + row_context_text(row)
            response = call_model(client, args, row["signed_url"], row_prompt)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
            finish_reason = str((response.get("choices") or [{}])[0].get("finish_reason", ""))
            clean_error = ""
            clean_method = ""
            try:
                clean_method = clean_chat_completion_response(raw_path, clean_path)
                status_value = "ok"
            except Exception as exc:
                status_value = "raw_only"
                clean_error = repr(exc)
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": status_value,
                "error": clean_error,
                "record_id": record_id,
                "session_id": session_id,
                "video_id": video_id,
                "source_video_id": source_video_id,
                "raw_output": str(raw_path),
                "clean_output": str(clean_path if clean_path.exists() else ""),
                "clean_method": clean_method,
                "finish_reason": finish_reason,
                **usage_fields(response),
            }
        except Exception as exc:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": repr(exc),
                "record_id": record_id,
                "session_id": session_id,
                "video_id": video_id,
                "source_video_id": source_video_id,
                "raw_output": str(raw_path),
                "clean_output": "",
                "clean_method": "",
                "finish_reason": "",
                **usage_fields({}),
            }
            print(f"ERROR {video_id}: {exc!r}", flush=True)
        upsert_csv(status_csv, status, STATUS_FIELDS, "record_id")
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)


if __name__ == "__main__":
    main()
