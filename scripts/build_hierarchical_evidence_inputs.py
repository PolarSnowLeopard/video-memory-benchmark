#!/usr/bin/env python3
"""Build JSONL inputs for hierarchical video evidence aggregation.

The first VLM pass runs on short video clips and writes one clean JSON file per
clip. This script groups those clip-level outputs into local windows, then
groups window-level outputs back into source-video/session records.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


MODEL_SELF_ASSESSMENT_FIELDS = {"confidence", "trackability"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def without_model_self_assessment(value: Any) -> Any:
    """Remove uncalibrated model self-assessments before the next LLM layer."""
    if isinstance(value, dict):
        return {
            key: without_model_self_assessment(child)
            for key, child in value.items()
            if key not in MODEL_SELF_ASSESSMENT_FIELDS
        }
    if isinstance(value, list):
        return [without_model_self_assessment(child) for child in value]
    return value


def source_video_id(row: dict[str, Any]) -> str:
    return str(row.get("source_video_id") or row.get("video_id") or row.get("record_id") or "")


def participant_id(row: dict[str, Any], source_id: str) -> str:
    return str(row.get("participant_id") or source_id.split("_", 1)[0])


def benchmark_order_metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw_order = row.get("benchmark_session_order")
    if raw_order is None or str(raw_order).strip() == "":
        return {}
    raw_eligible = row.get("benchmark_temporal_evolution_eligible")
    return {
        "benchmark_session_order": int(raw_order),
        "benchmark_order_status": str(row.get("benchmark_order_status") or ""),
        "benchmark_order_basis": str(row.get("benchmark_order_basis") or ""),
        "benchmark_temporal_evolution_eligible": (
            raw_eligible
            if isinstance(raw_eligible, bool)
            else str(raw_eligible).strip().casefold() == "true"
        ),
    }


def micro_clip_id(row: dict[str, str]) -> str:
    return row.get("session_id") or row.get("record_id") or row.get("clip_id") or ""


def load_clean_json(clean_dir: Path, record_id: str, allow_missing: bool) -> dict[str, Any] | None:
    path = clean_dir / f"{record_id}.clean.json"
    if not path.exists():
        if allow_missing:
            return None
        raise FileNotFoundError(f"Missing clean JSON for {record_id}: {path}")
    return read_json(path)


def build_window_records(
    micro_rows: list[dict[str, str]],
    micro_clean_dir: Path,
    window_sec: int,
    allow_missing: bool = False,
) -> list[dict[str, Any]]:
    if window_sec <= 0:
        raise ValueError("window_sec must be > 0")

    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in micro_rows:
        clip_id = micro_clip_id(row)
        if not clip_id:
            raise ValueError(f"Micro row has no session_id/record_id/clip_id: {row}")
        source_id = source_video_id(row)
        if not source_id:
            raise ValueError(f"Micro row has no source_video_id/video_id: {row}")
        start_sec = float(row.get("start_sec") or 0)
        end_sec = float(row.get("end_sec") or start_sec + float(row.get("duration_sec") or 0))
        evidence = load_clean_json(micro_clean_dir, clip_id, allow_missing)
        if evidence is None:
            continue
        window_index = int(start_sec // window_sec)
        grouped[(source_id, window_index)].append(
            {
                "clip_id": clip_id,
                "participant_id": participant_id(row, source_id),
                "source_video_id": source_id,
                **benchmark_order_metadata(row),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "duration_sec": end_sec - start_sec,
                "evidence": evidence,
            }
        )

    records: list[dict[str, Any]] = []
    for (source_id, window_index), clips in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        clips = sorted(clips, key=lambda item: (item["start_sec"], item["clip_id"]))
        participant = clips[0]["participant_id"]
        start_sec = min(clip["start_sec"] for clip in clips)
        end_sec = max(clip["end_sec"] for clip in clips)
        record_id = f"{source_id}_w{window_index:03d}"
        records.append(
            {
                "record_id": record_id,
                "window_id": record_id,
                "participant_id": participant,
                "source_video_id": source_id,
                **benchmark_order_metadata(clips[0]),
                "window_index": window_index,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "duration_sec": end_sec - start_sec,
                "micro_clip_ids": [clip["clip_id"] for clip in clips],
                "micro_clip_ranges": [
                    {
                        "clip_id": clip["clip_id"],
                        "start_sec": clip["start_sec"],
                        "end_sec": clip["end_sec"],
                    }
                    for clip in clips
                ],
                "upstream_model_self_assessment": "removed_unverified_fields",
                "micro_evidence": [
                    without_model_self_assessment(clip["evidence"])
                    for clip in clips
                ],
            }
        )
    return records


def build_session_records(
    window_records: list[dict[str, Any]],
    window_clean_dir: Path,
    allow_missing: bool = False,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in window_records:
        record_id = str(record.get("record_id") or record.get("window_id") or "")
        if not record_id:
            raise ValueError(f"Window record has no record_id/window_id: {record}")
        source_id = source_video_id(record)
        if not source_id:
            raise ValueError(f"Window record has no source_video_id/video_id: {record}")
        evidence = load_clean_json(window_clean_dir, record_id, allow_missing)
        if evidence is None:
            continue
        grouped[source_id].append(
            {
                "window_id": record_id,
                "participant_id": participant_id(record, source_id),
                "source_video_id": source_id,
                **benchmark_order_metadata(record),
                "window_index": int(record.get("window_index") or 0),
                "start_sec": float(record.get("start_sec") or 0),
                "end_sec": float(record.get("end_sec") or 0),
                "micro_clip_ids": list(record.get("micro_clip_ids") or []),
                "evidence": evidence,
            }
        )

    sessions: list[dict[str, Any]] = []
    for source_id, windows in sorted(grouped.items()):
        windows = sorted(windows, key=lambda item: (item["start_sec"], item["window_id"]))
        participant = windows[0]["participant_id"]
        start_sec = min(window["start_sec"] for window in windows)
        end_sec = max(window["end_sec"] for window in windows)
        sessions.append(
            {
                "record_id": source_id,
                "session_id": source_id,
                "participant_id": participant,
                "source_video_id": source_id,
                **benchmark_order_metadata(windows[0]),
                "start_sec": start_sec,
                "end_sec": end_sec,
                "duration_sec": end_sec - start_sec,
                "window_ids": [window["window_id"] for window in windows],
                "window_ranges": [
                    {
                        "window_id": window["window_id"],
                        "start_sec": window["start_sec"],
                        "end_sec": window["end_sec"],
                        "micro_clip_ids": window["micro_clip_ids"],
                    }
                    for window in windows
                ],
                "upstream_model_self_assessment": "removed_unverified_fields",
                "window_evidence": [
                    without_model_self_assessment(window["evidence"])
                    for window in windows
                ],
            }
        )
    return sessions


def add_common_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video-ids", help="Comma-separated source video ids to include.")
    parser.add_argument("--limit", type=int, help="Limit output records after grouping.")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    windows = subparsers.add_parser("windows", help="Build 2-minute window inputs from micro-clip clean JSON files.")
    windows.add_argument("--micro-url-csv", required=True)
    windows.add_argument("--micro-output-dir", required=True)
    windows.add_argument("--output-jsonl", required=True)
    windows.add_argument("--window-sec", type=int, default=120)
    windows.add_argument("--allow-missing", action="store_true")
    add_common_filter_args(windows)

    sessions = subparsers.add_parser("sessions", help="Build source-video/session inputs from window clean JSON files.")
    sessions.add_argument("--window-input-jsonl", required=True)
    sessions.add_argument("--window-output-dir", required=True)
    sessions.add_argument("--output-jsonl", required=True)
    sessions.add_argument("--allow-missing", action="store_true")
    add_common_filter_args(sessions)

    args = parser.parse_args()
    wanted = parse_list(args.video_ids)

    if args.command == "windows":
        rows = read_csv(Path(args.micro_url_csv))
        if wanted is not None:
            rows = [row for row in rows if source_video_id(row) in wanted]
        records = build_window_records(
            rows,
            Path(args.micro_output_dir),
            window_sec=args.window_sec,
            allow_missing=args.allow_missing,
        )
    else:
        records = read_jsonl(Path(args.window_input_jsonl))
        if wanted is not None:
            records = [record for record in records if source_video_id(record) in wanted]
        records = build_session_records(
            records,
            Path(args.window_output_dir),
            allow_missing=args.allow_missing,
        )

    if args.limit is not None:
        records = records[: args.limit]
    write_jsonl(Path(args.output_jsonl), records)
    print(f"Wrote {len(records)} records -> {args.output_jsonl}", flush=True)


if __name__ == "__main__":
    main()
