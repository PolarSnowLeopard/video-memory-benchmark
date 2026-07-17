#!/usr/bin/env python3
"""Run Batch-compatible JSONL requests through Bailian's online API concurrently."""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:
    from scripts.pipeline_progress import format_duration, progress_line
except ModuleNotFoundError:  # Direct execution via `python3 scripts/...`.
    from pipeline_progress import format_duration, progress_line


DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
            custom_id = str(record.get("custom_id") or "")
            if not custom_id:
                raise ValueError(f"Request at {path}:{line_number} has no custom_id")
            if custom_id in seen:
                raise ValueError(f"Duplicate custom_id in input: {custom_id}")
            if str(record.get("method") or "").upper() != "POST":
                raise ValueError(f"Only POST requests are supported: {custom_id}")
            if not isinstance(record.get("body"), dict):
                raise ValueError(f"Request has no JSON body: {custom_id}")
            seen.add(custom_id)
            records.append(record)
    return records


def existing_success_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid existing JSONL at {path}:{line_number}") from exc
            custom_id = str(record.get("custom_id") or "")
            if not custom_id:
                raise ValueError(f"Existing result has no custom_id at {path}:{line_number}")
            if custom_id in ids:
                raise ValueError(f"Duplicate custom_id in existing results: {custom_id}")
            response = record.get("response") or {}
            if int(response.get("status_code") or 0) != 200:
                raise ValueError(f"Existing result is not successful: {custom_id}")
            ids.add(custom_id)
    return ids


class RollingRateLimiter:
    def __init__(self, rpm: int, period_sec: float = 60.0) -> None:
        if rpm < 1:
            raise ValueError("rpm must be >= 1")
        self.rpm = rpm
        self.period_sec = period_sec
        self.timestamps: deque[float] = deque()
        self.condition = threading.Condition()

    def acquire(self) -> None:
        with self.condition:
            while True:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.period_sec:
                    self.timestamps.popleft()
                if len(self.timestamps) < self.rpm:
                    self.timestamps.append(now)
                    return
                self.condition.wait(
                    timeout=max(0.001, self.period_sec - (now - self.timestamps[0]))
                )


def retry_after_seconds(headers: Any) -> float | None:
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def batch_success_line(
    custom_id: str,
    body: dict[str, Any],
    request_id: str = "",
) -> dict[str, Any]:
    return {
        "id": request_id or f"online-{uuid.uuid4()}",
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": request_id,
            "body": body,
        },
        "error": None,
    }


def batch_error_line(
    custom_id: str,
    error_type: str,
    message: str,
    attempts: int,
    status_code: int | None = None,
) -> dict[str, Any]:
    return {
        "id": f"online-error-{uuid.uuid4()}",
        "custom_id": custom_id,
        "response": None,
        "error": {
            "type": error_type,
            "message": message[:4000],
            "status_code": status_code,
            "attempts": attempts,
        },
    }


