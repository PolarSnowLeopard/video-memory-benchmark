#!/usr/bin/env python3
"""Build a local HTML viewer from COS signed-url CSV files."""

from __future__ import annotations

import argparse
import csv
import html
from pathlib import Path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def by_stem(rows: list[dict[str, str]], suffixes: tuple[str, ...]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        name = Path(row["key"]).name
        stem = name
        for suffix in suffixes:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        out[stem] = row
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-csv", required=True)
    parser.add_argument("--raw-csv", required=True)
    parser.add_argument("--contact-csv", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    proxy_rows = read_rows(Path(args.proxy_csv))
    raw_rows = read_rows(Path(args.raw_csv))
    contact_rows = read_rows(Path(args.contact_csv))

    proxy = by_stem(proxy_rows, ("_540p.mp4",))
    raw = by_stem(raw_rows, (".MP4",))
    contact = by_stem(contact_rows, ("_contact.jpg",))

    ids = sorted(set(proxy) | set(raw) | set(contact))
    blocks = []
    for video_id in ids:
        proxy_url = proxy.get(video_id, {}).get("signed_url", "")
        raw_url = raw.get(video_id, {}).get("signed_url", "")
        contact_url = contact.get(video_id, {}).get("signed_url", "")
        parts = [f"<section><h2>{html.escape(video_id)}</h2>"]
        if contact_url:
            parts.append(f'<img src="{html.escape(contact_url)}" alt="{html.escape(video_id)} contact sheet">')
        if proxy_url:
            parts.append(
                f'<video controls preload="metadata" src="{html.escape(proxy_url)}"></video>'
            )
        links = []
        if proxy_url:
            links.append(f'<a href="{html.escape(proxy_url)}" target="_blank">打开 540p 视频</a>')
        if raw_url:
            links.append(f'<a href="{html.escape(raw_url)}" target="_blank">打开原始视频</a>')
        if links:
            parts.append("<p>" + " | ".join(links) + "</p>")
        parts.append("</section>")
        blocks.append("\n".join(parts))

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>P04 COS 视频索引</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 24px; background: #f7f7f7; color: #171717; }}
    h1 {{ font-size: 24px; margin: 0 0 16px; }}
    h2 {{ font-size: 18px; margin: 0 0 12px; }}
    section {{ background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 16px; margin: 0 0 20px; }}
    img, video {{ display: block; width: 100%; max-width: 1200px; margin: 8px 0 12px; background: #000; }}
    video {{ aspect-ratio: 16 / 9; }}
    a {{ color: #075bb5; }}
  </style>
</head>
<body>
  <h1>P04 COS 视频索引</h1>
  {"".join(blocks)}
</body>
</html>
"""
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
