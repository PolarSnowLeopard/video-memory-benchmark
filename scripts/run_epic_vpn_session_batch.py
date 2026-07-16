#!/usr/bin/env python3
"""Split proxy videos into fixed-length sessions for inference."""

from __future__ import annotations

import argparse
import configparser
import csv
import mimetypes
import os
import re
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEO_SUMMARY = ROOT / "data/processed/epic_kitchens_100/video_summary.csv"
DEFAULT_CUT_MODE = "reencode"
DEFAULT_REENCODE_CRF = 23
DEFAULT_MAX_DURATION_ERROR_SEC = 0.25
DEFAULT_MAX_SOURCE_DURATION_ERROR_SEC = 1.0
DEFAULT_DOWNLOAD_ATTEMPTS = 3
FFMPEG_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)"
)

STATUS_FIELDS = [
    "updated_at",
    "status",
    "error",
    "session_id",
    "participant_id",
    "source_video_id",
    "session_index",
    "start_sec",
    "end_sec",
    "duration_sec",
    "actual_duration_sec",
    "duration_error_sec",
    "duration_validated",
    "source_proxy_path",
    "session_path",
    "session_size_bytes",
    "cos_key",
    "session_deleted",
]

URL_FIELDS = [
    "session_id",
    "participant_id",
    "source_video_id",
    "video_id",
    "benchmark_session_order",
    "benchmark_order_status",
    "benchmark_order_basis",
    "benchmark_temporal_evolution_eligible",
    "session_index",
    "start_sec",
    "end_sec",
    "duration_sec",
    "actual_duration_sec",
    "duration_error_sec",
    "duration_validated",
    "local_path",
    "bucket",
    "region",
    "key",
    "size_bytes",
    "content_type",
    "signed_url",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def upsert_csv(path: Path, row: dict[str, str], fieldnames: list[str], key_fields: tuple[str, ...]) -> None:
    rows = read_csv(path) if path.exists() else []
    filtered = [r for r in rows if tuple(r.get(k, "") for k in key_fields) != tuple(row.get(k, "") for k in key_fields)]
    filtered.append({k: row.get(k, "") for k in fieldnames})
    write_csv(path, filtered, fieldnames)


def remove_csv_row(path: Path, fieldnames: list[str], key_field: str, key_value: str) -> None:
    if not path.exists():
        return
    rows = read_csv(path)
    filtered = [row for row in rows if row.get(key_field) != key_value]
    if len(filtered) != len(rows):
        write_csv(path, filtered, fieldnames)


def read_cos_config(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser()
    parser.read(path)
    if "common" not in parser:
        raise RuntimeError(f"Missing [common] section in {path}")
    common = parser["common"]
    required = ["secret_id", "secret_key", "bucket", "region"]
    missing = [key for key in required if not common.get(key)]
    if missing:
        raise RuntimeError(f"Missing COS config keys in {path}: {', '.join(missing)}")
    return {
        "secret_id": common["secret_id"],
        "secret_key": common["secret_key"],
        "bucket": common["bucket"],
        "region": common["region"],
        "schema": common.get("schema", "https"),
    }


def make_cos_client(config_path: Path):
    from qcloud_cos import CosConfig, CosS3Client

    cos = read_cos_config(config_path)
    config = CosConfig(
        Region=cos["region"],
        SecretId=cos["secret_id"],
        SecretKey=cos["secret_key"],
        Scheme=cos["schema"],
    )
    return CosS3Client(config), cos


def content_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def run(cmd: list[str], dry_run: bool = False) -> None:
    print("$ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    subprocess.run(cmd, check=True)


def resolve_binary(name: str, configured: str | None, env_name: str, dry_run: bool = False) -> str:
    if configured:
        return configured
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    found = shutil.which(name)
    if found:
        return found
    if dry_run:
        return name
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    raise FileNotFoundError(
        f"Cannot find {name}. Install ffmpeg, set {env_name}, or pass --{name}-bin."
    )


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe_duration.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


ffprobe_duration.ffprobe_bin = "ffprobe"  # type: ignore[attr-defined]


def parse_ffmpeg_duration(output: str) -> float:
    match = FFMPEG_DURATION_RE.search(output)
    if match is None:
        raise ValueError("Could not parse container duration from ffmpeg output")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def ffmpeg_container_duration(path: Path, ffmpeg_bin: str) -> float:
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    return parse_ffmpeg_duration(result.stderr)


def validate_session_duration(actual_sec: float, planned_sec: float, max_error_sec: float) -> float:
    error_sec = abs(actual_sec - planned_sec)
    if error_sec > max_error_sec:
        raise RuntimeError(
            "Session duration mismatch: "
            f"planned={fmt_float(planned_sec)}s actual={fmt_float(actual_sec)}s "
            f"error={fmt_float(error_sec)}s limit={fmt_float(max_error_sec)}s"
        )
    return error_sec


def metadata_durations(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    for row in read_csv(path):
        value = row.get("duration_sec") or ""
        if value:
            out[row["video_id"]] = float(value)
    return out


def parse_list(value: str | None) -> set[str] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    return items or None


def fmt_float(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def fmt_time_for_name(value: float) -> str:
    return f"{int(round(value)):06d}"


def split_bounds(duration_sec: float, session_duration_sec: int, min_tail_sec: int) -> list[tuple[float, float]]:
    if duration_sec <= 0:
        return []
    bounds: list[tuple[float, float]] = []
    start = 0.0
    while start < duration_sec:
        end = min(start + session_duration_sec, duration_sec)
        tail = duration_sec - end
        if bounds and 0 < tail < min_tail_sec:
            end = duration_sec
        bounds.append((start, end))
        start = end
    if len(bounds) >= 2 and bounds[-1][1] - bounds[-1][0] < min_tail_sec:
        prev_start, _ = bounds[-2]
        bounds[-2] = (prev_start, bounds[-1][1])
        bounds.pop()
    return bounds


def completed_session_ids(status_csv: Path, url_csv: Path, require_url: bool) -> set[str]:
    if not status_csv.exists():
        return set()
    ok_ids = {
        row["session_id"]
        for row in read_csv(status_csv)
        if row.get("status") == "ok"
        and row.get("duration_validated", "").lower() == "true"
        and row.get("session_id")
    }
    if not require_url:
        return ok_ids
    if not url_csv.exists():
        return set()
    uploaded_ids = {
        row["session_id"]
        for row in read_csv(url_csv)
        if row.get("signed_url")
        and row.get("duration_validated", "").lower() == "true"
        and row.get("session_id")
    }
    return ok_ids & uploaded_ids


def cut_session(source_path: Path, output_path: Path, start_sec: float, duration_sec: float, args: argparse.Namespace) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.cut_mode == "copy":
        cmd = [
            args.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            fmt_float(start_sec),
            "-i",
            str(source_path),
            "-t",
            fmt_float(duration_sec),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    else:
        cmd = [
            args.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            fmt_float(start_sec),
            "-i",
            str(source_path),
            "-t",
            fmt_float(duration_sec),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(getattr(args, "reencode_crf", DEFAULT_REENCODE_CRF)),
            "-pix_fmt",
            "yuv420p",
        ]
        if args.ffmpeg_threads > 0:
            cmd.extend(["-threads", str(args.ffmpeg_threads)])
        cmd.extend(["-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(output_path)])
    run(cmd, dry_run=args.dry_run)


def upload_session(client, cos: dict[str, str], path: Path, key: str, url_expire_days: int, dry_run: bool) -> str:
    bucket = cos["bucket"]
    if dry_run:
        print(f"Would upload {path} -> cos://{bucket}/{key}", flush=True)
        return ""
    print(f"Uploading {path} -> cos://{bucket}/{key}", flush=True)
    client.upload_file(
        Bucket=bucket,
        Key=key,
        LocalFilePath=str(path),
        PartSize=8,
        MAXThread=8,
        EnableMD5=True,
    )
    return client.get_presigned_url(
        Bucket=bucket,
        Key=key,
        Method="GET",
        Expired=url_expire_days * 24 * 3600,
    )


def local_session_url(session_root: Path, session_path: Path, local_url_base: str) -> str:
    relative = session_path.relative_to(session_root)
    return f"{local_url_base.rstrip('/')}/{relative.as_posix()}"


def source_video_id(row: dict[str, str]) -> str:
    return row.get("video_id") or row.get("source_video_id") or Path(row.get("key") or row.get("local_path") or "").stem.split("_540p16")[0]


def participant_id(row: dict[str, str], video_id: str) -> str:
    return row.get("participant_id") or video_id.split("_", 1)[0]


def benchmark_order_metadata(row: dict[str, str]) -> dict[str, str]:
    return {
        "benchmark_session_order": row.get("benchmark_session_order", ""),
        "benchmark_order_status": row.get("benchmark_order_status", ""),
        "benchmark_order_basis": row.get("benchmark_order_basis", ""),
        "benchmark_temporal_evolution_eligible": row.get(
            "benchmark_temporal_evolution_eligible", ""
        ),
    }


def source_proxy_path(row: dict[str, str], args: argparse.Namespace) -> Path:
    video_id = source_video_id(row)
    participant = participant_id(row, video_id)
    if args.source_cache_root:
        name = Path(row.get("key") or row.get("local_path") or f"{video_id}_540p16.mp4").name
        return args.source_cache_root / participant / name
    return Path(row["local_path"])


def download_source_proxy(
    row: dict[str, str],
    source_path: Path,
    dry_run: bool,
    *,
    attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
    retry_delay_sec: float = 3.0,
) -> None:
    signed_url = row.get("signed_url")
    if not signed_url:
        raise RuntimeError(f"Cannot download missing source without signed_url: {source_video_id(row)}")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = source_path.with_suffix(source_path.suffix + ".part")
    print(f"Downloading source proxy: {source_video_id(row)} -> {source_path}", flush=True)
    if dry_run:
        return
    for attempt in range(1, attempts + 1):
        tmp_path.unlink(missing_ok=True)
        try:
            downloaded = 0
            with urllib.request.urlopen(signed_url, timeout=3600) as response, tmp_path.open("wb") as f:
                content_length = response.headers.get("Content-Length")
                expected_size = int(content_length) if content_length else None
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
            if expected_size is not None and downloaded != expected_size:
                raise RuntimeError(
                    f"Incomplete proxy download: expected={expected_size} bytes actual={downloaded} bytes"
                )
            tmp_path.replace(source_path)
            return
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            if attempt >= attempts:
                raise
            print(
                f"Proxy download attempt {attempt}/{attempts} failed: {exc!r}; retrying",
                flush=True,
            )
            time.sleep(retry_delay_sec)


def expected_source_duration(row: dict[str, str], durations: dict[str, float]) -> float | None:
    for key in ("duration_sec", "source_duration_sec"):
        value = row.get(key) or ""
        if value:
            return float(value)
    video_id = source_video_id(row)
    if video_id in durations:
        return durations[video_id]
    return None


def source_duration(
    row: dict[str, str],
    durations: dict[str, float],
    source_path: Path,
    dry_run: bool,
    max_error_sec: float = DEFAULT_MAX_SOURCE_DURATION_ERROR_SEC,
    ffmpeg_bin: str = "ffmpeg",
) -> float:
    expected = expected_source_duration(row, durations)
    video_id = source_video_id(row)
    if dry_run:
        if expected is None:
            raise RuntimeError(f"Cannot determine duration for {video_id}; provide metadata")
        return expected
    if not source_path.exists():
        raise RuntimeError(f"source proxy missing: {source_path}")
    actual = ffmpeg_container_duration(source_path, ffmpeg_bin)
    if expected is not None and abs(actual - expected) > max_error_sec:
        raise RuntimeError(
            "Source proxy duration mismatch: "
            f"video={video_id} expected={fmt_float(expected)}s actual={fmt_float(actual)}s "
            f"error={fmt_float(abs(actual - expected))}s limit={fmt_float(max_error_sec)}s"
        )
    return actual


def process_session(
    row: dict[str, str],
    session_index: int,
    start_sec: float,
    end_sec: float,
    args: argparse.Namespace,
    cos_client,
    cos: dict[str, str] | None,
) -> dict[str, str]:
    video_id = source_video_id(row)
    participant = participant_id(row, video_id)
    session_id = f"{video_id}_s{session_index:03d}"
    source_path = source_proxy_path(row, args)
    session_name = (
        f"{session_id}_{fmt_time_for_name(start_sec)}_{fmt_time_for_name(end_sec)}_"
        f"{args.session_duration_sec}s.mp4"
    )
    session_path = args.session_root / participant / f"sessions_{args.session_duration_sec}s" / session_name
    cos_key = f"{args.cos_prefix.strip('/')}/{participant}/sessions_{args.session_duration_sec}s/{session_name}"
    duration_sec = end_sec - start_sec

    if not source_path.exists() and args.download_missing_source:
        download_source_proxy(
            row,
            source_path,
            args.dry_run,
            attempts=args.download_attempts,
            retry_delay_sec=args.download_retry_delay_sec,
        )
    if not args.dry_run and not source_path.exists():
        raise RuntimeError(f"source proxy missing: {source_path}")
    validated_session_ids = getattr(args, "validated_session_ids", set())
    should_cut = (
        args.overwrite_sessions
        or not session_path.exists()
        or session_id not in validated_session_ids
    )
    if should_cut:
        if not args.dry_run:
            remove_csv_row(args.url_csv, URL_FIELDS, "session_id", session_id)
        cut_session(source_path, session_path, start_sec, duration_sec, args)
    if not args.dry_run and not session_path.exists():
        raise RuntimeError(f"session clip missing after cut: {session_path}")

    actual_duration_sec: float | None = None
    duration_error_sec: float | None = None
    if not args.dry_run:
        actual_duration_sec = ffmpeg_container_duration(session_path, args.ffmpeg_bin)
        duration_error_sec = validate_session_duration(
            actual_duration_sec,
            duration_sec,
            args.max_duration_error_sec,
        )

    session_size = str(session_path.stat().st_size if session_path.exists() else "")
    signed_url = ""
    if args.local_url_base:
        signed_url = local_session_url(args.session_root, session_path, args.local_url_base)
        url_row = {
            "session_id": session_id,
            "participant_id": participant,
            "source_video_id": video_id,
            "video_id": video_id,
            **benchmark_order_metadata(row),
            "session_index": str(session_index),
            "start_sec": fmt_float(start_sec),
            "end_sec": fmt_float(end_sec),
            "duration_sec": fmt_float(duration_sec),
            "actual_duration_sec": fmt_float(actual_duration_sec) if actual_duration_sec is not None else "",
            "duration_error_sec": fmt_float(duration_error_sec) if duration_error_sec is not None else "",
            "duration_validated": str(actual_duration_sec is not None),
            "local_path": str(session_path),
            "bucket": "",
            "region": "",
            "key": str(session_path.relative_to(args.session_root)),
            "size_bytes": session_size,
            "content_type": content_type_for(session_path),
            "signed_url": signed_url,
        }
        if not args.dry_run:
            upsert_csv(args.url_csv, url_row, URL_FIELDS, ("session_id", "key"))
    elif not args.skip_upload:
        if not args.dry_run and (cos_client is None or cos is None):
            raise RuntimeError("COS client is not available")
        if cos is None:
            raise RuntimeError("COS config is not available")
        signed_url = upload_session(cos_client, cos, session_path, cos_key, args.url_expire_days, args.dry_run)
        url_row = {
            "session_id": session_id,
            "participant_id": participant,
            "source_video_id": video_id,
            "video_id": video_id,
            **benchmark_order_metadata(row),
            "session_index": str(session_index),
            "start_sec": fmt_float(start_sec),
            "end_sec": fmt_float(end_sec),
            "duration_sec": fmt_float(duration_sec),
            "actual_duration_sec": fmt_float(actual_duration_sec) if actual_duration_sec is not None else "",
            "duration_error_sec": fmt_float(duration_error_sec) if duration_error_sec is not None else "",
            "duration_validated": str(actual_duration_sec is not None),
            "local_path": str(session_path),
            "bucket": cos["bucket"],
            "region": cos["region"],
            "key": cos_key,
            "size_bytes": session_size,
            "content_type": content_type_for(session_path),
            "signed_url": signed_url,
        }
        if not args.dry_run:
            upsert_csv(args.url_csv, url_row, URL_FIELDS, ("session_id", "key"))

    session_deleted = False
    if args.delete_session_after_upload and (signed_url or args.dry_run) and session_path.exists():
        print(f"Deleting session clip: {session_path}", flush=True)
        if not args.dry_run:
            session_path.unlink()
            session_deleted = True

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "error": "",
        "session_id": session_id,
        "participant_id": participant,
        "source_video_id": video_id,
        "session_index": str(session_index),
        "start_sec": fmt_float(start_sec),
        "end_sec": fmt_float(end_sec),
        "duration_sec": fmt_float(duration_sec),
        "actual_duration_sec": fmt_float(actual_duration_sec) if actual_duration_sec is not None else "",
        "duration_error_sec": fmt_float(duration_error_sec) if duration_error_sec is not None else "",
        "duration_validated": str(actual_duration_sec is not None),
        "source_proxy_path": str(source_path),
        "session_path": str(session_path),
        "session_size_bytes": session_size,
        "cos_key": str(session_path.relative_to(args.session_root)) if args.local_url_base else ("" if args.skip_upload else cos_key),
        "session_deleted": str(session_deleted),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-url-csv", required=True, help="Proxy video URL CSV produced by run_epic_vpn_batch.py.")
    parser.add_argument("--data-root", default="/home/lighthouse/video-benchmark/data")
    parser.add_argument("--metadata-csv", default=str(VIDEO_SUMMARY))
    parser.add_argument("--source-cache-root", help="Use this root for source proxy files instead of local_path from the URL CSV.")
    parser.add_argument("--download-missing-source", action="store_true", help="Download missing source proxy videos from signed_url before cutting.")
    parser.add_argument("--download-attempts", type=int, default=DEFAULT_DOWNLOAD_ATTEMPTS)
    parser.add_argument("--download-retry-delay-sec", type=float, default=3.0)
    parser.add_argument("--session-duration-sec", type=int, default=300)
    parser.add_argument("--min-tail-sec", type=int, default=60)
    parser.add_argument("--cos-config", default="~/.cos.conf")
    parser.add_argument("--cos-prefix", default="video-benchmark/epic-kitchens")
    parser.add_argument("--url-expire-days", type=int, default=30)
    parser.add_argument(
        "--local-url-base",
        help="Emit local HTTP URLs instead of uploading sessions, e.g. http://127.0.0.1:18080.",
    )
    parser.add_argument(
        "--cut-mode",
        choices=["copy", "reencode"],
        default=DEFAULT_CUT_MODE,
        help="reencode is frame-accurate; copy may include adjacent GOP content.",
    )
    parser.add_argument("--reencode-crf", type=int, default=DEFAULT_REENCODE_CRF)
    parser.add_argument(
        "--max-duration-error-sec",
        type=float,
        default=DEFAULT_MAX_DURATION_ERROR_SEC,
    )
    parser.add_argument(
        "--max-source-duration-error-sec",
        type=float,
        default=DEFAULT_MAX_SOURCE_DURATION_ERROR_SEC,
        help="Maximum allowed difference between source metadata and downloaded proxy duration.",
    )
    parser.add_argument("--ffmpeg-bin", help="Path to ffmpeg. Defaults to PATH, FFMPEG_BIN, then imageio-ffmpeg.")
    parser.add_argument("--ffprobe-bin", help="Path to ffprobe. Defaults to PATH or FFPROBE_BIN.")
    parser.add_argument("--ffmpeg-threads", type=int, default=2)
    parser.add_argument("--video-ids", help="Comma-separated source video ids to process.")
    parser.add_argument("--limit-videos", type=int)
    parser.add_argument("--limit-sessions", type=int)
    parser.add_argument("--overwrite-sessions", action="store_true")
    parser.add_argument("--delete-session-after-upload", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--rerun-completed", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.session_duration_sec <= 0:
        raise SystemExit("--session-duration-sec must be > 0")
    if args.min_tail_sec < 0:
        raise SystemExit("--min-tail-sec must be >= 0")
    if args.ffmpeg_threads < 0:
        raise SystemExit("--ffmpeg-threads must be >= 0")
    if not 0 <= args.reencode_crf <= 51:
        raise SystemExit("--reencode-crf must be between 0 and 51")
    if args.max_duration_error_sec < 0:
        raise SystemExit("--max-duration-error-sec must be >= 0")
    if args.max_source_duration_error_sec < 0:
        raise SystemExit("--max-source-duration-error-sec must be >= 0")
    if args.download_attempts < 1:
        raise SystemExit("--download-attempts must be >= 1")
    if args.download_retry_delay_sec < 0:
        raise SystemExit("--download-retry-delay-sec must be >= 0")
    if args.local_url_base and args.delete_session_after_upload:
        raise SystemExit("--delete-session-after-upload cannot be used with --local-url-base")

    args.video_url_csv = Path(args.video_url_csv)
    args.data_root = Path(args.data_root)
    args.session_root = args.data_root / "sessions"
    args.source_cache_root = Path(args.source_cache_root) if args.source_cache_root else None
    args.ffmpeg_bin = resolve_binary("ffmpeg", args.ffmpeg_bin, "FFMPEG_BIN", args.dry_run)
    if args.ffprobe_bin or shutil.which("ffprobe") or os.environ.get("FFPROBE_BIN"):
        ffprobe_duration.ffprobe_bin = resolve_binary("ffprobe", args.ffprobe_bin, "FFPROBE_BIN", args.dry_run)  # type: ignore[attr-defined]
    args.cos_config = Path(args.cos_config).expanduser()
    metadata_csv = Path(args.metadata_csv)
    durations = metadata_durations(metadata_csv)

    stem = args.video_url_csv.stem
    for suffix in ("_proxy_540p16_urls", "_urls"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    args.status_csv = args.data_root / "processed/epic_pipeline_runs" / f"{stem}_sessions_{args.session_duration_sec}s_status.csv"
    args.url_csv = args.data_root / "cos_urls" / f"{stem}_sessions_{args.session_duration_sec}s_urls.csv"

    rows = read_csv(args.video_url_csv)
    wanted = parse_list(args.video_ids)
    selected = [row for row in rows if wanted is None or source_video_id(row) in wanted]
    if args.limit_videos is not None:
        selected = selected[: args.limit_videos]

    cos_client = None
    cos = None
    uses_cos_upload = not args.skip_upload and not args.local_url_base
    if uses_cos_upload and not args.dry_run:
        cos_client, cos = make_cos_client(args.cos_config)
    elif uses_cos_upload:
        cos = {"bucket": "DRY_RUN_BUCKET", "region": "DRY_RUN_REGION"}

    require_url = bool(args.local_url_base) or not args.skip_upload
    args.validated_session_ids = completed_session_ids(
        args.status_csv, args.url_csv, require_url=False
    )
    completed = set() if args.rerun_completed else completed_session_ids(args.status_csv, args.url_csv, require_url)

    print(f"Video URL CSV: {args.video_url_csv}", flush=True)
    print(f"Selected source videos: {len(selected)}", flush=True)
    print(f"Session duration: {args.session_duration_sec}s", flush=True)
    print(f"Min tail: {args.min_tail_sec}s", flush=True)
    print(f"Cut mode: {args.cut_mode}", flush=True)
    if args.cut_mode == "reencode":
        print(f"Reencode CRF: {args.reencode_crf}", flush=True)
    print(f"Max duration error: {fmt_float(args.max_duration_error_sec)}s", flush=True)
    print(f"Status CSV: {args.status_csv}", flush=True)
    print(f"URL CSV: {args.url_csv}", flush=True)
    if args.local_url_base:
        print(f"Local URL base: {args.local_url_base}", flush=True)
    if completed:
        print(f"Already completed sessions: {len(completed)}", flush=True)

    processed_sessions = 0
    total_planned = 0
    for row in selected:
        video_id = source_video_id(row)
        expected_duration = expected_source_duration(row, durations)
        if expected_duration is not None:
            expected_bounds = split_bounds(
                expected_duration,
                args.session_duration_sec,
                args.min_tail_sec,
            )
            expected_ids = {
                f"{video_id}_s{session_index:03d}"
                for session_index in range(len(expected_bounds))
            }
            if expected_ids and expected_ids <= completed:
                total_planned += len(expected_bounds)
                print(
                    f"\n{video_id}: all {len(expected_bounds)} sessions already completed; "
                    "skipping source download",
                    flush=True,
                )
                continue
        source_path = source_proxy_path(row, args)
        source_was_present = source_path.exists()
        if not source_was_present and args.download_missing_source:
            download_source_proxy(
                row,
                source_path,
                args.dry_run,
                attempts=args.download_attempts,
                retry_delay_sec=args.download_retry_delay_sec,
            )
        try:
            duration = source_duration(
                row,
                durations,
                source_path,
                args.dry_run,
                args.max_source_duration_error_sec,
                args.ffmpeg_bin,
            )
        except RuntimeError as exc:
            if (
                source_was_present
                and args.download_missing_source
                and str(exc).startswith("Source proxy duration mismatch:")
            ):
                print(f"Removing stale source proxy and downloading again: {source_path}", flush=True)
                source_path.unlink(missing_ok=True)
                download_source_proxy(
                    row,
                    source_path,
                    args.dry_run,
                    attempts=args.download_attempts,
                    retry_delay_sec=args.download_retry_delay_sec,
                )
                duration = source_duration(
                    row,
                    durations,
                    source_path,
                    args.dry_run,
                    args.max_source_duration_error_sec,
                    args.ffmpeg_bin,
                )
            else:
                raise
        bounds = split_bounds(duration, args.session_duration_sec, args.min_tail_sec)
        total_planned += len(bounds)
        print(f"\n{video_id}: duration={fmt_float(duration)}s sessions={len(bounds)}", flush=True)
        for session_index, (start_sec, end_sec) in enumerate(bounds):
            session_id = f"{video_id}_s{session_index:03d}"
            if args.limit_sessions is not None and processed_sessions >= args.limit_sessions:
                print("Session limit reached.", flush=True)
                print(f"Planned sessions seen: {total_planned}", flush=True)
                return
            if session_id in completed:
                print(f"  {session_id} already completed; skipping.", flush=True)
                continue
            print(f"  {session_id}: {fmt_float(start_sec)}-{fmt_float(end_sec)}", flush=True)
            try:
                status = process_session(row, session_index, start_sec, end_sec, args, cos_client, cos)
            except Exception as exc:
                status = {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "error",
                    "error": repr(exc),
                    "session_id": session_id,
                    "participant_id": participant_id(row, video_id),
                    "source_video_id": video_id,
                    "session_index": str(session_index),
                    "start_sec": fmt_float(start_sec),
                    "end_sec": fmt_float(end_sec),
                    "duration_sec": fmt_float(end_sec - start_sec),
                    "actual_duration_sec": "",
                    "duration_error_sec": "",
                    "duration_validated": "False",
                    "source_proxy_path": str(source_proxy_path(row, args)),
                    "session_path": "",
                    "session_size_bytes": "",
                    "cos_key": "",
                    "session_deleted": "",
                }
                print(f"ERROR {session_id}: {exc!r}", flush=True)
                if args.fail_fast:
                    if not args.dry_run:
                        upsert_csv(args.status_csv, status, STATUS_FIELDS, ("session_id",))
                    raise
            if not args.dry_run:
                upsert_csv(args.status_csv, status, STATUS_FIELDS, ("session_id",))
            processed_sessions += 1
    print(f"\nDone. Planned sessions: {total_planned}; processed this run: {processed_sessions}", flush=True)


if __name__ == "__main__":
    main()
