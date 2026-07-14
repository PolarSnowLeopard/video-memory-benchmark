#!/usr/bin/env python3
"""Select one deterministic midpoint session from every source video."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def select_midpoint_session_ids(rows: list[dict[str, str]]) -> list[str]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_session_ids: set[str] = set()
    for row in rows:
        source_id = row.get("source_video_id") or row.get("video_id") or ""
        session_id = row.get("session_id") or row.get("record_id") or ""
        if not source_id or not session_id:
            raise ValueError("Every row must contain source_video_id/video_id and session_id")
        if session_id in seen_session_ids:
            raise ValueError(f"Duplicate session id: {session_id}")
        seen_session_ids.add(session_id)
        grouped[source_id].append(row)

    selected: list[str] = []
    for source_id in sorted(grouped):
        source_rows = sorted(
            grouped[source_id],
            key=lambda row: (
                float(row.get("start_sec") or 0),
                int(row.get("session_index") or 0),
                row.get("session_id") or "",
            ),
        )
        midpoint = source_rows[len(source_rows) // 2]
        selected.append(midpoint.get("session_id") or midpoint["record_id"])
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-csv", required=True)
    parser.add_argument("--format", choices=("csv", "lines"), default="csv")
    args = parser.parse_args()

    selected = select_midpoint_session_ids(read_csv(Path(args.session_csv)))
    if args.format == "lines":
        print("\n".join(selected))
    else:
        print(",".join(selected))


if __name__ == "__main__":
    main()
