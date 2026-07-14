#!/usr/bin/env python3
"""Batch-call an OpenAI-compatible chat endpoint for JSONL text aggregation."""

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
    from .pipeline_progress import progress_line
else:
    from llm_json_cleaning import clean_chat_completion_response, extract_json_text
    from pipeline_progress import progress_line

STATUS_FIELDS = [
    "updated_at",
    "status",
    "error",
    "record_id",
    "raw_output",
    "clean_output",
    "clean_method",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def upsert_csv(path: Path, row: dict[str, str], fieldnames: list[str], key_field: str) -> None:
    rows = read_csv(path) if path.exists() else []
    rows = [existing for existing in rows if existing.get(key_field) != row.get(key_field)]
    rows.append({key: row.get(key, "") for key in fieldnames})
    write_csv(path, rows, fieldnames)


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


def record_id_from_record(record: dict[str, Any]) -> str:
    for key in ("record_id", "session_id", "window_id", "source_video_id", "video_id"):
        value = record.get(key)
        if value:
            return str(value)
    raise ValueError(f"Input record has no usable id: {record}")


def make_prompt_text(prompt: str, record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return f"{prompt.rstrip()}\n\n输入 JSON：\n{payload}"


def usage_fields(response: dict[str, Any]) -> dict[str, str]:
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": str(usage.get("prompt_tokens", "")),
        "completion_tokens": str(usage.get("completion_tokens", "")),
        "total_tokens": str(usage.get("total_tokens", "")),
    }


def call_model(client: Any, args: argparse.Namespace, prompt_text: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    extra_body = parse_json_object_arg(args.extra_body_json)
    if extra_body:
        kwargs["extra_body"] = extra_body
    response = client.chat.completions.create(**kwargs)
    return response.model_dump()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--extra-body-json",
        help="Extra JSON object passed as OpenAI-compatible request extra_body.",
    )
    parser.add_argument("--record-ids", help="Comma-separated record ids to run.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    args = parser.parse_args()

    from openai import OpenAI

    records = read_jsonl(Path(args.input_jsonl))
    wanted_records = parse_list(args.record_ids)
    selected: list[dict[str, Any]] = []
    for record in records:
        record_id = record_id_from_record(record)
        if wanted_records is not None and record_id not in wanted_records:
            continue
        selected.append(record)
    if args.limit is not None:
        selected = selected[: args.limit]

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    output_dir = Path(args.output_dir)
    status_csv = output_dir / "batch_status.csv"
    client = OpenAI(api_key=args.api_key, base_url=args.base_url, timeout=3600)

    print(f"Selected records: {len(selected)}", flush=True)
    batch_started = time.monotonic()
    for idx, record in enumerate(selected, start=1):
        record_id = record_id_from_record(record)
        raw_path = output_dir / f"{record_id}.json"
        clean_path = output_dir / f"{record_id}.clean.json"
        print(
            "\n"
            + progress_line(
                idx - 1,
                len(selected),
                time.monotonic() - batch_started,
                prefix="Text batch",
            )
            + f" | next={record_id}",
            flush=True,
        )

        if raw_path.exists() and clean_path.exists() and not args.overwrite:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "skipped",
                "error": "",
                "record_id": record_id,
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
            prompt_text = make_prompt_text(prompt, record)
            response = call_model(client, args, prompt_text)
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
                "raw_output": str(raw_path),
                "clean_output": "",
                "clean_method": "",
                "finish_reason": "",
                **usage_fields({}),
            }
            print(f"ERROR {record_id}: {exc!r}", flush=True)
        upsert_csv(status_csv, status, STATUS_FIELDS, "record_id")
        if args.sleep_seconds:
            time.sleep(args.sleep_seconds)

    print(
        progress_line(
            len(selected),
            len(selected),
            time.monotonic() - batch_started,
            prefix="Text batch",
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
