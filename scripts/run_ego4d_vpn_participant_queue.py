#!/usr/bin/env python3
"""Run bounded parallel Ego4D participant batches on the vpn host."""

from __future__ import annotations

import argparse
import csv
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

QUEUE_STATUS_FIELDS = [
    "updated_at",
    "participant_id",
    "status",
    "started_at",
    "finished_at",
    "returncode",
    "selected_videos",
    "uploaded_videos",
    "manifest",
    "url_csv",
    "log_path",
]

SOURCE_AUTH_CONTEXT_MARKERS = (
    "botocore.exceptions",
    "boto3",
    "s3transfer",
)

SOURCE_AUTH_ERROR_MARKERS = (
    "when calling the headobject operation: forbidden",
    "when calling the headobject operation: accessdenied",
    "expiredtoken",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
    "unrecognizedclientexception",
    "the security token included in the request is expired",
    "the aws access key id you provided does not exist",
)

COS_FATAL_CONTEXT_MARKERS = (
    "cosserviceerror",
    "qcloud_cos",
)

COS_FATAL_ERROR_MARKERS = (
    "<code>unavailableforlegalreasons</code>",
    "due to your account is arrears",
    "<code>accountisolated</code>",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fieldnames} for row in rows)
    tmp_path.replace(path)


def upsert_csv(path: Path, row: dict[str, str], fieldnames: list[str], key: str) -> None:
    rows = read_csv(path) if path.exists() else []
    rows = [existing for existing in rows if existing.get(key) != row.get(key)]
    rows.append({field: row.get(field, "") for field in fieldnames})
    rows.sort(key=lambda item: item.get(key, "").casefold())
    write_csv(path, rows, fieldnames)