def online_endpoint(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def execute_request(
    record: dict[str, Any],
    api_key: str,
    base_url: str,
    limiter: RollingRateLimiter,
    max_attempts: int,
    timeout_sec: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    custom_id = str(record["custom_id"])
    payload = json.dumps(
        record["body"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    last_error: Exception | None = None
    last_status: int | None = None
    for attempt in range(1, max_attempts + 1):
        limiter.acquire()
        request = urllib.request.Request(
            online_endpoint(base_url),
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "video-memory-benchmark-online-qc/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                response_body = json.loads(response.read())
                request_id = str(
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                    or response_body.get("request_id")
                    or ""
                )
                return batch_success_line(custom_id, response_body, request_id), None
        except urllib.error.HTTPError as exc:
            last_error = exc
            last_status = exc.code
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            if exc.code not in RETRYABLE_STATUS_CODES or attempt >= max_attempts:
                return None, batch_error_line(
                    custom_id,
                    "HTTPError",
                    detail or str(exc),
                    attempt,
                    exc.code,
                )
            delay = retry_after_seconds(exc.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                return None, batch_error_line(
                    custom_id, type(exc).__name__, repr(exc), attempt
                )
            delay = None
        if delay is None:
            delay = min(60.0, 2 ** (attempt - 1)) + random.random()
        time.sleep(delay)
    return None, batch_error_line(
        custom_id,
        type(last_error).__name__ if last_error else "UnknownError",
        repr(last_error),
        max_attempts,
        last_status,
    )


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a", encoding="utf-8")
        self.lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            self.handle.write(line)
            self.handle.flush()
            os.fsync(self.handle.fileno())

    def close(self) -> None:
        self.handle.close()


def parse_id_filter(value: str | None) -> set[str] | None:
    if not value:
        return None
    result = {item.strip() for item in value.split(",") if item.strip()}
    return result or None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", required=True)
    parser.add_argument("--output-jsonl", required=True)
    parser.add_argument("--error-jsonl", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--max-workers", type=int, default=100)
    parser.add_argument("--rpm", type=int, default=300)
    parser.add_argument("--max-attempts", type=int, default=5)
    parser.add_argument("--timeout-sec", type=float, default=3600)
    parser.add_argument("--record-ids")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.max_workers < 1:
        parser.error("--max-workers must be >= 1")
    if args.rpm < 1:
        parser.error("--rpm must be >= 1")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be >= 1")
    if args.timeout_sec <= 0:
        parser.error("--timeout-sec must be > 0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")

    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(f"Missing required environment variable: {args.api_key_env}")
    records = read_jsonl(Path(args.input_jsonl))
    wanted = parse_id_filter(args.record_ids)
    if wanted is not None:
        records = [record for record in records if record["custom_id"] in wanted]
        missing = sorted(wanted - {str(record["custom_id"]) for record in records})
        if missing:
            raise ValueError(f"Unknown record ids: {', '.join(missing)}")
    if args.limit is not None:
        records = records[: args.limit]

    output_path = Path(args.output_jsonl)
    error_path = Path(args.error_jsonl)
    completed_ids = existing_success_ids(output_path)
    selected = [
        record for record in records if str(record["custom_id"]) not in completed_ids
    ]
    print(
        f"Selected={len(records)} already_completed={len(records) - len(selected)} "
        f"pending={len(selected)} workers={args.max_workers} rpm={args.rpm}",
        flush=True,
    )
    if not selected:
        return

    result_writer = JsonlWriter(output_path)
    error_writer = JsonlWriter(error_path)
    limiter = RollingRateLimiter(args.rpm)
    started = time.monotonic()
    succeeded = 0
    failed = 0
    try:
        with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
            future_ids = {
                executor.submit(
                    execute_request,
                    record,
                    api_key,
                    args.base_url,
                    limiter,
                    args.max_attempts,
                    args.timeout_sec,
                ): str(record["custom_id"])
                for record in selected
            }
            for future in as_completed(future_ids):
                result, error = future.result()
                if result is not None:
                    result_writer.write(result)
                    succeeded += 1
                else:
                    error_writer.write(error or {})
                    failed += 1
                done = succeeded + failed
                elapsed = time.monotonic() - started
                completed_rpm = done / elapsed * 60 if elapsed else 0.0
                print(
                    "\r"
                    + progress_line(done, len(selected), elapsed, prefix="Online QC")
                    + f" | ok={succeeded} failed={failed} completed_rpm={completed_rpm:.2f}",
                    end="\n" if done == len(selected) else "",
                    flush=True,
                )
    finally:
        result_writer.close()
        error_writer.close()
    elapsed = time.monotonic() - started
    print(
        f"Finished: ok={succeeded} failed={failed} elapsed={format_duration(elapsed)} "
        f"completed_rpm={(succeeded + failed) / elapsed * 60:.2f}",
        flush=True,
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
