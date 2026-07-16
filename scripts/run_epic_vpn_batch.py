#!/usr/bin/env python3
"""Download, transcode, and upload EPIC-KITCHENS videos on the vpn server."""

from __future__ import annotations

import argparse
import configparser
import csv
import hashlib
import mimetypes
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_PROXY_DURATION_ERROR_SEC = 1.0
FFMPEG_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):([0-5]\d):([0-5]\d(?:\.\d+)?)"
)


STATUS_FIELDS = [
    "updated_at",
    "status",
    "error",
    "participant_id",
    "video_id",
    "raw_path",
    "raw_size_bytes",
    "raw_md5_ok",
    "proxy_path",
    "proxy_size_bytes",
    "proxy_duration_sec",
    "proxy_duration_error_sec",
    "proxy_duration_validated",
    "cos_key",
    "cos_size_verified",
    "raw_deleted",
    "proxy_deleted",
]

URL_FIELDS = [
    "participant_id",
    "video_id",
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


def md5sum(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_epic55_splits(downloader_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in read_csv(downloader_dir / "data/epic_55_splits.csv"):
        out[row["video_id"]] = row["split"]
    return out


def load_md5(downloader_dir: Path) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for row in read_csv(downloader_dir / "data/md5.csv"):
        out[(row["version"], row["file_remote_path"])] = row["md5"]
    return out


def remote_key(video_id: str, epic55_splits: dict[str, str]) -> tuple[str, str]:
    participant = video_id.split("_", 1)[0]
    suffix = video_id.split("_", 1)[1]
    if len(suffix) == 3:
        return "100", f"{participant}/videos/{video_id}.MP4"
    split = epic55_splits[video_id]
    return "55", f"videos/{split}/{participant}/{video_id}.MP4"


def check_raw_md5(path: Path, video_id: str, epic55_splits: dict[str, str], md5s: dict[tuple[str, str], str]) -> bool | None:
    version, key = remote_key(video_id, epic55_splits)
    expected = md5s.get((version, key))
    if not expected:
        return None
    return md5sum(path) == expected


def run(cmd: list[str], cwd: Path | None = None, dry_run: bool = False) -> None:
    printable = " ".join(cmd)
    print(f"$ {printable}", flush=True)
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def parse_ffmpeg_duration(output: str) -> float:
    match = FFMPEG_DURATION_RE.search(output)
    if match is None:
        raise ValueError("Could not parse container duration from ffmpeg output")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def media_duration(path: Path, ffmpeg_bin: str = "ffmpeg") -> float:
    result = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    return parse_ffmpeg_duration(result.stderr)


def validate_proxy_duration(actual_sec: float, expected_sec: float, max_error_sec: float) -> float:
    error_sec = abs(actual_sec - expected_sec)
    if error_sec > max_error_sec:
        raise RuntimeError(
            "Proxy duration mismatch: "
            f"expected={expected_sec:.3f}s actual={actual_sec:.3f}s "
            f"error={error_sec:.3f}s limit={max_error_sec:.3f}s"
        )
    return error_sec


def download_video(
    python: str,
    downloader_dir: Path,
    raw_root: Path,
    video_id: str,
    dry_run: bool,
) -> None:
    run(
        [
            python,
            "epic_downloader.py",
            "--videos",
            "--specific-videos",
            video_id,
            "--output-path",
            str(raw_root),
        ],
        cwd=downloader_dir,
        dry_run=dry_run,
    )


def transcode_proxy(raw_path: Path, proxy_path: Path, ffmpeg_threads: int, dry_run: bool) -> None:
    proxy_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = proxy_path.with_name(f"{proxy_path.stem}.part{proxy_path.suffix}")
    temp_path.unlink(missing_ok=True)
    cmd = [
        "ffmpeg",
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
        "scale=-2:540,fps=16",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
    ]
    if ffmpeg_threads > 0:
        cmd.extend(["-threads", str(ffmpeg_threads)])
    cmd.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
    )
    try:
        run(cmd, dry_run=dry_run)
        if not dry_run:
            temp_path.replace(proxy_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
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


def content_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def response_content_length(response: dict[str, Any]) -> int | None:
    for key, value in response.items():
        if key.lower().replace("-", "") == "contentlength":
            return int(value)
    return None


def upload_proxy(
    client,
    cos: dict[str, str],
    path: Path,
    key: str,
    url_expire_days: int,
    dry_run: bool,
) -> tuple[str, bool]:
    bucket = cos["bucket"]
    if not dry_run:
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
    print(f"Would upload {path} -> cos://{bucket}/{key}", flush=True)
    return "", False


def delete_file(path: Path, label: str, dry_run: bool) -> bool:
    if not path.exists():
        return False
    print(f"Deleting {label}: {path}", flush=True)
    if dry_run:
        return False
    path.unlink()
    return True


def select_rows(rows: list[dict[str, str]], video_ids: set[str] | None, limit: int | None) -> list[dict[str, str]]:
    selected = [row for row in rows if video_ids is None or row["video_id"] in video_ids]
    return selected[:limit] if limit is not None else selected


def completed_video_ids(status_csv: Path, url_csv: Path, skip_upload: bool) -> set[str]:
    if not status_csv.exists():
        return set()
    ok_ids = {row["video_id"] for row in read_csv(status_csv) if row.get("status") == "ok" and row.get("video_id")}
    if skip_upload:
        return ok_ids
    if not url_csv.exists():
        return set()
    uploaded_ids = {row["video_id"] for row in read_csv(url_csv) if row.get("signed_url") and row.get("video_id")}
    return ok_ids & uploaded_ids


def process_one(
    row: dict[str, str],
    args: argparse.Namespace,
    epic55_splits: dict[str, str],
    md5s: dict[tuple[str, str], str],
    cos_client,
    cos: dict[str, str] | None,
) -> dict[str, str]:
    video_id = row["video_id"]
    participant = row.get("participant_id") or video_id.split("_", 1)[0]
    raw_path = args.raw_root / "EPIC-KITCHENS" / participant / "videos" / f"{video_id}.MP4"
    proxy_path = args.proxy_root / participant / f"{video_id}_540p16.mp4"
    cos_key = f"{args.cos_prefix.strip('/')}/{participant}/proxy_540p16/{proxy_path.name}"

    if not raw_path.exists():
        download_video(args.python, args.downloader_dir, args.raw_root, video_id, args.dry_run)
    if not args.dry_run and not raw_path.exists():
        raise RuntimeError(f"raw video missing after download: {raw_path}")

    raw_md5_ok: bool | None = None
    if raw_path.exists():
        raw_md5_ok = check_raw_md5(raw_path, video_id, epic55_splits, md5s)
        if raw_md5_ok is False:
            bad_path = raw_path.with_suffix(raw_path.suffix + ".bad")
            raw_path.rename(bad_path)
            print(f"MD5 mismatch; moved bad file to {bad_path}", flush=True)
            download_video(args.python, args.downloader_dir, args.raw_root, video_id, args.dry_run)
            raw_md5_ok = check_raw_md5(raw_path, video_id, epic55_splits, md5s)
            if raw_md5_ok is False:
                raise RuntimeError(f"raw MD5 still mismatched after redownload: {raw_path}")

    if args.overwrite_proxy or not proxy_path.exists():
        transcode_proxy(raw_path, proxy_path, args.ffmpeg_threads, args.dry_run)
    if not args.dry_run and not proxy_path.exists():
        raise RuntimeError(f"proxy video missing after transcode: {proxy_path}")

    proxy_duration_sec: float | None = None
    proxy_duration_error_sec: float | None = None
    if not args.dry_run:
        expected_duration_sec = float(row.get("duration_sec") or 0)
        if expected_duration_sec <= 0:
            raise RuntimeError(f"Missing expected duration for proxy validation: {video_id}")
        proxy_duration_sec = media_duration(proxy_path)
        proxy_duration_error_sec = validate_proxy_duration(
            proxy_duration_sec,
            expected_duration_sec,
            args.max_proxy_duration_error_sec,
        )

    raw_size_bytes = str(raw_path.stat().st_size if raw_path.exists() else "")
    proxy_size_bytes = str(proxy_path.stat().st_size if proxy_path.exists() else "")
    raw_deleted = False
    proxy_deleted = False

    signed_url = ""
    cos_size_verified = False
    if not args.skip_upload:
        if cos_client is None or cos is None:
            raise RuntimeError("COS client is not available")
        signed_url, cos_size_verified = upload_proxy(
            cos_client,
            cos,
            proxy_path,
            cos_key,
            args.url_expire_days,
            args.dry_run,
        )
        url_row = {
            "participant_id": participant,
            "video_id": video_id,
            "local_path": str(proxy_path),
            "bucket": cos["bucket"],
            "region": cos["region"],
            "key": cos_key,
            "size_bytes": proxy_size_bytes,
            "content_type": content_type_for(proxy_path),
            "signed_url": signed_url,
        }
        if not args.dry_run:
            upsert_csv(args.url_csv, url_row, URL_FIELDS, ("video_id", "key"))
        if signed_url or args.dry_run:
            if args.delete_raw_after_upload:
                raw_deleted = delete_file(raw_path, "raw video", args.dry_run)
            if args.delete_proxy_after_upload:
                proxy_deleted = delete_file(proxy_path, "proxy video", args.dry_run)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "error": "",
        "participant_id": participant,
        "video_id": video_id,
        "raw_path": str(raw_path),
        "raw_size_bytes": raw_size_bytes,
        "raw_md5_ok": "" if raw_md5_ok is None else str(raw_md5_ok),
        "proxy_path": str(proxy_path),
        "proxy_size_bytes": proxy_size_bytes,
        "proxy_duration_sec": "" if proxy_duration_sec is None else f"{proxy_duration_sec:.3f}",
        "proxy_duration_error_sec": "" if proxy_duration_error_sec is None else f"{proxy_duration_error_sec:.3f}",
        "proxy_duration_validated": str(proxy_duration_sec is not None),
        "cos_key": "" if args.skip_upload else cos_key,
        "cos_size_verified": str(cos_size_verified),
        "raw_deleted": str(raw_deleted),
        "proxy_deleted": str(proxy_deleted),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--data-root", default="/home/lighthouse/video-benchmark/data")
    parser.add_argument("--downloader-dir", default="/home/lighthouse/video-benchmark/data/external/epic-kitchens-download-scripts-100")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--cos-config", default="~/.cos.conf")
    parser.add_argument("--cos-prefix", default="video-benchmark/epic-kitchens")
    parser.add_argument("--url-expire-days", type=int, default=30)
    parser.add_argument("--video-ids", help="Comma-separated subset from manifest")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite-proxy", action="store_true")
    parser.add_argument(
        "--max-proxy-duration-error-sec",
        type=float,
        default=DEFAULT_MAX_PROXY_DURATION_ERROR_SEC,
    )
    parser.add_argument("--ffmpeg-threads", type=int, default=0, help="Threads per ffmpeg transcode. 0 lets ffmpeg choose.")
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument(
        "--delete-raw-after-upload",
        action="store_true",
        help="Delete the original MP4 only after proxy upload and URL CSV update succeed.",
    )
    parser.add_argument(
        "--delete-proxy-after-upload",
        action="store_true",
        help="Delete the transcoded proxy only after proxy upload and URL CSV update succeed.",
    )
    parser.add_argument("--rerun-completed", action="store_true", help="Reprocess videos already marked ok in status and URL CSVs.")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    args.manifest = Path(args.manifest)
    if args.ffmpeg_threads < 0:
        raise SystemExit("--ffmpeg-threads must be >= 0")
    if args.max_proxy_duration_error_sec < 0:
        raise SystemExit("--max-proxy-duration-error-sec must be >= 0")
    args.data_root = Path(args.data_root)
    args.raw_root = args.data_root / "raw"
    args.proxy_root = args.data_root / "proxy"
    args.downloader_dir = Path(args.downloader_dir)
    args.cos_config = Path(args.cos_config).expanduser()
    manifest_stem = args.manifest.stem
    args.status_csv = args.data_root / "processed/epic_pipeline_runs" / f"{manifest_stem}_status.csv"
    args.url_csv = args.data_root / "cos_urls" / f"{manifest_stem}_proxy_540p16_urls.csv"

    video_ids = {v.strip() for v in args.video_ids.split(",") if v.strip()} if args.video_ids else None
    rows = select_rows(read_csv(args.manifest), video_ids, args.limit)
    epic55_splits = load_epic55_splits(args.downloader_dir)
    md5s = load_md5(args.downloader_dir)

    cos_client = None
    cos = None
    if not args.skip_upload:
        cos_client, cos = make_cos_client(args.cos_config)

    print(f"Manifest: {args.manifest}", flush=True)
    print(f"Selected videos: {len(rows)}", flush=True)
    print(f"Status CSV: {args.status_csv}", flush=True)
    print(f"URL CSV: {args.url_csv}", flush=True)
    completed = set() if args.rerun_completed else completed_video_ids(args.status_csv, args.url_csv, args.skip_upload)
    if completed:
        print(f"Already completed videos: {len(completed)}", flush=True)

    for idx, row in enumerate(rows, start=1):
        video_id = row["video_id"]
        if video_id in completed:
            print(f"\n[{idx}/{len(rows)}] {video_id} already completed; skipping.", flush=True)
            continue
        print(f"\n[{idx}/{len(rows)}] {video_id}", flush=True)
        try:
            status = process_one(row, args, epic55_splits, md5s, cos_client, cos)
        except Exception as exc:
            status = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "status": "error",
                "error": repr(exc),
                "participant_id": row.get("participant_id", video_id.split("_", 1)[0]),
                "video_id": video_id,
                "raw_path": "",
                "raw_size_bytes": "",
                "raw_md5_ok": "",
                "proxy_path": "",
                "proxy_size_bytes": "",
                "proxy_duration_sec": "",
                "proxy_duration_error_sec": "",
                "proxy_duration_validated": "False",
                "cos_key": "",
                "cos_size_verified": "False",
                "raw_deleted": "",
                "proxy_deleted": "",
            }
            print(f"ERROR {video_id}: {exc!r}", flush=True)
            if args.fail_fast:
                if not args.dry_run:
                    upsert_csv(args.status_csv, status, STATUS_FIELDS, ("video_id",))
                raise
        if not args.dry_run:
            upsert_csv(args.status_csv, status, STATUS_FIELDS, ("video_id",))


if __name__ == "__main__":
    main()
