#!/usr/bin/env python3
"""Build a lightweight EPIC-KITCHENS processing manifest."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES = ROOT / "data/processed/epic_kitchens_100/candidate_videos.csv"
DEFAULT_VIDEO_SUMMARY = ROOT / "data/processed/epic_kitchens_100/video_summary.csv"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def source_subset(video_id: str) -> str:
    suffix = video_id.split("_", 1)[1]
    return "epic_100_extension" if len(suffix) == 3 else "epic_55_original"


def source_split(summary_row: dict[str, str]) -> str:
    if int(summary_row.get("train_actions", "0") or "0") > 0:
        return "train"
    if int(summary_row.get("validation_actions", "0") or "0") > 0:
        return "validation"
    if int(summary_row.get("test_timestamps", "0") or "0") > 0:
        return "test"
    return "unknown"


def priority_rank(use: str) -> int:
    ranks = {
        "direct_session": 0,
        "cut_into_sessions": 1,
        "short_support_session": 2,
        "low_priority": 3,
    }
    return ranks.get(use, 9)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--video-summary", default=str(DEFAULT_VIDEO_SUMMARY))
    parser.add_argument("--participants", help="Comma-separated participant ids, e.g. P04,P02")
    parser.add_argument("--video-ids", help="Comma-separated explicit video ids. Overrides participant filtering.")
    parser.add_argument(
        "--uses",
        default="direct_session,cut_into_sessions",
        help="Comma-separated suggested_use values to include.",
    )
    parser.add_argument("--limit", type=int, help="Maximum number of rows after sorting")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    candidates = read_rows(Path(args.candidates))
    summaries = {row["video_id"]: row for row in read_rows(Path(args.video_summary))}
    participants = parse_list(args.participants)
    video_ids = parse_list(args.video_ids)
    uses = parse_list(args.uses)

    selected: list[dict[str, str]] = []
    for row in candidates:
        video_id = row["video_id"]
        if video_ids is not None and video_id not in video_ids:
            continue
        if video_ids is None and participants is not None and row["participant_id"] not in participants:
            continue
        if uses is not None and row["suggested_use"] not in uses:
            continue

        summary = summaries.get(video_id, {})
        out = {
            "priority": "",
            "participant_id": row["participant_id"],
            "video_id": video_id,
            "video_sequence": row["video_sequence"],
            "duration_sec": summary.get("duration_sec", ""),
            "duration_min": row["duration_min"],
            "fps": summary.get("fps", ""),
            "resolution": summary.get("resolution", ""),
            "source_subset": source_subset(video_id),
            "source_split": source_split(summary),
            "labelled_actions": row["labelled_actions"],
            "actions_per_min": row["actions_per_min"],
            "suggested_use": row["suggested_use"],
        }
        selected.append(out)

    selected.sort(
        key=lambda r: (
            r["participant_id"],
            priority_rank(r["suggested_use"]),
            int(r["video_sequence"]),
        )
    )
    if args.limit is not None:
        selected = selected[: args.limit]
    for idx, row in enumerate(selected, start=1):
        row["priority"] = str(idx)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "priority",
        "participant_id",
        "video_id",
        "video_sequence",
        "duration_sec",
        "duration_min",
        "fps",
        "resolution",
        "source_subset",
        "source_split",
        "labelled_actions",
        "actions_per_min",
        "suggested_use",
    ]
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(selected)
    print(f"Wrote {output} ({len(selected)} rows)")


if __name__ == "__main__":
    main()
