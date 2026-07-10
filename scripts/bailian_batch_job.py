#!/usr/bin/env python3
"""Submit, inspect, and download Bailian OpenAI-compatible Batch jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
SECRET_FIELDS = {"api_key", "secret_key", "authorization"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_api_key(env_name: str = "DASHSCOPE_API_KEY") -> str:
    value = os.environ.get(env_name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {env_name}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_job_record(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_job_record(path: Path, record: dict[str, Any]) -> None:
    safe_record = {key: value for key, value in record.items() if key.lower() not in SECRET_FIELDS}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(safe_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_can_submit(job_record_path: Path, force_new: bool) -> None:
    if not job_record_path.exists() or force_new:
        return
    record = read_job_record(job_record_path)
    status = str(record.get("status") or "unknown")
    if record.get("batch_id") and status not in TERMINAL_STATUSES:
        raise RuntimeError(
            f"Existing Batch job {record['batch_id']} is {status}; use --force-new to submit another job"
        )


def object_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Cannot serialize API object: {type(value)!r}")


def make_client(base_url: str, api_key_env: str) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=require_api_key(api_key_env), base_url=base_url, timeout=3600)


def validate_input_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"Batch input is empty: {path}")
    if size > 500 * 1024 * 1024:
        raise ValueError(f"Batch input exceeds 500 MB: {path}")


def submit_job(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input_jsonl)
    job_path = Path(args.job_record)
    validate_input_file(input_path)
    ensure_can_submit(job_path, args.force_new)
    client = make_client(args.base_url, args.api_key_env)
    with input_path.open("rb") as handle:
        uploaded = client.files.create(file=handle, purpose="batch")
    uploaded_data = object_dict(uploaded)
    input_file_id = str(uploaded_data.get("id") or "")
    if not input_file_id:
        raise RuntimeError("Bailian file upload response has no id")
    metadata = {
        "ds_name": args.name,
        "ds_description": args.description,
        "input_sha256": sha256_file(input_path),
    }
    batch = client.batches.create(
        input_file_id=input_file_id,
        endpoint="/v1/chat/completions",
        completion_window=args.completion_window,
        metadata=metadata,
    )
    batch_data = object_dict(batch)
    record = {
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "base_url": args.base_url,
        "input_jsonl": str(input_path),
        "input_sha256": metadata["input_sha256"],
        "input_size_bytes": input_path.stat().st_size,
        "input_file_id": input_file_id,
        "batch_id": batch_data.get("id"),
        "status": batch_data.get("status"),
        "endpoint": "/v1/chat/completions",
        "completion_window": args.completion_window,
        "output_file_id": batch_data.get("output_file_id"),
        "error_file_id": batch_data.get("error_file_id"),
        "request_counts": batch_data.get("request_counts"),
        "metadata": metadata,
    }
    write_job_record(job_path, record)
    return record


def refresh_job(client: Any, record: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(record.get("batch_id") or "")
    if not batch_id:
        raise ValueError("Job record has no batch_id")
    batch_data = object_dict(client.batches.retrieve(batch_id))
    record.update(
        {
            "updated_at": utc_now(),
            "status": batch_data.get("status"),
            "output_file_id": batch_data.get("output_file_id"),
            "error_file_id": batch_data.get("error_file_id"),
            "request_counts": batch_data.get("request_counts"),
            "errors": batch_data.get("errors"),
            "completed_at": batch_data.get("completed_at"),
            "failed_at": batch_data.get("failed_at"),
            "expires_at": batch_data.get("expires_at"),
        }
    )
    return record


def status_job(args: argparse.Namespace) -> dict[str, Any]:
    job_path = Path(args.job_record)
    record = read_job_record(job_path)
    client = make_client(args.base_url or str(record.get("base_url") or DEFAULT_BASE_URL), args.api_key_env)
    record = refresh_job(client, record)
    write_job_record(job_path, record)
    return record


def write_remote_file(client: Any, file_id: str, path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output already exists; use --overwrite: {path}")
    response = client.files.content(file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(response, "write_to_file"):
        response.write_to_file(path)
        return
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        path.write_bytes(content)
        return
    if hasattr(response, "read"):
        path.write_bytes(response.read())
        return
    raise TypeError(f"Unsupported file content response: {type(response)!r}")


def download_job(args: argparse.Namespace) -> dict[str, Any]:
    job_path = Path(args.job_record)
    record = read_job_record(job_path)
    base_url = args.base_url or str(record.get("base_url") or DEFAULT_BASE_URL)
    client = make_client(base_url, args.api_key_env)
    record = refresh_job(client, record)
    output_file_id = str(record.get("output_file_id") or "")
    error_file_id = str(record.get("error_file_id") or "")
    if not output_file_id and not error_file_id:
        write_job_record(job_path, record)
        raise RuntimeError(f"Batch job has no downloadable files; status={record.get('status')}")
    if output_file_id:
        if not args.output_jsonl:
            raise ValueError("--output-jsonl is required when the job has an output file")
        write_remote_file(client, output_file_id, Path(args.output_jsonl), args.overwrite)
        record["downloaded_output_jsonl"] = args.output_jsonl
    if error_file_id:
        if not args.error_jsonl:
            raise ValueError("--error-jsonl is required when the job has an error file")
        write_remote_file(client, error_file_id, Path(args.error_jsonl), args.overwrite)
        record["downloaded_error_jsonl"] = args.error_jsonl
    record["updated_at"] = utc_now()
    write_job_record(job_path, record)
    return record


def add_connection_args(parser: argparse.ArgumentParser, optional_base_url: bool = False) -> None:
    parser.add_argument(
        "--base-url",
        default=None if optional_base_url else DEFAULT_BASE_URL,
        help="Bailian OpenAI-compatible base URL.",
    )
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit")
    submit.add_argument("--input-jsonl", required=True)
    submit.add_argument("--job-record", required=True)
    submit.add_argument("--name", default="video-evidence-qc")
    submit.add_argument("--description", default="Hierarchical video evidence quality control")
    submit.add_argument("--completion-window", default="24h")
    submit.add_argument("--force-new", action="store_true")
    add_connection_args(submit)

    status = subparsers.add_parser("status")
    status.add_argument("--job-record", required=True)
    add_connection_args(status, optional_base_url=True)

    download = subparsers.add_parser("download")
    download.add_argument("--job-record", required=True)
    download.add_argument("--output-jsonl")
    download.add_argument("--error-jsonl")
    download.add_argument("--overwrite", action="store_true")
    add_connection_args(download, optional_base_url=True)

    args = parser.parse_args()
    if args.command == "submit":
        result = submit_job(args)
    elif args.command == "status":
        result = status_job(args)
    else:
        result = download_job(args)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
