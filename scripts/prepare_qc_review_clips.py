#!/usr/bin/env python3
"""Plan, cut, and optionally upload temporary evidence clips for disputed QC items."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_bailian_qc_batch import proxy_url, source_video_id_from_proxy_row
from scripts.run_epic_vpn_session_batch import (
    DEFAULT_REENCODE_PRESET,
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
DEFAULT_CUT_MODE = "reencode"
DEFAULT_MAX_CLIPS_PER_CANDIDATE = 16
DEFAULT_MIN_TAIL_SEC = 10.0


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


def index_unique_rows(rows, id_getter, label: str):
    indexed = {}
    for row in rows:
        record_id = str(id_getter(row) or "")
        if not record_id:
            raise ValueError(f"{label} row has no id")
        if record_id in indexed:
            raise ValueError(f"Duplicate {label} id: {record_id}")
        indexed[record_id] = row
    return indexed


def support_grid_indices(item: dict[str, Any], clip_sec: int) -> list[int]:
    record_id = str(
        item.get("record_id")
        or f"{item.get('source_video_id')}:{item.get('candidate_id')}"
    )
    support_ranges = item.get("support_ranges") or []
    if not support_ranges:
        raise ValueError(f"Review item has no support ranges: {record_id}")
    indices: set[int] = set()
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
        indices.update(range(first_index, last_index + 1))
    return sorted(indices)


def uniformly_sample_indices(indices: list[int], limit: int) -> list[int]:
    if len(indices) <= limit:
        return indices
    if limit == 1:
        return [indices[len(indices) // 2]]
    positions = [
        round(sample_index * (len(indices) - 1) / (limit - 1))
        for sample_index in range(limit)
    ]
    return [indices[position] for position in positions]


def build_clip_specs(
    review_items: list[dict[str, Any]],
    clip_sec: int = 30,
    max_clips_per_candidate: int = DEFAULT_MAX_CLIPS_PER_CANDIDATE,
    source_durations: dict[str, float] | None = None,
    min_tail_sec: float = DEFAULT_MIN_TAIL_SEC,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    if clip_sec <= 0:
        raise ValueError("clip_sec must be > 0")
    if max_clips_per_candidate <= 0:
        raise ValueError("max_clips_per_candidate must be > 0")
    if min_tail_sec < 0:
        raise ValueError("min_tail_sec must be >= 0")
    source_durations = source_durations or {}
    specs: dict[tuple[str, int], dict[str, Any]] = {}
    mappings: dict[str, list[str]] = {}
    for item in review_items:
        source_video_id = str(item.get("source_video_id") or "")
        candidate_id = str(item.get("candidate_id") or "")
        if not source_video_id or not candidate_id:
            raise ValueError("Review item requires source_video_id and candidate_id")
        record_id = str(item.get("record_id") or f"{source_video_id}:{candidate_id}")
        clip_ids: list[str] = []
        available_indices = support_grid_indices(item, clip_sec)
        selected_indices = uniformly_sample_indices(
            available_indices, max_clips_per_candidate
        )
        for clip_index in selected_indices:
            clip_id = f"{source_video_id}_qc_s{clip_index:05d}"
            key = (source_video_id, clip_index)
            if key not in specs:
                start_sec = float(clip_index * clip_sec)
                end_sec = float((clip_index + 1) * clip_sec)
                source_duration = source_durations.get(source_video_id)
                if source_duration is not None and end_sec > source_duration:
                    end_sec = source_duration
                    if end_sec - start_sec < min_tail_sec:
                        start_sec = max(0.0, end_sec - clip_sec)
                if end_sec <= start_sec:
                    raise ValueError(
                        f"Review clip starts beyond source duration: {clip_id}"
                    )
                specs[key] = {
                    "clip_id": clip_id,
                    "participant_id": str(
                        item.get("participant_id") or source_video_id.split("_", 1)[0]
                    ),
                    "source_video_id": source_video_id,
                    "clip_index": clip_index,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "duration_sec": end_sec - start_sec,
                    "candidate_ids": [],
                }
            if candidate_id not in specs[key]["candidate_ids"]:
                specs[key]["candidate_ids"].append(candidate_id)
            clip_ids.append(clip_id)
        mappings[record_id] = sorted(clip_ids)
    ordered = sorted(specs.values(), key=lambda item: (item["source_video_id"], item["clip_index"]))
    for spec in ordered:
        spec["candidate_ids"] = sorted(spec["candidate_ids"])
    return ordered, mappings


def load_source_durations(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    durations: dict[str, float] = {}
    for record in read_jsonl(path):
        source_id = str(
            record.get("source_video_id")
            or record.get("session_id")
            or record.get("record_id")
            or ""
        )
        if not source_id:
            raise ValueError("Source metadata record has no source video id")
        if source_id in durations:
            raise ValueError(f"Duplicate source duration: {source_id}")
        try:
            duration = float(record["duration_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid source duration: {source_id}") from exc
        if duration <= 0:
            raise ValueError(f"Invalid source duration: {source_id}")
        durations[source_id] = duration
    return durations


def source_cache_path(source_cache_root: Path, row: dict[str, str], source_id: str) -> Path:
    participant_id = row.get("participant_id") or source_id.split("_", 1)[0]
    return source_cache_root / participant_id / f"{source_id}_540p16.mp4"


def ensure_source_proxy(
    source_cache_root: Path,
    row: dict[str, str],
    source_id: str,
    dry_run: bool,
) -> tuple[Path, bool]:
    local_value = row.get("local_path")
    if local_value and Path(local_value).is_file():
        return Path(local_value), False
    cache_path = source_cache_path(source_cache_root, row, source_id)
    if cache_path.is_file():
        return cache_path, False
    signed_url = proxy_url(row)
    if not signed_url:
        raise ValueError(f"Missing source proxy signed URL: {source_id}")
    print(f"Downloading source proxy: {source_id} -> {cache_path}", flush=True)
    if dry_run:
        return cache_path, True
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".part")
    with urllib.request.urlopen(signed_url, timeout=3600) as response, temp_path.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    temp_path.replace(cache_path)
    return cache_path, True


def delete_downloaded_sources(paths: set[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def release_source_after_spec(
    source_id: str,
    remaining_specs: dict[str, int],
    source_paths: dict[str, Path],
    downloaded_sources: set[Path],
    delete_source_after: bool,
    dry_run: bool,
) -> bool:
    """Release a run-downloaded proxy as soon as its final clip is processed."""

    remaining_specs[source_id] -= 1
    source_finished = remaining_specs[source_id] == 0
    if not source_finished or not delete_source_after or dry_run:
        return source_finished
    source_path = source_paths.pop(source_id, None)
    if source_path in downloaded_sources:
        delete_downloaded_sources({source_path})
        downloaded_sources.discard(source_path)
    return source_finished


def mapping_records(
    review_items: list[dict[str, Any]],
    mappings: dict[str, list[str]],
    specs: list[dict[str, Any]],
    clip_sec: int = 30,
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
        available_count = len(support_grid_indices(item, clip_sec))
        is_exhaustive = len(clip_ids) == available_count
        record["clip_selection"] = {
            "strategy": "all_support_grid_clips" if is_exhaustive else "uniform_grid_cap",
            "is_exhaustive": is_exhaustive,
            "available_clip_count": available_count,
            "selected_clip_count": len(clip_ids),
        }
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
    parser.add_argument("--source-metadata-jsonl")
    parser.add_argument("--clip-sec", type=int, default=30)
    parser.add_argument("--min-tail-sec", type=float, default=DEFAULT_MIN_TAIL_SEC)
    parser.add_argument(
        "--max-clips-per-candidate",
        type=int,
        default=DEFAULT_MAX_CLIPS_PER_CANDIDATE,
    )
    parser.add_argument("--ffmpeg-bin")
    parser.add_argument(
        "--cut-mode", choices=["copy", "reencode"], default=DEFAULT_CUT_MODE
    )
    parser.add_argument("--reencode-preset", default=DEFAULT_REENCODE_PRESET)
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
    specs, mappings = build_clip_specs(
        review_items,
        args.clip_sec,
        args.max_clips_per_candidate,
        load_source_durations(
            Path(args.source_metadata_jsonl) if args.source_metadata_jsonl else None
        ),
        args.min_tail_sec,
    )
    proxy_rows = index_unique_rows(
        read_csv(Path(args.proxy_url_csv)),
        id_getter=source_video_id_from_proxy_row,
        label="source proxy",
    )
    existing_rows = index_unique_rows(
        read_csv(Path(args.output_url_csv)),
        id_getter=lambda row: row.get("clip_id"),
        label="existing review clip",
    )
    output_root = Path(args.output_root)
    source_cache_root = Path(args.source_cache_root)
    ffmpeg_bin = resolve_binary("ffmpeg", args.ffmpeg_bin, "FFMPEG_BIN", dry_run=args.dry_run)
    cut_args = SimpleNamespace(
        ffmpeg_bin=ffmpeg_bin,
        cut_mode=args.cut_mode,
        reencode_preset=args.reencode_preset,
        ffmpeg_threads=args.ffmpeg_threads,
        dry_run=args.dry_run,
    )
    cos_client = None
    cos: dict[str, str] = {}
    if args.upload:
        cos_client, cos = make_cos_client(Path(args.cos_config).expanduser())

    source_paths: dict[str, Path] = {}
    downloaded_sources: set[Path] = set()
    output_rows: list[dict[str, Any]] = []
    remaining_specs = Counter(str(spec["source_video_id"]) for spec in specs)
    try:
        for spec in specs:
            clip_id = spec["clip_id"]
            source_id = spec["source_video_id"]
            existing = existing_rows.get(clip_id)
            if existing and existing.get("signed_url") and not args.overwrite:
                output_rows.append(existing)
                release_source_after_spec(
                    source_id,
                    remaining_specs,
                    source_paths,
                    downloaded_sources,
                    args.delete_source_after,
                    args.dry_run,
                )
                continue
            source_row = proxy_rows.get(source_id)
            if source_row is None:
                raise ValueError(f"Missing source proxy row: {source_id}")
            source_path = source_paths.get(source_id)
            if source_path is None:
                source_path, downloaded_by_run = ensure_source_proxy(
                    source_cache_root, source_row, source_id, args.dry_run
                )
                source_paths[source_id] = source_path
                if downloaded_by_run:
                    downloaded_sources.add(source_path)
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
            release_source_after_spec(
                source_id,
                remaining_specs,
                source_paths,
                downloaded_sources,
                args.delete_source_after,
                args.dry_run,
            )
    finally:
        if args.delete_source_after and not args.dry_run:
            delete_downloaded_sources(downloaded_sources)

    write_csv(Path(args.output_url_csv), output_rows, URL_FIELDS)
    write_csv(Path(args.cleanup_csv), output_rows, URL_FIELDS)
    mapping_output = mapping_records(review_items, mappings, specs, args.clip_sec)
    write_jsonl(Path(args.mapping_jsonl), mapping_output)
    print(
        f"Prepared clip_specs={len(specs)} review_items={len(review_items)} "
        f"sampled={sum(not item['clip_selection']['is_exhaustive'] for item in mapping_output)} "
        f"upload={args.upload}",
        flush=True,
    )


if __name__ == "__main__":
    main()
