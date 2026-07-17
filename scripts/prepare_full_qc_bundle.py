#!/usr/bin/env python3
"""Build and validate the minimal handoff bundle for full-dataset QC."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import tarfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.build_bailian_qc_batch import source_id, source_video_id_from_proxy_row
    from scripts.run_hierarchical_extraction_participants import (
        discover_participant_manifests,
        participant_slug,
    )
except ModuleNotFoundError:  # Direct execution via `python3 scripts/...`.
    from build_bailian_qc_batch import source_id, source_video_id_from_proxy_row
    from run_hierarchical_extraction_participants import (
        discover_participant_manifests,
        participant_slug,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_index(
    records: list[dict[str, Any]],
    id_getter,
    label: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(id_getter(record) or "")
        if not record_id:
            raise ValueError(f"{label} record has no source id")
        if record_id in result:
            raise ValueError(f"Duplicate {label} source id: {record_id}")
        result[record_id] = record
    return result


def latest_participant_statuses(output_root: Path) -> tuple[dict[str, dict[str, str]], list[Path]]:
    status_files = sorted(output_root.glob("participant_pipeline_status*.csv"))
    if not status_files:
        raise FileNotFoundError(
            f"No participant_pipeline_status*.csv found in {output_root}"
        )
    latest: dict[str, dict[str, str]] = {}
    for path in status_files:
        for row in read_csv(path):
            participant = str(row.get("participant_id") or "").upper()
            if not participant:
                continue
            previous = latest.get(participant)
            if previous is None or str(row.get("updated_at") or "") > str(
                previous.get("updated_at") or ""
            ):
                latest[participant] = row
    return latest, status_files


def ensure_clean_output_dir(path: Path, overwrite: bool) -> None:
    if path.exists():
        if not overwrite:
            raise FileExistsError(f"Bundle directory already exists: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True)


def candidate_count(record: dict[str, Any]) -> int:
    candidates = record.get("cross_session_evidence_candidates")
    if not isinstance(candidates, list):
        raise ValueError(
            f"Session record {source_id(record)} has no cross_session_evidence_candidates list"
        )
    return len(candidates)


def ordered_field_union(rows: list[dict[str, str]]) -> list[str]:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    required_first = ["participant_id", "video_id", "key", "signed_url"]
    return [field for field in required_first if field in seen] + [
        field for field in fields if field not in required_first
    ]


def build_bundle(
    output_root: Path,
    manifest_dir: Path,
    bundle_dir: Path,
    expected_participants: int,
    overwrite: bool = False,
) -> dict[str, Any]:
    manifests = discover_participant_manifests(manifest_dir, "all")
    if len(manifests) != expected_participants:
        raise ValueError(
            f"Expected {expected_participants} participant manifests, found {len(manifests)}"
        )
    statuses, status_files = latest_participant_statuses(output_root)
    expected_participant_ids = {participant for participant, _ in manifests}
    missing_statuses = sorted(expected_participant_ids - set(statuses))
    if missing_statuses:
        raise ValueError(
            "Missing participant pipeline status: " + ", ".join(missing_statuses)
        )
    not_ok = {
        participant: statuses[participant].get("status", "")
        for participant in sorted(expected_participant_ids)
        if statuses[participant].get("status") != "ok"
    }
    if not_ok:
        detail = ", ".join(f"{key}={value}" for key, value in not_ok.items())
        raise ValueError(f"Participant pipeline is not complete: {detail}")

    ensure_clean_output_dir(bundle_dir, overwrite)
    session_records_dir = bundle_dir / "session_records"
    session_records_dir.mkdir()

    all_sessions: dict[str, dict[str, Any]] = {}
    all_inputs: dict[str, dict[str, Any]] = {}
    all_proxies: dict[str, dict[str, str]] = {}
    participant_reports: list[dict[str, Any]] = []
    proxy_fields: list[str] = []

    for participant, manifest_path in manifests:
        slug = participant_slug(participant)
        accepted_dir = (
            output_root / f"{slug}_qc" / "validation" / "session" / "accepted"
        )
        input_path = (
            output_root
            / f"{slug}_qc"
            / "hierarchical"
            / "session_inputs_30s_120s.jsonl"
        )
        if not accepted_dir.is_dir():
            raise FileNotFoundError(f"Missing accepted session directory: {accepted_dir}")
        if not input_path.is_file():
            raise FileNotFoundError(f"Missing session input JSONL: {input_path}")

        proxy_rows = read_csv(manifest_path)
        proxy_index = unique_index(
            proxy_rows, source_video_id_from_proxy_row, f"{participant} proxy"
        )
        input_rows = read_jsonl(input_path)
        input_index = unique_index(input_rows, source_id, f"{participant} session input")
        accepted_rows = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(accepted_dir.glob("*.clean.json"))
        ]
        accepted_index = unique_index(
            accepted_rows, source_id, f"{participant} accepted session"
        )

        proxy_ids = set(proxy_index)
        input_ids = set(input_index)
        accepted_ids = set(accepted_index)
        if proxy_ids != input_ids or proxy_ids != accepted_ids:
            raise ValueError(
                f"{participant} source mismatch: proxies={len(proxy_ids)} "
                f"inputs={len(input_ids)} accepted={len(accepted_ids)} "
                f"missing_inputs={sorted(proxy_ids - input_ids)[:10]} "
                f"missing_accepted={sorted(proxy_ids - accepted_ids)[:10]} "
                f"unexpected_inputs={sorted(input_ids - proxy_ids)[:10]} "
                f"unexpected_accepted={sorted(accepted_ids - proxy_ids)[:10]}"
            )

        participant_candidates = 0
        for current_source in sorted(proxy_ids):
            if current_source in all_sessions:
                raise ValueError(f"Duplicate source across participants: {current_source}")
            session = accepted_index[current_source]
            session_participant = str(session.get("participant_id") or participant)
            if session_participant.upper() != participant:
                raise ValueError(
                    f"Session participant mismatch for {current_source}: "
                    f"{session_participant} != {participant}"
                )
            participant_candidates += candidate_count(session)
            all_sessions[current_source] = session
            all_inputs[current_source] = input_index[current_source]
            proxy_row = dict(proxy_index[current_source])
            proxy_row["participant_id"] = proxy_row.get("participant_id") or participant
            proxy_row["video_id"] = proxy_row.get("video_id") or current_source
            if not proxy_row.get("key"):
                raise ValueError(f"Proxy row has no COS key: {current_source}")
            if not proxy_row.get("signed_url"):
                raise ValueError(f"Proxy row has no signed URL: {current_source}")
            all_proxies[current_source] = proxy_row
            for field in proxy_row:
                if field not in proxy_fields:
                    proxy_fields.append(field)

            target = session_records_dir / f"{current_source}.clean.json"
            target.write_text(
                json.dumps(session, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        participant_reports.append(
            {
                "participant_id": participant,
                "sources": len(proxy_ids),
                "candidates": participant_candidates,
            }
        )

    ordered_ids = sorted(all_sessions)
    session_inputs_path = bundle_dir / "session_inputs_30s_120s.jsonl"
    proxy_urls_path = bundle_dir / "proxy_540p16_urls.csv"
    write_jsonl(session_inputs_path, [all_inputs[source] for source in ordered_ids])
    proxy_rows = [all_proxies[source] for source in ordered_ids]
    write_csv(proxy_urls_path, proxy_rows, ordered_field_union(proxy_rows))

    normalized_status_rows = [
        statuses[participant] for participant in sorted(expected_participant_ids)
    ]
    status_fields = list(normalized_status_rows[0])
    write_csv(bundle_dir / "participant_pipeline_status.csv", normalized_status_rows, status_fields)

    report = {
        "created_at": utc_now(),
        "output_root": str(output_root),
        "manifest_dir": str(manifest_dir),
        "participants": len(manifests),
        "sources": len(all_sessions),
        "candidates": sum(candidate_count(record) for record in all_sessions.values()),
        "participant_status_counts": dict(
            sorted(Counter(row.get("status") or "unknown" for row in normalized_status_rows).items())
        ),
        "participant_reports": participant_reports,
        "status_files": [str(path) for path in status_files],
        "files": {
            "session_inputs_30s_120s.jsonl": {
                "rows": len(all_inputs),
                "sha256": sha256_file(session_inputs_path),
            },
            "proxy_540p16_urls.csv": {
                "rows": len(all_proxies),
                "sha256": sha256_file(proxy_urls_path),
            },
            "session_records": {
                "files": len(all_sessions),
            },
        },
    }
    (bundle_dir / "bundle_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def create_archive(bundle_dir: Path, archive_path: Path, overwrite: bool) -> None:
    if archive_path.exists() and not overwrite:
        raise FileExistsError(f"Archive already exists: {archive_path}")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = archive_path.with_name(archive_path.name + ".part")
    if temp_path.exists():
        temp_path.unlink()
    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            archive.add(bundle_dir, arcname=bundle_dir.name)
        temp_path.replace(archive_path)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--archive")
    parser.add_argument("--expected-participants", type=int, default=37)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    bundle_dir = Path(args.bundle_dir)
    report = build_bundle(
        Path(args.output_root),
        Path(args.manifest_dir),
        bundle_dir,
        args.expected_participants,
        args.overwrite,
    )
    if args.archive:
        create_archive(bundle_dir, Path(args.archive), args.overwrite)
    print(
        f"QC bundle ready: participants={report['participants']} "
        f"sources={report['sources']} candidates={report['candidates']} "
        f"dir={bundle_dir}",
        flush=True,
    )
    if args.archive:
        print(f"Archive: {args.archive}", flush=True)


if __name__ == "__main__":
    main()
