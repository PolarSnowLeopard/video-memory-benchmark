#!/usr/bin/env python3
"""Finalize QC candidates into reference-ready, rejected, and unresolved outputs."""

from __future__ import annotations

import argparse
import copy
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


ACCEPT_DECISIONS = {"accept", "accept_original", "接受", "接受原事实"}
ACCEPT_CORRECTED_DECISIONS = {"accept_corrected", "接受修正"}
REJECT_DECISIONS = {"reject", "拒绝"}
AUTO_READY_STATUSES = {"verification_passed", "local_verification_passed"}
AUTO_REJECTED_STATUSES = {"local_verification_rejected", "human_rejected"}


def human_key(row: dict[str, Any]) -> str:
    return f"{row.get('source_video_id')}:{row.get('candidate_id')}"


def human_decision_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = human_key(row)
        if key in result:
            raise ValueError(f"Duplicate human review row: {key}")
        decision = str(row.get("human_decision") or "").strip()
        if decision and decision not in (
            ACCEPT_DECISIONS | ACCEPT_CORRECTED_DECISIONS | REJECT_DECISIONS
        ):
            raise ValueError(f"Invalid human_decision for {key}: {decision}")
        result[key] = row
    return result


def output_candidate(record: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    item = copy.deepcopy(candidate)
    item.update(
        {
            "reference_id": f"{record.get('source_video_id')}:{candidate.get('candidate_id')}",
            "source_video_id": record.get("source_video_id"),
            "session_id": record.get("session_id") or record.get("source_video_id"),
            "participant_id": record.get("participant_id"),
            "pipeline_version": record.get("pipeline_version", "v0.2"),
        }
    )
    return item


def finalize_candidates(
    qc_records: list[dict[str, Any]], human_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    decisions = human_decision_map(human_rows)
    ready: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in sorted(qc_records, key=lambda value: str(value.get("source_video_id") or "")):
        source_video_id = str(record.get("source_video_id") or "")
        for candidate in record.get("candidates") or []:
            candidate_id = str(candidate.get("candidate_id") or "")
            key = f"{source_video_id}:{candidate_id}"
            if not source_video_id or not candidate_id:
                raise ValueError("QC candidate requires source_video_id and candidate_id")
            if key in seen:
                raise ValueError(f"Duplicate QC candidate: {key}")
            seen.add(key)
            item = output_candidate(record, candidate)
            human = decisions.get(key, {})
            decision = str(human.get("human_decision") or "").strip()
            if decision in ACCEPT_DECISIONS:
                item["qc_status"] = "human_accepted"
                item["usable_for_reference"] = True
                item["human_notes"] = human.get("human_notes") or ""
                ready.append(item)
            elif decision in ACCEPT_CORRECTED_DECISIONS:
                approved_claim = str(human.get("approved_claim") or "").strip()
                if not approved_claim:
                    raise ValueError(f"accept_corrected requires approved_claim: {key}")
                item["original_claim"] = item.get("claim")
                item["claim"] = approved_claim
                item["qc_status"] = "human_accepted"
                item["usable_for_reference"] = True
                item["human_notes"] = human.get("human_notes") or ""
                ready.append(item)
            elif decision in REJECT_DECISIONS:
                item["qc_status"] = "human_rejected"
                item["usable_for_reference"] = False
                item["human_notes"] = human.get("human_notes") or ""
                rejected.append(item)
            elif item.get("qc_status") in AUTO_READY_STATUSES and item.get(
                "usable_for_reference"
            ) is True:
                ready.append(item)
            elif item.get("qc_status") in AUTO_REJECTED_STATUSES:
                rejected.append(item)
            else:
                unresolved.append(item)
    return ready, rejected, unresolved


def read_qc_records(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        return [
            json.loads(item.read_text(encoding="utf-8"))
            for item in sorted(path.glob("*.qc.json"))
        ]
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def read_human_rows(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qc-records", required=True, help="Directory of .qc.json files or JSONL.")
    parser.add_argument("--human-review-csv")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    qc_records = read_qc_records(Path(args.qc_records))
    human_rows = read_human_rows(Path(args.human_review_csv) if args.human_review_csv else None)
    ready, rejected, unresolved = finalize_candidates(qc_records, human_rows)
    output_dir = Path(args.output_dir)
    write_jsonl(output_dir / "reference_ready.jsonl", ready)
    write_jsonl(output_dir / "rejected.jsonl", rejected)
    write_jsonl(output_dir / "unresolved.jsonl", unresolved)
    report = {
        "candidates": len(ready) + len(rejected) + len(unresolved),
        "reference_ready": len(ready),
        "rejected": len(rejected),
        "unresolved": len(unresolved),
        "ready_by_type": dict(sorted(Counter(str(item.get("type") or "unknown") for item in ready).items())),
    }
    (output_dir / "finalization_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
