#!/usr/bin/env python3
"""Upload selected EPIC-KITCHENS files to Tencent COS and write signed URLs.

Run this on the server that has access to the video files and a configured
~/.cos.conf. The config must contain bucket, region, secret_id, and secret_key.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import mimetypes
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client


def read_cos_config(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser()
    parser.read(path)
    if "common" not in parser:
        raise SystemExit(f"Missing [common] section in {path}")
    common = parser["common"]
    required = ["secret_id", "secret_key", "bucket", "region"]
    missing = [key for key in required if not common.get(key)]
    if missing:
        raise SystemExit(f"Missing COS config keys in {path}: {', '.join(missing)}")
    return {
        "secret_id": common["secret_id"],
        "secret_key": common["secret_key"],
        "bucket": common["bucket"],
        "region": common["region"],
        "schema": common.get("schema", "https"),
    }


def content_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def upload_one(client: CosS3Client, bucket: str, local_path: Path, key: str) -> None:
    print(f"Uploading {local_path} -> cos://{bucket}/{key}", flush=True)
    client.upload_file(
        Bucket=bucket,
        Key=key,
        LocalFilePath=str(local_path),
        PartSize=8,
        MAXThread=8,
        EnableMD5=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="~/.cos.conf")
    parser.add_argument("--prefix", required=True, help="COS object key prefix")
    parser.add_argument("--url-expire-days", type=int, default=7)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("files", nargs="+")
    args = parser.parse_args()

    cos = read_cos_config(Path(args.config).expanduser())
    config = CosConfig(
        Region=cos["region"],
        SecretId=cos["secret_id"],
        SecretKey=cos["secret_key"],
        Scheme=cos["schema"],
    )
    client = CosS3Client(config)
    bucket = cos["bucket"]

    rows: list[dict[str, str]] = []
    prefix = args.prefix.strip("/")
    expire_seconds = args.url_expire_days * 24 * 3600
    for item in args.files:
        local_path = Path(item)
        if not local_path.is_file():
            raise SystemExit(f"Not a file: {local_path}")
        key = f"{prefix}/{local_path.name}"
        upload_one(client, bucket, local_path, key)
        url = client.get_presigned_url(
            Bucket=bucket,
            Key=key,
            Method="GET",
            Expired=expire_seconds,
        )
        rows.append(
            {
                "local_path": str(local_path),
                "bucket": bucket,
                "region": cos["region"],
                "key": key,
                "size_bytes": str(local_path.stat().st_size),
                "content_type": content_type_for(local_path),
                "signed_url": url,
            }
        )

    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "local_path",
                "bucket",
                "region",
                "key",
                "size_bytes",
                "content_type",
                "signed_url",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {output_csv}", flush=True)


if __name__ == "__main__":
    main()