def participant_slug(participant_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", participant_id).strip("_.-").lower()
    if not slug:
        raise ValueError(f"Unsafe participant id: {participant_id!r}")
    return slug


def source_manifest_participant(path: Path) -> tuple[str, int]:
    rows = read_csv(path)
    if not rows:
        raise ValueError(f"Empty participant manifest: {path}")
    participants = {row.get("participant_id", "").strip() for row in rows}
    participants.discard("")
    if len(participants) != 1:
        raise ValueError(f"Expected exactly one participant in {path}, found {participants}")
    participant = participants.pop()
    orders = [int(row.get("benchmark_session_order") or 0) for row in rows]
    if orders != list(range(1, len(rows) + 1)):
        raise ValueError(f"Non-contiguous benchmark session order in {path}: {orders[:20]}")
    return participant, len(rows)


def discover_manifests(manifest_dir: Path, participants: str) -> list[tuple[str, Path, int]]:
    available: dict[str, tuple[str, Path, int]] = {}
    for path in sorted(manifest_dir.glob("*_all_videos.csv")):
        participant, count = source_manifest_participant(path)
        key = participant.casefold()
        if key in available:
            raise ValueError(f"Duplicate manifests for participant {participant}")
        available[key] = (participant, path, count)

    if participants.strip().casefold() == "all":
        selected = sorted(available.values(), key=lambda item: item[0].casefold())
    else:
        selected = []
        for requested in (item.strip() for item in participants.split(",")):
            if not requested:
                continue
            item = available.get(requested.casefold())
            if item is None:
                raise FileNotFoundError(f"Missing participant manifest: {requested}")
            selected.append(item)
    if not selected:
        raise FileNotFoundError(f"No participant manifests found in {manifest_dir}")
    return selected


def output_url_csv(data_root: Path, participant_id: str) -> Path:
    return (
        data_root
        / "cos_urls"
        / f"{participant_slug(participant_id)}_all_videos_proxy_540p16_urls.csv"
    )


def manifest_video_uids(path: Path) -> set[str]:
    return {row["video_uid"] for row in read_csv(path) if row.get("video_uid")}


def completed_manifest_video_count(url_csv: Path, manifest: Path) -> int:
    expected = manifest_video_uids(manifest)
    if not url_csv.exists():
        return 0
    uploaded = {
        row["video_uid"]
        for row in read_csv(url_csv)
        if row.get("video_uid") and row.get("signed_url")
    }
    return len(expected & uploaded)


def prune_queue_status(path: Path, participant_ids: set[str]) -> int:
    if not path.exists():
        return 0
    rows = read_csv(path)
    selected = {participant.casefold() for participant in participant_ids}
    retained = [
        row
        for row in rows
        if row.get("participant_id", "").casefold() in selected
    ]
    removed = len(rows) - len(retained)
    if removed:
        write_csv(path, retained, QUEUE_STATUS_FIELDS)
    return removed


def read_log_tail(
    path: Path,
    max_bytes: int = 1024 * 1024,
    start_offset: int = 0,
) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(start_offset, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def detect_source_auth_error(log_path: Path, start_offset: int = 0) -> str | None:
    text = read_log_tail(log_path, start_offset=start_offset).casefold()
    if not any(marker in text for marker in SOURCE_AUTH_CONTEXT_MARKERS):
        return None
    return next(
        (marker for marker in SOURCE_AUTH_ERROR_MARKERS if marker in text),
        None,
    )


def detect_cos_fatal_error(log_path: Path, start_offset: int = 0) -> str | None:
    text = read_log_tail(log_path, start_offset=start_offset).casefold()
    if not any(marker in text for marker in COS_FATAL_CONTEXT_MARKERS):
        return None
    return next(
        (marker for marker in COS_FATAL_ERROR_MARKERS if marker in text),
        None,
    )


def detect_global_fatal_error(
    log_path: Path,
    start_offset: int = 0,
) -> tuple[str, str] | None:
    source_error = detect_source_auth_error(log_path, start_offset)
    if source_error:
        return "source_auth_error", source_error
    cos_error = detect_cos_fatal_error(log_path, start_offset)
    if cos_error:
        return "cos_account_error", cos_error
    return None


def cos_write_preflight(client, bucket: str, key: str) -> None:
    created = False
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"video-memory-benchmark")
        created = True
        client.head_object(Bucket=bucket, Key=key)
    except Exception:
        if created:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass
        raise
    client.delete_object(Bucket=bucket, Key=key)


def verify_cos_writable(config_path: Path, prefix: str, run_name: str) -> None:
    if __package__:
        from .run_ego4d_vpn_batch import make_cos_client
    else:
        from run_ego4d_vpn_batch import make_cos_client

    client, cos = make_cos_client(config_path)
    key = (
        f"{prefix.strip('/')}/_pipeline_health/"
        f"{participant_slug(run_name)}-{uuid.uuid4().hex}.txt"
    )
    cos_write_preflight(client, cos["bucket"], key)


def build_batch_command(manifest: Path, participant_id: str, args: argparse.Namespace) -> list[str]:
    command = [
        args.python,
        str(args.batch_script),
        "--manifest",
        str(manifest),
        "--ego4d-root",
        str(args.ego4d_root),
        "--ego4d-video-dir",
        str(args.ego4d_video_dir),
        "--data-root",
        str(args.data_root),
        "--aws-profile",
        args.aws_profile,
        "--cos-config",
        str(args.cos_config),
        "--cos-prefix",
        args.cos_prefix,
        "--run-name",
        f"{participant_slug(participant_id)}_all_videos",
        "--url-expire-days",
        str(args.url_expire_days),
        "--ffmpeg-threads",
        str(args.ffmpeg_threads),
        "--min-free-gb",
        str(args.min_free_gb),
        "--fail-fast",
    ]
    if not args.keep_raw:
        command.append("--delete-raw-after-upload")
    if not args.keep_proxy:
        command.append("--delete-proxy-after-upload")
    if getattr(args, "rerun_completed_participants", False):
        command.append("--rerun-completed")
    return command


def terminate_active(active: dict[str, dict[str, object]]) -> None:
    for item in active.values():
        process = item["process"]
        assert isinstance(process, subprocess.Popen)
        if process.poll() is None:
            process.terminate()
    time.sleep(5)
    for item in active.values():
        process = item["process"]
        assert isinstance(process, subprocess.Popen)
        if process.poll() is None:
            process.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--participants", default="all")
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--data-root", default="/home/lighthouse/video-benchmark/data")
    parser.add_argument("--ego4d-root", default="/home/lighthouse/video-benchmark/data/ego4d")
    parser.add_argument("--ego4d-video-dir")
    parser.add_argument("--aws-profile", default="ego4d")
    parser.add_argument("--cos-config", default="~/.cos.conf")
    parser.add_argument("--cos-prefix", default="video-benchmark/ego4d")
    parser.add_argument("--url-expire-days", type=int, default=90)
    parser.add_argument("--ffmpeg-threads", type=int, default=1)
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--batch-script", default=str(ROOT / "scripts/run_ego4d_vpn_batch.py"))
    parser.add_argument("--run-name", default="ego4d_general_full_queue")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--keep-proxy", action="store_true")
    parser.add_argument("--rerun-completed-participants", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_workers < 1:
        parser.error("--max-workers must be >= 1")
    if args.ffmpeg_threads < 0:
        parser.error("--ffmpeg-threads must be >= 0")
    if args.url_expire_days < 1:
        parser.error("--url-expire-days must be >= 1")
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be >= 1")

    args.manifest_dir = Path(args.manifest_dir)
    args.data_root = Path(args.data_root)
    args.ego4d_root = Path(args.ego4d_root)
    args.ego4d_video_dir = (
        Path(args.ego4d_video_dir)
        if args.ego4d_video_dir
        else args.ego4d_root / "v2/video_540ss"
    )
    args.cos_config = Path(args.cos_config).expanduser()
    args.batch_script = Path(args.batch_script)
    return args


def main() -> None:
    args = parse_args()
    manifests = discover_manifests(args.manifest_dir, args.participants)
    if not args.dry_run:
        print("Checking COS write access...", flush=True)
        verify_cos_writable(args.cos_config, args.cos_prefix, args.run_name)
        print("COS write access: ok", flush=True)
    queue_status = (
        args.data_root
        / "processed/ego4d_pipeline_runs"
        / f"{args.run_name}_status.csv"
    )
    removed_status_rows = prune_queue_status(
        queue_status,
        {participant_id for participant_id, _, _ in manifests},
    )
    if removed_status_rows:
        print(
            f"Pruned {removed_status_rows} stale participant status rows.",
            flush=True,
        )
    log_dir = (
        args.data_root
        / "processed/ego4d_pipeline_runs"
        / f"{args.run_name}_logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    pending: list[dict[str, object]] = []
    for participant_id, manifest, expected in manifests:
        url_csv = output_url_csv(args.data_root, participant_id)
        completed = completed_manifest_video_count(url_csv, manifest)
        status = "ok" if completed == expected and not args.rerun_completed_participants else "queued"
        upsert_csv(
            queue_status,
            {
                "updated_at": utc_now(),
                "participant_id": participant_id,
                "status": status,
                "started_at": "",
                "finished_at": utc_now() if status == "ok" else "",
                "returncode": "0" if status == "ok" else "",
                "selected_videos": str(expected),
                "uploaded_videos": str(completed),
                "manifest": str(manifest),
                "url_csv": str(url_csv),
                "log_path": "",
            },
            QUEUE_STATUS_FIELDS,
            "participant_id",
        )
        if status != "ok":
            pending.append(
                {
                    "participant_id": participant_id,
                    "manifest": manifest,
                    "expected": expected,
                    "url_csv": url_csv,
                }
            )

    print(f"Participants discovered: {len(manifests)}", flush=True)
    print(f"Participants pending: {len(pending)}", flush=True)
    print(f"Max workers: {args.max_workers}", flush=True)
    print(f"Queue status: {queue_status}", flush=True)
    print(f"Raw cleanup: {not args.keep_raw}", flush=True)
    print(f"Proxy cleanup: {not args.keep_proxy}", flush=True)

    if args.dry_run:
        for item in pending:
            print(
                subprocess.list2cmdline(
                    build_batch_command(
                        Path(item["manifest"]), str(item["participant_id"]), args
                    )
                ),
                flush=True,
            )
        return

    active: dict[str, dict[str, object]] = {}
    fatal_error: tuple[str, str, str] | None = None

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
            manifest = Path(item["manifest"])
            log_path = log_dir / f"{participant_slug(participant_id)}.log"
            log_file = log_path.open("a", encoding="utf-8")
            log_start_offset = log_file.tell()
            started_at = utc_now()
            command = build_batch_command(manifest, participant_id, args)
            print(f"Starting {participant_id}: {subprocess.list2cmdline(command)}", flush=True)
            log_file.write(f"[{started_at}] Starting {subprocess.list2cmdline(command)}\n")
            log_file.flush()
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            active[participant_id] = {
                **item,
                "process": process,
                "log_file": log_file,
                "log_path": log_path,
                "log_start_offset": log_start_offset,
                "started_at": started_at,
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
                    "selected_videos": str(item["expected"]),
                    "uploaded_videos": str(
                        completed_manifest_video_count(
                            Path(item["url_csv"]), Path(item["manifest"])
                        )
                    ),
                    "manifest": str(manifest),
                    "url_csv": str(item["url_csv"]),
                    "log_path": str(log_path),
                },
                QUEUE_STATUS_FIELDS,
                "participant_id",
            )

        finished: list[str] = []
        for participant_id, item in active.items():
            process = item["process"]
            assert isinstance(process, subprocess.Popen)
            returncode = process.poll()
            if returncode is None:
                continue
            log_file = item["log_file"]
            log_file.write(f"[{utc_now()}] Finished with return code {returncode}\n")
            log_file.close()
            expected = int(item["expected"])
            uploaded = completed_manifest_video_count(
                Path(item["url_csv"]), Path(item["manifest"])
            )
            status = "ok" if returncode == 0 and uploaded == expected else "error"
            detected_fatal_error = (
                detect_global_fatal_error(
                    Path(item["log_path"]),
                    int(item["log_start_offset"]),
                )
                if returncode != 0
                else None
            )
            if detected_fatal_error:
                status, marker = detected_fatal_error
                if fatal_error is None:
                    fatal_error = (participant_id, status, marker)
            print(
                f"Finished {participant_id}: {status}; uploaded={uploaded}/{expected}; rc={returncode}",
                flush=True,
            )
            upsert_csv(
                queue_status,
                {
                    "updated_at": utc_now(),
                    "participant_id": participant_id,
                    "status": status,
                    "started_at": str(item["started_at"]),
                    "finished_at": utc_now(),
                    "returncode": str(returncode),
                    "selected_videos": str(expected),
                    "uploaded_videos": str(uploaded),
                    "manifest": str(item["manifest"]),
                    "url_csv": str(item["url_csv"]),
                    "log_path": str(item["log_path"]),
                },
                QUEUE_STATUS_FIELDS,
                "participant_id",
            )
            finished.append(participant_id)
        for participant_id in finished:
            active.pop(participant_id)
        if fatal_error is not None:
            failed_participant, failure_status, marker = fatal_error
            failure_label = (
                "Ego4D source authorization"
                if failure_status == "source_auth_error"
                else "COS account availability"
            )
            blocked_status = (
                "blocked_source_auth"
                if failure_status == "source_auth_error"
                else "blocked_cos_account"
            )
            print(
                f"Fatal {failure_label} failure detected for "
                f"{failed_participant} ({marker}); stopping the queue.",
                flush=True,
            )
            terminate_active(active)
            for participant_id, item in active.items():
                process = item["process"]
                assert isinstance(process, subprocess.Popen)
                process.wait()
                log_file = item["log_file"]
                log_file.write(
                    f"[{utc_now()}] Aborted after {failure_label} failure "
                    f"in {failed_participant}\n"
                )
                log_file.close()
                expected = int(item["expected"])
                uploaded = completed_manifest_video_count(
                    Path(item["url_csv"]), Path(item["manifest"])
                )
                upsert_csv(
                    queue_status,
                    {
                        "updated_at": utc_now(),
                        "participant_id": participant_id,
                        "status": blocked_status,
                        "started_at": str(item["started_at"]),
                        "finished_at": utc_now(),
                        "returncode": str(process.returncode),
                        "selected_videos": str(expected),
                        "uploaded_videos": str(uploaded),
                        "manifest": str(item["manifest"]),
                        "url_csv": str(item["url_csv"]),
                        "log_path": str(item["log_path"]),
                    },
                    QUEUE_STATUS_FIELDS,
                    "participant_id",
                )
            active.clear()
            if failure_status == "source_auth_error":
                raise SystemExit(
                    "Ego4D source authorization failed. Renew or replace the AWS "
                    "profile, validate one video, then rerun the same queue command."
                )
            raise SystemExit(
                "COS account is unavailable. Restore COS write access, run the "
                "write preflight, then rerun the same queue command."
            )
        if pending or active:
            time.sleep(args.poll_seconds)

    final_rows = read_csv(queue_status)
    failed = [row["participant_id"] for row in final_rows if row.get("status") == "error"]
    print(f"Queue finished. Status: {queue_status}", flush=True)
    if failed:
        raise SystemExit(f"Participants failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
