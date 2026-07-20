#!/usr/bin/env python3
"""Audit whether selected Ego4D video_540ss objects are downloadable."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse


AUDIT_FIELDS = [
    "video_uid",
    "participant_id",
    "benchmark_session_order",
    "status",
    "bucket",
    "key",
    "size_bytes",
    "error",
    "checked_at",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in AUDIT_FIELDS}
            for row in rows
        )
    temporary.replace(path)


def classify_head_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "").casefold()
            if code in {
                "expiredtoken",
                "invalidaccesskeyid",
                "invalidtoken",
                "requestexpired",
                "signaturedoesnotmatch",
            }:
                return "error"
            if code in {"403", "accessdenied", "allaccessdisabled"}:
                return "forbidden"
            if code in {"404", "nosuchkey", "notfound"}:
                return "not_found"
    text = str(exc).casefold()
    if any(
        marker in text
        for marker in (
            "expiredtoken",
            "invalidaccesskeyid",
            "invalid token",
            "request has expired",
            "signaturedoesnotmatch",
        )
    ):
        return "error"
    if "(403)" in text or "forbidden" in text or "accessdenied" in text:
        return "forbidden"
    if "(404)" in text or "not found" in text or "nosuchkey" in text:
        return "not_found"
    return "error"


def audit_download_access(
    source_rows: Sequence[dict[str, str]],
    download_rows: Sequence[dict[str, str]],
    head_object: Callable[[str, str], dict[str, object]],
    *,
    workers: int = 16,
    checked_at: str | None = None,
) -> list[dict[str, object]]:
    if workers < 1:
        raise ValueError("workers must be >= 1")

    download_by_uid: dict[str, dict[str, str]] = {}
    for row in download_rows:
        video_uid = row.get("video_uid", "").strip()
        if not video_uid:
            continue
        if video_uid in download_by_uid:
            raise ValueError(f"Duplicate video_uid in download manifest: {video_uid}")
        download_by_uid[video_uid] = row

    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    results: dict[str, dict[str, object]] = {}
    downloadable: list[tuple[dict[str, str], str, str]] = []
    seen: set[str] = set()
    for row in source_rows:
        video_uid = row.get("video_uid", "").strip()
        if not video_uid:
            raise ValueError("Benchmark manifest contains an empty video_uid")
        if video_uid in seen:
            raise ValueError(f"Duplicate video_uid in benchmark manifest: {video_uid}")
        seen.add(video_uid)
        download = download_by_uid.get(video_uid)
        if download is None:
            results[video_uid] = {
                "video_uid": video_uid,
                "participant_id": row.get("participant_id", ""),
                "benchmark_session_order": row.get("benchmark_session_order", ""),
                "status": "not_in_download_manifest",
                "bucket": "",
                "key": "",
                "size_bytes": "",
                "error": "",
                "checked_at": checked_at,
            }
            continue
        parsed = urlparse(download.get("s3_path", ""))
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
            raise ValueError(
                f"Invalid s3_path for {video_uid}: {download.get('s3_path')!r}"
            )
        downloadable.append((row, parsed.netloc, parsed.path.lstrip("/")))

    def check(item: tuple[dict[str, str], str, str]) -> dict[str, object]:
        row, bucket, key = item
        video_uid = row["video_uid"]
        try:
            response = head_object(bucket, key)
        except Exception as exc:
            return {
                "video_uid": video_uid,
                "participant_id": row.get("participant_id", ""),
                "benchmark_session_order": row.get("benchmark_session_order", ""),
                "status": classify_head_error(exc),
                "bucket": bucket,
                "key": key,
                "size_bytes": "",
                "error": repr(exc),
                "checked_at": checked_at,
            }
        return {
            "video_uid": video_uid,
            "participant_id": row.get("participant_id", ""),
            "benchmark_session_order": row.get("benchmark_session_order", ""),
            "status": "available",
            "bucket": bucket,
            "key": key,
            "size_bytes": int(response.get("ContentLength") or 0),
            "error": "",
            "checked_at": checked_at,
        }

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check, item): item[0]["video_uid"] for item in downloadable}
        for future in as_completed(futures):
            result = future.result()
            results[str(result["video_uid"])] = result
            completed += 1
            if completed % 250 == 0 or completed == len(downloadable):
                print(
                    f"HEAD audit: {completed}/{len(downloadable)}",
                    flush=True,
                )

    return [results[row["video_uid"]] for row in source_rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--source-manifest",
        type=Path,
        help=(
            "Candidate or benchmark CSV. When candidate_status is present, only "
            "eligible rows are audited."
        ),
    )
    source.add_argument(
        "--benchmark-manifest",
        dest="source_manifest",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--download-manifest", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--available-video-uids-output", type=Path, required=True)
    parser.add_argument("--aws-profile", default="ego4d")
    parser.add_argument("--region", default="us-west-1")
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    return args


def main() -> None:
    args = parse_args()
    import boto3
    from botocore.config import Config

    source_rows = read_csv(args.source_manifest)
    if source_rows and "candidate_status" in source_rows[0]:
        source_rows = [
            row for row in source_rows if row.get("candidate_status") == "eligible"
        ]
    if not source_rows:
        raise SystemExit(f"No rows selected from {args.source_manifest}")
    download_rows = read_csv(args.download_manifest)
    client = boto3.Session(
        profile_name=args.aws_profile,
        region_name=args.region,
    ).client(
        "s3",
        config=Config(
            connect_timeout=10,
            read_timeout=20,
            max_pool_connections=max(16, args.workers),
            retries={"max_attempts": 10, "mode": "standard"},
        ),
    )

    rows = audit_download_access(
        source_rows,
        download_rows,
        lambda bucket, key: client.head_object(Bucket=bucket, Key=key),
        workers=args.workers,
    )
    write_csv(args.output_csv, rows)
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        counts[status] = counts.get(status, 0) + 1
    print(f"Audit status: {counts}")
    print(f"Wrote {args.output_csv}")
    if counts.get("error"):
        raise SystemExit(
            f"Access audit has {counts['error']} indeterminate errors; "
            "do not regenerate benchmark manifests from this audit."
        )
    available = [str(row["video_uid"]) for row in rows if row["status"] == "available"]
    args.available_video_uids_output.parent.mkdir(parents=True, exist_ok=True)
    args.available_video_uids_output.write_text(
        "".join(f"{video_uid}\n" for video_uid in available),
        encoding="utf-8",
    )
    print(f"Wrote {args.available_video_uids_output}")


if __name__ == "__main__":
    main()
