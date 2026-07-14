#!/usr/bin/env python3
"""Download, transcode, validate, and upload bounded Ego4D video batches."""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence


STATUS_FIELDS = [
    "updated_at",
    "status",
    "error",
    "participant_id",
    "video_uid",
    "raw_path",
    "raw_size_bytes",
    "raw_probe_ok",
    "proxy_path",
    "proxy_size_bytes",
    "proxy_probe_ok",
    "source_duration_sec",
    "proxy_duration_sec",
    "cos_key",
    "cos_size_verified",
    "raw_deleted",
    "proxy_deleted",
]

URL_FIELDS = [
    "dataset",
    "participant_id",
    "fb_participant_id",
    "video_uid",
    "video_id",
    "canonical_session_id",
    "duration_sec",
    "source_duration_sec",
    "scenarios",
    "has_audio",
    "cross_video_order_status",
    "cross_video_order_basis",
    "temporal_evolution_eligible",
    "benchmark_session_order",
    "benchmark_order_status",
    "benchmark_order_basis",
    "benchmark_temporal_evolution_eligible",
    "local_path",
    "bucket",
    "region",
    "key",
    "size_bytes",
    "content_type",
    "signed_url",
]


@dataclass(frozen=True)
class MediaProbe:
    duration_sec: float
    video_codec: str
    width: int
    height: int
    avg_fps: float
    audio_codec: str | None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key, "") for key in fieldnames} for row in rows)
    tmp_path.replace(path)


def upsert_csv(
    path: Path,
    row: dict[str, str],
    fieldnames: list[str],
    key_fields: tuple[str, ...],
) -> None:
    rows = read_csv(path) if path.exists() else []
    row_key = tuple(row.get(key, "") for key in key_fields)
    rows = [
        existing
        for existing in rows
        if tuple(existing.get(key, "") for key in key_fields) != row_key
    ]
    rows.append(row)
    write_csv(path, rows, fieldnames)


def safe_component(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"Unsafe {label}: {value!r}")
    return value


def load_manifest(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    if not rows:
        raise ValueError(f"Manifest is empty: {path}")
    required = {"video_uid", "participant_id"}
    missing_fields = sorted(required - set(rows[0]))
    if missing_fields:
        raise ValueError(f"Manifest is missing fields: {', '.join(missing_fields)}")

    seen: set[str] = set()
    for row in rows:
        uid = safe_component((row.get("video_uid") or "").strip(), "video_uid")
        safe_component((row.get("participant_id") or "").strip(), "participant_id")
        if uid in seen:
            raise ValueError(f"Duplicate video_uid in manifest: {uid}")
        seen.add(uid)
    return rows


def select_rows(
    rows: Sequence[dict[str, str]],
    video_uids: set[str] | None,
    limit: int | None,
) -> list[dict[str, str]]:
    selected = [
        row for row in rows if video_uids is None or row["video_uid"] in video_uids
    ]
    return selected[:limit] if limit is not None else selected


def write_command(cmd: Sequence[str]) -> None:
    print("$ " + " ".join(cmd), flush=True)


def run(cmd: list[str], dry_run: bool = False) -> None:
    write_command(cmd)
    if not dry_run:
        subprocess.run(cmd, check=True)


def resolve_binary(name: str, configured: str | None, env_name: str) -> str:
    if configured:
        return configured
    if os.environ.get(env_name):
        return os.environ[env_name]
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"Cannot find {name}. Install it, set {env_name}, or pass --{name}-bin."
    )


