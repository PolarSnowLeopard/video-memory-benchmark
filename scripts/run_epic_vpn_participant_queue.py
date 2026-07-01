#!/usr/bin/env python3
"""Run EPIC-KITCHENS participant batches with bounded parallelism on vpn."""

from __future__ import annotations

import argparse
import csv
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data/processed/epic_kitchens_100"
VIDEO_SUMMARY = PROCESSED / "video_summary.csv"
CANDIDATE_VIDEOS = PROCESSED / "candidate_videos.csv"
CANDIDATE_PARTICIPANTS = PROCESSED / "candidate_participants.csv"
PARTICIPANT_SUMMARY = PROCESSED / "participant_summary.csv"

MANIFEST_FIELDS = [
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

QUEUE_STATUS_FIELDS = [
    "updated_at",
    "participant_id",
    "status",
    "started_at",
    "finished_at",
    "returncode",
    "selected_videos",
    "manifest",
    "log_path",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    rows = [old for old in rows if old.get(key_field) != row.get(key_field)]
    rows.append({field: row.get(field, "") for field in fieldnames})
    write_csv(path, rows, fieldnames)


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    if value == "all":
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


def video_sequence(video_id: str) -> int:
    return int(video_id.split("_", 1)[1])


def suggested_use(summary_row: dict[str, str]) -> str:
    duration_min = float(summary_row["duration_min"])
    labelled_actions = int(summary_row["labelled_actions"])
    if 4 <= duration_min <= 12 and labelled_actions >= 60:
        return "direct_session"
    if duration_min > 12 and labelled_actions >= 120:
        return "cut_into_sessions"
    if labelled_actions >= 60:
        return "short_support_session"
    return "low_priority"


def participant_order(value: str) -> list[str]:
    if value == "all":
        return [row["participant_id"] for row in read_csv(PARTICIPANT_SUMMARY)]
    if value == "all-candidates":
        return [row["participant_id"] for row in read_csv(CANDIDATE_PARTICIPANTS)]
    return [item.strip() for item in value.split(",") if item.strip()]


def candidate_manifest_rows(participant_id: str, uses: set[str] | None) -> list[dict[str, str]]:
    summaries = {row["video_id"]: row for row in read_csv(VIDEO_SUMMARY)}
    rows: list[dict[str, str]] = []
    for row in read_csv(CANDIDATE_VIDEOS):
        if row["participant_id"] != participant_id:
            continue
        if uses is not None and row["suggested_use"] not in uses:
            continue
        summary = summaries[row["video_id"]]
        rows.append(
            {
                "priority": "",
                "participant_id": row["participant_id"],
                "video_id": row["video_id"],
                "video_sequence": row["video_sequence"],
                "duration_sec": summary["duration_sec"],
                "duration_min": row["duration_min"],
                "fps": summary["fps"],
                "resolution": summary["resolution"],
                "source_subset": source_subset(row["video_id"]),
                "source_split": source_split(summary),
                "labelled_actions": row["labelled_actions"],
                "actions_per_min": row["actions_per_min"],
                "suggested_use": row["suggested_use"],
            }
        )
    rows.sort(key=lambda row: video_sequence(row["video_id"]))
    return rows


def all_video_manifest_rows(participant_id: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in read_csv(VIDEO_SUMMARY):
        if row["participant_id"] != participant_id:
            continue
        rows.append(
            {
                "priority": "",
                "participant_id": row["participant_id"],
                "video_id": row["video_id"],
                "video_sequence": str(video_sequence(row["video_id"])),
                "duration_sec": row["duration_sec"],
                "duration_min": row["duration_min"],
                "fps": row["fps"],
                "resolution": row["resolution"],
                "source_subset": source_subset(row["video_id"]),
                "source_split": source_split(row),
                "labelled_actions": row["labelled_actions"],
                "actions_per_min": row["actions_per_min"],
                "suggested_use": suggested_use(row),
            }
        )
    rows.sort(key=lambda row: video_sequence(row["video_id"]))
    return rows


def build_manifest(participant_id: str, args: argparse.Namespace) -> tuple[Path, int]:
    uses = None if args.uses == "all" else parse_list(args.uses)
    if args.selection == "candidates":
        rows = candidate_manifest_rows(participant_id, uses)
        suffix = "candidates"
    else:
        rows = all_video_manifest_rows(participant_id)
        suffix = "all_videos"

    for idx, row in enumerate(rows, start=1):
        row["priority"] = str(idx)

    manifest_path = args.manifest_dir / f"{participant_id.lower()}_{suffix}.csv"
    write_csv(manifest_path, rows, MANIFEST_FIELDS)
    return manifest_path, len(rows)


def build_batch_cmd(manifest: Path, args: argparse.Namespace) -> list[str]:
    cmd = [
        args.python,
        str(args.batch_script),
        "--manifest",
        str(manifest),
        "--data-root",
        str(args.data_root),
        "--downloader-dir",
        str(args.downloader_dir),
        "--python",
        args.python,
        "--cos-prefix",
        args.cos_prefix,
        "--url-expire-days",
        str(args.url_expire_days),
        "--ffmpeg-threads",
        str(args.ffmpeg_threads),
    ]
    if not args.keep_raw:
        cmd.append("--delete-raw-after-upload")
    if args.delete_proxy_after_upload:
        cmd.append("--delete-proxy-after-upload")
    if args.skip_upload:
        cmd.append("--skip-upload")
    if args.rerun_completed:
        cmd.append("--rerun-completed")
    return cmd


def terminate_active(active: dict[str, dict[str, object]]) -> None:
    for item in active.values():
        proc = item["proc"]
        assert isinstance(proc, subprocess.Popen)
        if proc.poll() is None:
            proc.terminate()
    time.sleep(5)
    for item in active.values():
        proc = item["proc"]
        assert isinstance(proc, subprocess.Popen)
        if proc.poll() is None:
            proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--participants", default="all-candidates", help="Comma-separated ids, all-candidates, or all.")
    parser.add_argument("--selection", choices=["candidates", "all-videos"], default="candidates")
    parser.add_argument("--uses", default="direct_session,cut_into_sessions", help="Only used with --selection candidates; use all to keep all candidate uses.")
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--data-root", default="/home/lighthouse/video-benchmark/data")
    parser.add_argument("--downloader-dir", default="/home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100")
    parser.add_argument("--batch-script", default=str(ROOT / "scripts/run_epic_vpn_batch.py"))
    parser.add_argument("--manifest-dir", default=str(PROCESSED / "manifests/queue"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--cos-prefix", default="video-benchmark/epic-kitchens")
    parser.add_argument("--url-expire-days", type=int, default=30)
    parser.add_argument("--ffmpeg-threads", type=int, default=2, help="Threads per ffmpeg worker. Default is 2 for five-way vpn runs.")
    parser.add_argument("--run-name", default=f"participant_queue_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    parser.add_argument("--keep-raw", action="store_true", help="Keep original MP4 files after upload. Default deletes raw files after upload.")
    parser.add_argument("--delete-proxy-after-upload", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--rerun-completed", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.max_workers < 1:
        raise SystemExit("--max-workers must be >= 1")
    if args.ffmpeg_threads < 0:
        raise SystemExit("--ffmpeg-threads must be >= 0")

    args.data_root = Path(args.data_root)
    args.downloader_dir = Path(args.downloader_dir)
    args.batch_script = Path(args.batch_script)
    args.manifest_dir = Path(args.manifest_dir)

    queue_status = args.data_root / "processed/epic_pipeline_runs" / f"{args.run_name}_status.csv"
    log_dir = args.data_root / "processed/epic_pipeline_runs" / f"{args.run_name}_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    args.manifest_dir.mkdir(parents=True, exist_ok=True)

    participants = participant_order(args.participants)
    pending: list[dict[str, object]] = []
    for participant_id in participants:
        manifest, count = build_manifest(participant_id, args)
        row = {
            "updated_at": utc_now(),
            "participant_id": participant_id,
            "status": "queued" if count else "skipped_empty_manifest",
            "started_at": "",
            "finished_at": utc_now() if not count else "",
            "returncode": "",
            "selected_videos": str(count),
            "manifest": str(manifest),
            "log_path": "",
        }
        upsert_csv(queue_status, row, QUEUE_STATUS_FIELDS, "participant_id")
        if count:
            pending.append({"participant_id": participant_id, "manifest": manifest, "selected_videos": count})

    print(f"Queue status: {queue_status}", flush=True)
    print(f"Log dir:      {log_dir}", flush=True)
    print(f"Participants queued: {len(pending)}", flush=True)
    print(f"Max workers: {args.max_workers}", flush=True)
    print(f"Selection:   {args.selection}", flush=True)
    if not args.keep_raw:
        print("Raw cleanup: enabled after successful upload", flush=True)

    if args.dry_run:
        for item in pending:
            manifest = item["manifest"]
            assert isinstance(manifest, Path)
            print(" ".join(build_batch_cmd(manifest, args)), flush=True)
        return

    active: dict[str, dict[str, object]] = {}

    def handle_signal(signum, _frame) -> None:
        print(f"Received signal {signum}; terminating active workers.", flush=True)
        terminate_active(active)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while pending or active:
        while pending and len(active) < args.max_workers:
            item = pending.pop(0)
            participant_id = str(item["participant_id"])
            manifest = item["manifest"]
            selected_videos = str(item["selected_videos"])
            assert isinstance(manifest, Path)
            log_path = log_dir / f"{participant_id}.log"
            log_file = log_path.open("a", encoding="utf-8")
            started_at = utc_now()
            cmd = build_batch_cmd(manifest, args)
            print(f"Starting {participant_id}: {' '.join(cmd)}", flush=True)
            log_file.write(f"[{started_at}] Starting {' '.join(cmd)}\n")
            log_file.flush()
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT)
            active[participant_id] = {
                "proc": proc,
                "log_file": log_file,
                "started_at": started_at,
                "log_path": log_path,
                "manifest": manifest,
                "selected_videos": selected_videos,
            }
            upsert_csv(
                queue_status,
                {
                    "updated_at": utc_now(),
                    "participant_id": participant_id,
                    "status": "running",
                    "started_at": started_at,
                    "finished_at": "",
                    "returncode": "",
                    "selected_videos": selected_videos,
                    "manifest": str(manifest),
                    "log_path": str(log_path),
                },
                QUEUE_STATUS_FIELDS,
                "participant_id",
            )

        finished: list[str] = []
        for participant_id, item in active.items():
            proc = item["proc"]
            assert isinstance(proc, subprocess.Popen)
            rc = proc.poll()
            if rc is None:
                continue
            log_file = item["log_file"]
            assert hasattr(log_file, "close")
            log_file.write(f"[{utc_now()}] Finished with return code {rc}\n")
            log_file.close()
            status = "ok" if rc == 0 else "error"
            print(f"Finished {participant_id}: {status} ({rc})", flush=True)
            upsert_csv(
                queue_status,
                {
                    "updated_at": utc_now(),
                    "participant_id": participant_id,
                    "status": status,
                    "started_at": str(item["started_at"]),
                    "finished_at": utc_now(),
                    "returncode": str(rc),
                    "selected_videos": str(item["selected_videos"]),
                    "manifest": str(item["manifest"]),
                    "log_path": str(item["log_path"]),
                },
                QUEUE_STATUS_FIELDS,
                "participant_id",
            )
            finished.append(participant_id)
        for participant_id in finished:
            active.pop(participant_id)
        if pending or active:
            time.sleep(args.poll_seconds)

    print(f"Done. Queue status: {queue_status}", flush=True)


if __name__ == "__main__":
    main()
