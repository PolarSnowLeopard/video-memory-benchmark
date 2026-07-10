#!/usr/bin/env python3
"""Plan, cut, and optionally upload temporary evidence clips for disputed QC items."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_bailian_qc_batch import proxy_url, source_video_id_from_proxy_row
from scripts.run_epic_vpn_session_batch import (
    content_type_for,
    cut_session,
    make_cos_client,
    resolve_binary,
    upload_session,
)


URL_FIELDS = [
    "clip_id",
    "participant_id",
    "source_video_id",
    "clip_index",
    "start_sec",
    "end_sec",
    "duration_sec",
    "candidate_ids",
    "local_path",
    "bucket",
    "region",
    "key",
    "size_bytes",
    "content_type",
    "signed_url",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fields} for row in rows])


def build_clip_specs(
    review_items: list[dict[str, Any]], clip_sec: int = 30
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    if clip_sec <= 0:
        raise ValueError("clip_sec must be > 0")
    specs: dict[tuple[str, int], dict[str, Any]] = {}
    mappings: dict[str, list[str]] = {}
    for item in review_items:
        source_video_id = str(item.get("source_video_id") or "")
        candidate_id = str(item.get("candidate_id") or "")
        if not source_video_id or not candidate_id:
            raise ValueError("Review item requires source_video_id and candidate_id")
        record_id = str(item.get("record_id") or f"{source_video_id}:{candidate_id}")
        clip_ids: list[str] = []
        support_ranges = item.get("support_ranges") or []
        if not support_ranges:
            raise ValueError(f"Review item has no support ranges: {record_id}")
        for support in support_ranges:
            try:
                start_sec = float(support["start_sec"])
                end_sec = float(support["end_sec"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Review item has invalid support range: {record_id}") from exc
            if start_sec < 0 or end_sec <= start_sec:
                raise ValueError(f"Review item has invalid support range: {record_id}")
            first_index = int(math.floor(start_sec / clip_sec))
            last_index = int(math.ceil(end_sec / clip_sec) - 1)
            for clip_index in range(first_index, last_index + 1):
                clip_id = f"{source_video_id}_qc_s{clip_index:05d}"
                key = (source_video_id, clip_index)
                if key not in specs:
                    specs[key] = {
                        "clip_id": clip_id,
                        "participant_id": str(
                            item.get("participant_id") or source_video_id.split("_", 1)[0]
                        ),
                        "source_video_id": source_video_id,
                        "clip_index": clip_index,
                        "start_sec": float(clip_index * clip_sec),
                        "end_sec": float((clip_index + 1) * clip_sec),
                        "duration_sec": float(clip_sec),
                        "candidate_ids": [],
                    }
                if candidate_id not in specs[key]["candidate_ids"]:
                    specs[key]["candidate_ids"].append(candidate_id)
                if clip_id not in clip_ids:
                    clip_ids.append(clip_id)
        mappings[record_id] = sorted(clip_ids)
    ordered = sorted(specs.values(), key=lambda item: (item["source_video_id"], item["clip_index"]))
    for spec in ordered:
        spec["candidate_ids"] = sorted(spec["candidate_ids"])
    return ordered, mappings


def source_cache_path(source_cache_root: Path, row: dict[str, str], source_id: str) -> Path:
    participant_id = row.get("participant_id") or source_id.split("_", 1)[0]
    return source_cache_root / participant_id / f"{source_id}_540p16.mp4"


def ensure_source_proxy(
    source_cache_root: Path,
    row: dict[str, str],
    source_id: str,
    dry_run: bool,
) -> Path:
    local_value = row.get("local_path")
    if local_value and Path(local_value).is_file():
        return Path(local_value)
    cache_path = source_cache_path(source_cache_root, row, source_id)
    if cache_path.is_file():
        return cache_path
    signed_url = proxy_url(row)
    if not signed_url:
        raise ValueError(f"Missing source proxy signed URL: {source_id}")
    print(f"Downloading source proxy: {source_id} -> {cache_path}", flush=True)
    if dry_run:
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".part")
    with urllib.request.urlopen(signed_url, timeout=3600) as response, temp_path.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    temp_path.replace(cache_path)
    return cache_path


def mapping_records(
    review_items: list[dict[str, Any]],
    mappings: dict[str, list[str]],
    specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specs_by_id = {item["clip_id"]: item for item in specs}
    records: list[dict[str, Any]] = []
    for item in review_items:
        record_id = str(
            item.get("record_id")
            or f"{item.get('source_video_id')}:{item.get('candidate_id')}"
        )
        clip_ids = mappings[record_id]
        record = dict(item)
        record["clip_ids"] = clip_ids
        record["clip_ranges"] = [
            {
                "clip_id": clip_id,
                "start_sec": specs_by_id[clip_id]["start_sec"],
                "end_sec": specs_by_id[clip_id]["end_sec"],
            }
            for clip_id in clip_ids
        ]
        records.append(record)
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-queue", required=True)
    parser.add_argument("--proxy-url-csv", required=True)
    parser.add_argument("--source-cache-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--output-url-csv", required=True)
    parser.add_argument("--mapping-jsonl", required=True)
    parser.add_argument("--cleanup-csv", required=True)
    parser.add_argument("--clip-sec", type=int, default=30)
    parser.add_argument("--ffmpeg-bin")
    parser.add_argument("--cut-mode", choices=["copy", "reencode"], default="copy")
    parser.add_argument("--ffmpeg-threads", type=int, default=2)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--cos-config", default="~/.cos.conf")
    parser.add_argument("--cos-prefix", default="video-benchmark/qc-temp")
    parser.add_argument("--url-expire-days", type=int, default=14)
    parser.add_argument("--delete-local-after-upload", action="store_true")
    parser.add_argument("--delete-source-after", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    review_items = read_jsonl(Path(args.review_queue))
    specs, mappings = build_clip_specs(review_items, args.clip_sec)
    proxy_rows = {
        source_video_id_from_proxy_row(row): row
        for row in read_csv(Path(args.proxy_url_csv))
        if source_video_id_from_proxy_row(row)
    }
    existing_rows = {row.get("clip_id", ""): row for row in read_csv(Path(args.output_url_csv))}
    output_root = Path(args.output_root)
    source_cache_root = Path(args.source_cache_root)
    ffmpeg_bin = resolve_binary("ffmpeg", args.ffmpeg_bin, "FFMPEG_BIN", dry_run=args.dry_run)
    cut_args = SimpleNamespace(
        ffmpeg_bin=ffmpeg_bin,
        cut_mode=args.cut_mode,
        ffmpeg_threads=args.ffmpeg_threads,
        dry_run=args.dry_run,
    )
    cos_client = None
    cos: dict[str, str] = {}
    if args.upload:
        cos_client, cos = make_cos_client(Path(args.cos_config).expanduser())

    source_paths: dict[str, Path] = {}
    output_rows: list[dict[str, Any]] = []
    for spec in specs:
        clip_id = spec["clip_id"]
        existing = existing_rows.get(clip_id)
        if existing and existing.get("signed_url") and not args.overwrite:
            output_rows.append(existing)
            continue
        source_id = spec["source_video_id"]
        source_row = proxy_rows.get(source_id)
        if source_row is None:
            raise ValueError(f"Missing source proxy row: {source_id}")
        source_path = source_paths.get(source_id)
        if source_path is None:
            source_path = ensure_source_proxy(
                source_cache_root, source_row, source_id, args.dry_run
            )
            source_paths[source_id] = source_path
        clip_path = (
            output_root
            / spec["participant_id"]
            / source_id
            / f"{clip_id}_{int(spec['start_sec']):06d}_{int(spec['end_sec']):06d}.mp4"
        )
        if args.overwrite or not clip_path.is_file():
            cut_session(
                source_path,
                clip_path,
                spec["start_sec"],
                spec["duration_sec"],
                cut_args,
            )
        key = (
            f"{args.cos_prefix.strip('/')}/{spec['participant_id']}/{source_id}/{clip_path.name}"
        )
        signed_url = ""
        if args.upload:
            signed_url = upload_session(
                cos_client,
                cos,
                clip_path,
                key,
                args.url_expire_days,
                args.dry_run,
            )
        size_bytes = "" if args.dry_run or not clip_path.exists() else str(clip_path.stat().st_size)
        row = {
            **spec,
            "candidate_ids": json.dumps(spec["candidate_ids"], ensure_ascii=False),
            "local_path": str(clip_path),
            "bucket": cos.get("bucket", ""),
            "region": cos.get("region", ""),
            "key": key if args.upload else "",
            "size_bytes": size_bytes,
            "content_type": content_type_for(clip_path),
            "signed_url": signed_url,
        }
        output_rows.append(row)
        if args.delete_local_after_upload and signed_url and clip_path.exists():
            clip_path.unlink()

    write_csv(Path(args.output_url_csv), output_rows, URL_FIELDS)
    write_csv(Path(args.cleanup_csv), output_rows, URL_FIELDS)
    write_jsonl(Path(args.mapping_jsonl), mapping_records(review_items, mappings, specs))
    if args.delete_source_after and not args.dry_run:
        for source_path in source_paths.values():
            if source_path.exists() and source_path.is_relative_to(source_cache_root):
                source_path.unlink()
    print(
        f"Prepared clip_specs={len(specs)} review_items={len(review_items)} upload={args.upload}",
        flush=True,
    )


if __name__ == "__main__":
    main()