def parse_fps(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    return float(Fraction(value))


def probe_media(path: Path, ffprobe_bin: str) -> MediaProbe:
    result = subprocess.run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), None
    )
    if video is None:
        raise ValueError(f"No video stream found: {path}")
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"), None
    )
    duration_value = (payload.get("format") or {}).get("duration") or video.get(
        "duration"
    )
    if duration_value is None:
        raise ValueError(f"No duration found: {path}")
    duration_sec = float(duration_value)
    if duration_sec <= 0:
        raise ValueError(f"Non-positive duration for {path}: {duration_sec}")
    return MediaProbe(
        duration_sec=duration_sec,
        video_codec=str(video.get("codec_name") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        avg_fps=parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        audio_codec=str(audio.get("codec_name")) if audio else None,
    )


def validate_proxy(
    source: MediaProbe,
    proxy: MediaProbe,
    short_side: int,
    fps: float,
) -> None:
    errors: list[str] = []
    if proxy.video_codec != "h264":
        errors.append(f"video codec is {proxy.video_codec!r}, expected 'h264'")
    if min(proxy.width, proxy.height) != short_side:
        errors.append(
            f"short side is {min(proxy.width, proxy.height)}, expected {short_side}"
        )
    if abs(proxy.avg_fps - fps) > 0.05:
        errors.append(f"average fps is {proxy.avg_fps:.4f}, expected {fps:.4f}")
    duration_tolerance = max(2.0, source.duration_sec * 0.005)
    if abs(proxy.duration_sec - source.duration_sec) > duration_tolerance:
        errors.append(
            "duration differs by "
            f"{abs(proxy.duration_sec - source.duration_sec):.3f}s "
            f"(tolerance {duration_tolerance:.3f}s)"
        )
    if source.audio_codec and proxy.audio_codec != "aac":
        errors.append(
            f"audio codec is {proxy.audio_codec!r}, expected 'aac' for source audio"
        )
    if errors:
        raise ValueError("Invalid proxy: " + "; ".join(errors))


def scale_filter(short_side: int, fps: float) -> str:
    fps_text = f"{fps:g}"
    return (
        f"scale=w='if(gte(iw,ih),-2,{short_side})':"
        f"h='if(gte(iw,ih),{short_side},-2)',fps={fps_text}"
    )


def download_video(video_uid: str, args: argparse.Namespace) -> None:
    run(
        [
            args.ego4d_cli,
            "--output_directory",
            str(args.ego4d_root),
            "--datasets",
            args.ego4d_dataset,
            "--video_uids",
            video_uid,
            "--version",
            args.ego4d_version,
            "--aws_profile_name",
            args.aws_profile,
            "--no-metadata",
            "--yes",
        ],
        dry_run=args.dry_run,
    )


def ensure_source_video(
    video_uid: str,
    raw_path: Path,
    args: argparse.Namespace,
) -> MediaProbe | None:
    if raw_path.exists():
        try:
            return probe_media(raw_path, args.ffprobe_bin)
        except Exception:
            invalid_path = raw_path.with_suffix(
                raw_path.suffix + ".invalid-" + datetime.now().strftime("%Y%m%d%H%M%S")
            )
            print(f"Source probe failed; moving file to {invalid_path}", flush=True)
            if not args.dry_run:
                raw_path.replace(invalid_path)

    if args.skip_download:
        raise FileNotFoundError(f"Source video is missing or invalid: {raw_path}")
    download_video(video_uid, args)
    if args.dry_run and not raw_path.exists():
        return None
    if not raw_path.exists():
        raise FileNotFoundError(f"Ego4D CLI did not create expected video: {raw_path}")
    return probe_media(raw_path, args.ffprobe_bin)


def ensure_free_space(path: Path, min_free_gb: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(path).free
    required = int(min_free_gb * 1024**3)
    if free_bytes < required:
        raise RuntimeError(
            f"Insufficient free space at {path}: {free_bytes / 1024**3:.2f} GiB "
            f"available, {min_free_gb:.2f} GiB required"
        )


def transcode_proxy(
    raw_path: Path,
    proxy_path: Path,
    args: argparse.Namespace,
) -> None:
    ensure_free_space(proxy_path.parent, args.min_free_gb)
    tmp_path = proxy_path.with_suffix(".part.mp4")
    if tmp_path.exists() and not args.dry_run:
        tmp_path.unlink()
    cmd = [
        args.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(raw_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        scale_filter(args.short_side, args.fps),
        "-c:v",
        "libx264",
        "-preset",
        args.preset,
        "-crf",
        str(args.crf),
        "-pix_fmt",
        "yuv420p",
    ]
    if args.ffmpeg_threads > 0:
        cmd.extend(["-threads", str(args.ffmpeg_threads)])
    cmd.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(tmp_path),
        ]
    )
    try:
        run(cmd, dry_run=args.dry_run)
        if not args.dry_run:
            tmp_path.replace(proxy_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


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


def response_content_length(response: dict[str, Any]) -> int | None:
    for key, value in response.items():
        if key.lower().replace("-", "") == "contentlength":
            return int(value)
    return None


def upload_and_verify(
    client,
    cos: dict[str, str],
    path: Path,
    key: str,
    url_expire_days: int,
    dry_run: bool,
) -> tuple[str, bool]:
    bucket = cos["bucket"]
    if dry_run:
        print(f"Would upload {path} -> cos://{bucket}/{key}", flush=True)
        return "", False
    print(f"Uploading {path} -> cos://{bucket}/{key}", flush=True)
    client.upload_file(
        Bucket=bucket,
        Key=key,
        LocalFilePath=str(path),
        PartSize=8,
        MAXThread=8,
        EnableMD5=True,
    )
    head = client.head_object(Bucket=bucket, Key=key)
    remote_size = response_content_length(head)
    local_size = path.stat().st_size
    if remote_size is None:
        raise RuntimeError(f"COS HEAD response has no Content-Length for {key}")
    if remote_size != local_size:
        raise RuntimeError(
            f"COS size mismatch for {key}: local={local_size}, remote={remote_size}"
        )
    signed_url = client.get_presigned_url(
        Bucket=bucket,
        Key=key,
        Method="GET",
        Expired=url_expire_days * 24 * 3600,
    )
    if not signed_url:
        raise RuntimeError(f"COS client returned an empty signed URL for {key}")
    return signed_url, True


def content_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def delete_file(path: Path, label: str, dry_run: bool) -> bool:
    if not path.exists():
        return False
    print(f"Deleting {label}: {path}", flush=True)
    if dry_run:
        return False
    path.unlink()
    return True


def completed_video_uids(
    status_csv: Path,
    url_csv: Path,
    skip_upload: bool,
) -> set[str]:
    if not status_csv.exists():
        return set()
    ok = {
        row["video_uid"]
        for row in read_csv(status_csv)
        if row.get("status") == "ok" and row.get("video_uid")
    }
    if skip_upload:
        return ok
    if not url_csv.exists():
        return set()
    uploaded = {
        row["video_uid"]
        for row in read_csv(url_csv)
        if row.get("signed_url") and row.get("video_uid")
    }
    return ok & uploaded


def fmt_float(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def process_one(
    row: dict[str, str],
    args: argparse.Namespace,
    cos_client,
    cos: dict[str, str] | None,
) -> dict[str, str]:
    video_uid = safe_component(row["video_uid"].strip(), "video_uid")
    participant = safe_component(row["participant_id"].strip(), "participant_id")
    raw_path = args.ego4d_video_dir / f"{video_uid}.mp4"
    proxy_path = (
        args.proxy_root / participant / f"{video_uid}_{args.short_side}p{args.fps:g}.mp4"
    )
    cos_key = (
        f"{args.cos_prefix.strip('/')}/{participant}/proxy_{args.short_side}p{args.fps:g}/"
        f"{proxy_path.name}"
    )

    source_probe = ensure_source_video(video_uid, raw_path, args)
    raw_probe_ok = source_probe is not None
    if source_probe is None:
        source_duration = float(row.get("duration_sec") or 0)
    else:
        source_duration = source_probe.duration_sec

    if args.overwrite_proxy or not proxy_path.exists():
        transcode_proxy(raw_path, proxy_path, args)
    if args.dry_run and not proxy_path.exists():
        proxy_probe = None
    else:
        if not proxy_path.exists():
            raise FileNotFoundError(f"Proxy video missing after transcode: {proxy_path}")
        proxy_probe = probe_media(proxy_path, args.ffprobe_bin)
        if source_probe is None:
            raise RuntimeError("Source probe is unexpectedly unavailable")
        validate_proxy(source_probe, proxy_probe, args.short_side, args.fps)

    raw_size = str(raw_path.stat().st_size) if raw_path.exists() else ""
    proxy_size = str(proxy_path.stat().st_size) if proxy_path.exists() else ""
    proxy_duration = proxy_probe.duration_sec if proxy_probe else 0.0
    signed_url = ""
    cos_size_verified = False
    raw_deleted = False
    proxy_deleted = False

    if not args.skip_upload:
        if cos_client is None or cos is None:
            raise RuntimeError("COS client is not available")
        signed_url, cos_size_verified = upload_and_verify(
            cos_client,
            cos,
            proxy_path,
            cos_key,
            args.url_expire_days,
            args.dry_run,
        )
        url_row = {
            "dataset": "ego4d",
            "participant_id": participant,
            "fb_participant_id": row.get("fb_participant_id", ""),
            "video_uid": video_uid,
            "video_id": video_uid,
            "canonical_session_id": row.get("canonical_session_id") or video_uid,
            "duration_sec": fmt_float(source_duration),
            "source_duration_sec": fmt_float(source_duration),
            "scenarios": row.get("scenarios", ""),
            "has_audio": row.get("has_audio", ""),
            "cross_video_order_status": row.get("cross_video_order_status", ""),
            "cross_video_order_basis": row.get("cross_video_order_basis", ""),
            "temporal_evolution_eligible": row.get(
                "temporal_evolution_eligible", ""
            ),
            "benchmark_session_order": row.get("benchmark_session_order", ""),
            "benchmark_order_status": row.get("benchmark_order_status", ""),
            "benchmark_order_basis": row.get("benchmark_order_basis", ""),
            "benchmark_temporal_evolution_eligible": row.get(
                "benchmark_temporal_evolution_eligible", ""
            ),
            "local_path": str(proxy_path),
            "bucket": cos["bucket"],
            "region": cos["region"],
            "key": cos_key,
            "size_bytes": proxy_size,
            "content_type": content_type_for(proxy_path),
            "signed_url": signed_url,
        }
        if not args.dry_run:
            upsert_csv(args.url_csv, url_row, URL_FIELDS, ("video_uid", "key"))

        persisted = args.dry_run or (
            cos_size_verified
            and bool(signed_url)
            and args.url_csv.exists()
            and video_uid
            in {
                item.get("video_uid", "")
                for item in read_csv(args.url_csv)
                if item.get("signed_url")
            }
        )
        if not persisted:
            raise RuntimeError(f"Upload metadata was not persisted for {video_uid}")
        if args.delete_raw_after_upload:
            raw_deleted = delete_file(raw_path, "downloaded Ego4D video", args.dry_run)
        if args.delete_proxy_after_upload:
            proxy_deleted = delete_file(proxy_path, "local proxy video", args.dry_run)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "error": "",
        "participant_id": participant,
        "video_uid": video_uid,
        "raw_path": str(raw_path),
        "raw_size_bytes": raw_size,
        "raw_probe_ok": str(raw_probe_ok),
        "proxy_path": str(proxy_path),
        "proxy_size_bytes": proxy_size,
        "proxy_probe_ok": str(proxy_probe is not None),
        "source_duration_sec": fmt_float(source_duration),
        "proxy_duration_sec": fmt_float(proxy_duration) if proxy_probe else "",
        "cos_key": "" if args.skip_upload else cos_key,
        "cos_size_verified": str(cos_size_verified),
        "raw_deleted": str(raw_deleted),
        "proxy_deleted": str(proxy_deleted),
    }


def default_run_name(manifest: Path) -> str:
    value = f"{manifest.parent.name}_{manifest.stem}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--ego4d-root", default="/home/lighthouse/video-benchmark/data/ego4d"
    )
    parser.add_argument(
        "--ego4d-video-dir",
        help="Directory containing canonical video_540ss MP4 files; defaults to <ego4d-root>/v2/video_540ss.",
    )
    parser.add_argument("--ego4d-dataset", default="video_540ss")
    parser.add_argument("--ego4d-version", default="v2_1")
    parser.add_argument("--aws-profile", default="ego4d")
    parser.add_argument("--ego4d-cli")
    parser.add_argument("--data-root", default="/home/lighthouse/video-benchmark/data")
    parser.add_argument("--run-name")
    parser.add_argument("--cos-config", default="~/.cos.conf")
    parser.add_argument("--cos-prefix", default="video-benchmark/ego4d")
    parser.add_argument("--url-expire-days", type=int, default=30)
    parser.add_argument("--video-uids", help="Comma-separated subset from manifest")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--short-side", type=int, default=540)
    parser.add_argument("--fps", type=float, default=16.0)
    parser.add_argument("--crf", type=int, default=28)
    parser.add_argument("--preset", default="veryfast")
    parser.add_argument("--ffmpeg-threads", type=int, default=0)
    parser.add_argument("--ffmpeg-bin")
    parser.add_argument("--ffprobe-bin")
    parser.add_argument("--min-free-gb", type=float, default=10.0)
    parser.add_argument("--overwrite-proxy", action="store_true")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--delete-raw-after-upload", action="store_true")
    parser.add_argument("--delete-proxy-after-upload", action="store_true")
    parser.add_argument("--rerun-completed", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.short_side < 2 or args.short_side % 2:
        parser.error("--short-side must be a positive even integer")
    if args.fps <= 0:
        parser.error("--fps must be positive")
    if not 0 <= args.crf <= 51:
        parser.error("--crf must be between 0 and 51")
    if args.ffmpeg_threads < 0:
        parser.error("--ffmpeg-threads must be non-negative")
    if args.min_free_gb < 0:
        parser.error("--min-free-gb must be non-negative")
    if args.url_expire_days < 1:
        parser.error("--url-expire-days must be at least 1")
    if args.skip_upload and (
        args.delete_raw_after_upload or args.delete_proxy_after_upload
    ):
        parser.error("cleanup-after-upload flags cannot be used with --skip-upload")
    return args


def main() -> None:
    args = parse_args()
    args.manifest = Path(args.manifest)
    args.ego4d_root = Path(args.ego4d_root)
    args.ego4d_video_dir = (
        Path(args.ego4d_video_dir)
        if args.ego4d_video_dir
        else args.ego4d_root / "v2/video_540ss"
    )
    args.data_root = Path(args.data_root)
    args.proxy_root = args.data_root / "proxy/ego4d"
    args.cos_config = Path(args.cos_config).expanduser()
    args.run_name = args.run_name or default_run_name(args.manifest)
    args.status_csv = (
        args.data_root
        / "processed/ego4d_pipeline_runs"
        / f"{args.run_name}_status.csv"
    )
    profile_name = f"{args.short_side}p{args.fps:g}"
    args.url_csv = (
        args.data_root
        / "cos_urls"
        / f"{args.run_name}_proxy_{profile_name}_urls.csv"
    )
    if args.skip_download:
        args.ego4d_cli = (
            args.ego4d_cli
            or os.environ.get("EGO4D_CLI")
            or shutil.which("ego4d")
            or "ego4d"
        )
    else:
        args.ego4d_cli = resolve_binary("ego4d", args.ego4d_cli, "EGO4D_CLI")
    args.ffmpeg_bin = resolve_binary("ffmpeg", args.ffmpeg_bin, "FFMPEG_BIN")
    args.ffprobe_bin = resolve_binary("ffprobe", args.ffprobe_bin, "FFPROBE_BIN")

    video_uids = (
        {value.strip() for value in args.video_uids.split(",") if value.strip()}
        if args.video_uids
        else None
    )
    rows = select_rows(load_manifest(args.manifest), video_uids, args.limit)
    if video_uids is not None:
        missing = sorted(video_uids - {row["video_uid"] for row in rows})
        if missing:
            raise SystemExit(f"Requested video_uids are absent from manifest: {missing}")

    cos_client = None
    cos = None
    if not args.skip_upload:
        cos_client, cos = make_cos_client(args.cos_config)

    print(f"Manifest: {args.manifest}", flush=True)
    print(f"Ego4D video directory: {args.ego4d_video_dir}", flush=True)
    print(f"Selected videos: {len(rows)}", flush=True)
    print(f"Status CSV: {args.status_csv}", flush=True)
    print(f"URL CSV: {args.url_csv}", flush=True)
    print(
        "Cleanup: "
        f"raw={args.delete_raw_after_upload}, proxy={args.delete_proxy_after_upload}",
        flush=True,
    )

    completed = (
        set()
        if args.rerun_completed
        else completed_video_uids(args.status_csv, args.url_csv, args.skip_upload)
    )
    if completed:
        print(f"Already completed videos: {len(completed)}", flush=True)

    for index, row in enumerate(rows, start=1):
        video_uid = row["video_uid"]
        if video_uid in completed:
            print(
                f"\n[{index}/{len(rows)}] {video_uid} already completed; skipping.",
                flush=True,
            )
            continue
        print(f"\n[{index}/{len(rows)}] {video_uid}", flush=True)
        try:
            status = process_one(row, args, cos_client, cos)
        except Exception as exc:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": repr(exc),
                "participant_id": row.get("participant_id", ""),
                "video_uid": video_uid,
            }
            print(f"ERROR {video_uid}: {exc!r}", flush=True)
            if not args.dry_run:
                upsert_csv(args.status_csv, status, STATUS_FIELDS, ("video_uid",))
            if args.fail_fast:
                raise
            continue
        if not args.dry_run:
            upsert_csv(args.status_csv, status, STATUS_FIELDS, ("video_uid",))


if __name__ == "__main__":
    main()
