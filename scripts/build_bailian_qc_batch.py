#!/usr/bin/env python3
"""Build OpenAI-compatible Bailian Batch JSONL for video evidence QC."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            if len(line.encode("utf-8")) > 6 * 1024 * 1024:
                raise ValueError(f"Batch line exceeds 6 MB: {record.get('custom_id') or record.get('source_video_id')}")
            handle.write(line + "\n")


def source_id(record: dict[str, Any]) -> str:
    return str(
        record.get("source_video_id")
        or record.get("session_id")
        or record.get("record_id")
        or record.get("video_id")
        or ""
    )


def source_video_id_from_proxy_row(row: dict[str, str]) -> str:
    for field in ("source_video_id", "video_id", "record_id"):
        if row.get(field):
            return row[field]
    value = row.get("key") or row.get("local_path") or row.get("signed_url") or row.get("url") or ""
    path = unquote(urlparse(value).path) if "://" in value else value
    stem = Path(path).stem
    for suffix in ("_540p16", "_540p"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def proxy_url(row: dict[str, str]) -> str:
    for field in ("signed_url", "video_url", "proxy_url", "url"):
        if row.get(field):
            return row[field]
    return ""


def unique_by_source(records: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        value = source_id(record)
        if not value:
            raise ValueError(f"{label} record has no source video id")
        if value in result:
            raise ValueError(f"Duplicate {label} source video id: {value}")
        result[value] = record
    return result


def build_source_requests(
    session_records: list[dict[str, Any]],
    session_inputs: list[dict[str, Any]],
    proxy_rows: list[dict[str, str]],
    prompt: str,
    model: str,
    fps: float,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not 0.1 <= fps <= 10:
        raise ValueError("fps must be between 0.1 and 10")
    sessions = unique_by_source(session_records, "session")
    inputs = unique_by_source(session_inputs, "session input")
    proxies: dict[str, dict[str, str]] = {}
    for row in proxy_rows:
        value = source_video_id_from_proxy_row(row)
        if value:
            if value in proxies:
                raise ValueError(f"Duplicate proxy source video id: {value}")
            proxies[value] = row

    requests: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for current_source, session in sorted(sessions.items()):
        session_input = inputs.get(current_source)
        if session_input is None:
            raise ValueError(f"Missing session input for {current_source}")
        current_proxy = proxies.get(current_source)
        signed_url = proxy_url(current_proxy or {})
        if not signed_url:
            raise ValueError(f"Missing signed URL for {current_source}")
        window_ranges = {
            str(item.get("window_id") or ""): {
                "start_sec": float(item.get("start_sec") or 0),
                "end_sec": float(item.get("end_sec") or 0),
            }
            for item in session_input.get("window_ranges") or []
            if item.get("window_id")
        }
        candidates: list[dict[str, Any]] = []
        manifest_candidates: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()
        for candidate in session.get("cross_session_evidence_candidates") or []:
            if candidate.get("qc_status") == "schema_failed":
                continue
            candidate_id = str(candidate.get("candidate_id") or "")
            if not candidate_id:
                raise ValueError(f"Candidate without candidate_id in {current_source}")
            if candidate_id in seen_candidate_ids:
                raise ValueError(f"Duplicate candidate id in {current_source}: {candidate_id}")
            seen_candidate_ids.add(candidate_id)
            support_window_ids = [str(value) for value in candidate.get("supporting_window_ids") or []]
            support_ranges: list[dict[str, float]] = []
            for window_id in support_window_ids:
                if window_id not in window_ranges:
                    raise ValueError(
                        f"Candidate {candidate_id} references unknown window {window_id} in {current_source}"
                    )
                support_ranges.append(window_ranges[window_id])
            if not support_ranges:
                raise ValueError(f"Candidate {candidate_id} has no support ranges in {current_source}")
            payload_candidate = {
                "candidate_id": candidate_id,
                "type": candidate.get("type"),
                "claim": candidate.get("claim"),
                "observed_value": candidate.get("observed_value"),
                "supporting_window_ids": support_window_ids,
                "support_ranges": support_ranges,
                "extractor_confidence": candidate.get("normalized_confidence")
                or candidate.get("confidence"),
                "quality_flags": list(candidate.get("quality_flags") or []),
            }
            candidates.append(payload_candidate)
            manifest_candidates.append(
                {
                    "candidate_id": candidate_id,
                    "supporting_window_ids": support_window_ids,
                    "support_ranges": support_ranges,
                    "quality_flags": list(candidate.get("quality_flags") or []),
                }
            )
        if not candidates:
            continue
        input_payload = {
            "source_video_id": current_source,
            "participant_id": session.get("participant_id"),
            "candidates": candidates,
        }
        prompt_text = (
            prompt.rstrip()
            + "\n\n输入 JSON：\n"
            + json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"))
        )
        requests.append(
            {
                "custom_id": current_source,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": model,
                    "enable_thinking": False,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "video_url",
                                    "video_url": {"url": signed_url, "fps": fps},
                                },
                                {"type": "text", "text": prompt_text},
                            ],
                        }
                    ],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                },
            }
        )
        manifests.append(
            {
                "custom_id": current_source,
                "source_video_id": current_source,
                "participant_id": session.get("participant_id"),
                "model": model,
                "fps": fps,
                "video_url_sha256": hashlib.sha256(signed_url.encode("utf-8")).hexdigest(),
                "candidate_ids": [item["candidate_id"] for item in manifest_candidates],
                "candidates": manifest_candidates,
            }
        )
    return requests, manifests


def build_local_requests(
    review_items: list[dict[str, Any]],
    clip_rows: list[dict[str, str]],
    prompt: str,
    model: str,
    fps: float,
    max_tokens: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not 0.1 <= fps <= 10:
        raise ValueError("fps must be between 0.1 and 10")
    clips: dict[str, dict[str, str]] = {}
    for row in clip_rows:
        clip_id = str(row.get("clip_id") or row.get("session_id") or "")
        if not clip_id:
            raise ValueError("Clip row has no clip_id")
        if clip_id in clips:
            raise ValueError(f"Duplicate clip id: {clip_id}")
        clips[clip_id] = row

    requests: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    for item in review_items:
        source_video_id = str(item.get("source_video_id") or "")
        candidate_id = str(item.get("candidate_id") or "")
        record_id = str(item.get("record_id") or f"{source_video_id}:{candidate_id}")
        if not source_video_id or not candidate_id:
            raise ValueError("Local review item requires source_video_id and candidate_id")
        if record_id in seen_record_ids:
            raise ValueError(f"Duplicate local review record id: {record_id}")
        seen_record_ids.add(record_id)
        clip_ids = [str(value) for value in item.get("clip_ids") or []]
        if not clip_ids:
            raise ValueError(f"Local review item has no clip_ids: {record_id}")
        selected_clips: list[dict[str, Any]] = []
        for clip_id in clip_ids:
            row = clips.get(clip_id)
            if row is None:
                raise ValueError(f"Local review item references unknown clip: {clip_id}")
            if str(row.get("source_video_id") or "") != source_video_id:
                raise ValueError(f"Local review clip source mismatch: {clip_id}")
            signed_url = proxy_url(row)
            if not signed_url:
                raise ValueError(f"Missing signed URL for local review clip: {clip_id}")
            selected_clips.append(
                {
                    "clip_id": clip_id,
                    "start_sec": float(row.get("start_sec") or 0),
                    "end_sec": float(row.get("end_sec") or 0),
                    "signed_url": signed_url,
                }
            )
        selected_clips.sort(key=lambda value: (value["start_sec"], value["clip_id"]))
        input_payload = {
            "source_video_id": source_video_id,
            "candidate": {
                "candidate_id": candidate_id,
                "type": item.get("candidate_type") or item.get("type"),
                "claim": item.get("claim"),
                "observed_value": item.get("observed_value"),
                "support_ranges": list(item.get("support_ranges") or []),
                "quality_flags": list(item.get("quality_flags") or []),
                "first_pass_verdict": item.get("first_pass_verdict"),
            },
            "clip_ranges": [
                {
                    "clip_id": clip["clip_id"],
                    "start_sec": clip["start_sec"],
                    "end_sec": clip["end_sec"],
                }
                for clip in selected_clips
            ],
        }
        prompt_text = (
            prompt.rstrip()
            + "\n\n输入 JSON：\n"
            + json.dumps(input_payload, ensure_ascii=False, separators=(",", ":"))
        )
        content: list[dict[str, Any]] = [
            {
                "type": "video_url",
                "video_url": {"url": clip["signed_url"], "fps": fps},
            }
            for clip in selected_clips
        ]
        content.append({"type": "text", "text": prompt_text})
        requests.append(
            {
                "custom_id": record_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": model,
                    "enable_thinking": False,
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0,
                    "max_tokens": max_tokens,
                },
            }
        )
        manifests.append(
            {
                "custom_id": record_id,
                "record_id": record_id,
                "source_video_id": source_video_id,
                "participant_id": item.get("participant_id"),
                "candidate_id": candidate_id,
                "model": model,
                "fps": fps,
                "clip_ids": [clip["clip_id"] for clip in selected_clips],
                "clip_ranges": input_payload["clip_ranges"],
                "support_ranges": list(item.get("support_ranges") or []),
                "quality_flags": list(item.get("quality_flags") or []),
                "video_url_sha256": [
                    hashlib.sha256(clip["signed_url"].encode("utf-8")).hexdigest()
                    for clip in selected_clips
                ],
            }
        )
    return requests, manifests


def load_session_records(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        return [
            json.loads(item.read_text(encoding="utf-8"))
            for item in sorted(path.glob("*.clean.json"))
        ]
    return read_jsonl(path)


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source", help="Build one full-video QC request per source video.")
    source.add_argument("--session-records", required=True, help="Directory of clean JSON files or JSONL.")
    source.add_argument("--session-input-jsonl", required=True)
    source.add_argument("--proxy-url-csv", required=True)
    source.add_argument("--prompt-file", default="prompts/video_candidate_verification_schema_zh.txt")
    source.add_argument("--output-jsonl", required=True)
    source.add_argument("--manifest-jsonl", required=True)
    source.add_argument("--model", default="qwen3.7-plus")
    source.add_argument("--fps", type=float, default=0.5)
    source.add_argument("--max-tokens", type=int, default=8192)
    source.add_argument("--video-ids", help="Comma-separated source video ids.")
    source.add_argument("--limit", type=int)
    local = subparsers.add_parser("local", help="Build one short-evidence QC request per candidate.")
    local.add_argument("--review-mapping-jsonl", required=True)
    local.add_argument("--clip-url-csv", required=True)
    local.add_argument(
        "--prompt-file", default="prompts/video_candidate_local_verification_schema_zh.txt"
    )
    local.add_argument("--output-jsonl", required=True)
    local.add_argument("--manifest-jsonl", required=True)
    local.add_argument("--model", default="qwen3.7-plus")
    local.add_argument("--fps", type=float, default=1.0)
    local.add_argument("--max-tokens", type=int, default=4096)
    local.add_argument("--record-ids", help="Comma-separated source:candidate record ids.")
    local.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.command == "source":
        sessions = load_session_records(Path(args.session_records))
        wanted = parse_list(args.video_ids)
        if wanted is not None:
            sessions = [record for record in sessions if source_id(record) in wanted]
        if args.limit is not None:
            sessions = sessions[: args.limit]
        requests, manifests = build_source_requests(
            sessions,
            read_jsonl(Path(args.session_input_jsonl)),
            read_csv(Path(args.proxy_url_csv)),
            Path(args.prompt_file).read_text(encoding="utf-8"),
            args.model,
            args.fps,
            args.max_tokens,
        )
        candidate_count = sum(len(item["candidate_ids"]) for item in manifests)
    else:
        review_items = read_jsonl(Path(args.review_mapping_jsonl))
        wanted = parse_list(args.record_ids)
        if wanted is not None:
            review_items = [
                item
                for item in review_items
                if str(
                    item.get("record_id")
                    or f"{item.get('source_video_id')}:{item.get('candidate_id')}"
                )
                in wanted
            ]
        if args.limit is not None:
            review_items = review_items[: args.limit]
        requests, manifests = build_local_requests(
            review_items,
            read_csv(Path(args.clip_url_csv)),
            Path(args.prompt_file).read_text(encoding="utf-8"),
            args.model,
            args.fps,
            args.max_tokens,
        )
        candidate_count = len(manifests)
    write_jsonl(Path(args.output_jsonl), requests)
    write_jsonl(Path(args.manifest_jsonl), manifests)
    print(
        f"Wrote {len(requests)} requests with {candidate_count} candidates -> {args.output_jsonl}",
        flush=True,
    )


if __name__ == "__main__":
    main()
