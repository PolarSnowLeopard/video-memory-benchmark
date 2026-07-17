#!/usr/bin/env python3
"""Refresh COS GET signatures in an existing proxy-video CSV without uploading."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

try:
    from scripts.run_epic_vpn_batch import make_cos_client
except ModuleNotFoundError:  # Direct execution via `python3 scripts/...`.
    from run_epic_vpn_batch import make_cos_client


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)
    temp_path.replace(path)


def refresh_rows(
    rows: list[dict[str, str]],
    client: Any,
    default_bucket: str,
    default_region: str,
    expire_seconds: int,
) -> list[dict[str, str]]:
    refreshed: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row in rows:
        current = dict(row)
        key = str(current.get("key") or "")
        bucket = str(current.get("bucket") or default_bucket)
        region = str(current.get("region") or default_region)
        if not key:
            raise ValueError(
                f"Proxy row has no COS key: {current.get('video_id') or current.get('source_video_id')}"
            )
        if not bucket:
            raise ValueError(f"Proxy row has no bucket: {key}")
        if region and default_region and region != default_region:
            raise ValueError(
                f"Proxy row region differs from COS config: {key}: {region} != {default_region}"
            )
        identity = (bucket, key)
        if identity in seen_keys:
            raise ValueError(f"Duplicate COS object in proxy CSV: {bucket}/{key}")
        seen_keys.add(identity)
        current["bucket"] = bucket
        current["region"] = region or default_region
        current["signed_url"] = client.get_presigned_url(
            Bucket=bucket,
            Key=key,
            Method="GET",
            Expired=expire_seconds,
        )
        if not current["signed_url"]:
            raise RuntimeError(f"COS returned an empty signed URL: {bucket}/{key}")
        refreshed.append(current)
    return refreshed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--cos-config", default="~/.cos.conf")
    parser.add_argument("--url-expire-days", type=int, default=21)
    args = parser.parse_args()
    if args.url_expire_days < 1:
        parser.error("--url-expire-days must be >= 1")

    input_path = Path(args.input_csv)
    output_path = Path(args.output_csv)
    fields, rows = read_csv(input_path)
    if not rows:
        raise ValueError(f"Proxy CSV has no rows: {input_path}")
    for field in ("bucket", "region", "key", "signed_url"):
        if field not in fields:
            fields.append(field)
    client, cos = make_cos_client(Path(args.cos_config).expanduser())
    refreshed = refresh_rows(
        rows,
        client,
        cos["bucket"],
        cos["region"],
        args.url_expire_days * 24 * 3600,
    )
    write_csv(output_path, fields, refreshed)
    print(
        f"Refreshed {len(refreshed)} signed URLs for {args.url_expire_days} days "
        f"-> {output_path}",
        flush=True,
    )


if __name__ == "__main__":
    main()
