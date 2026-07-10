#!/usr/bin/env python3
"""Merge Bailian Batch verification results into session evidence candidates."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from scripts.qc_common import candidate_review_fingerprint
except ModuleNotFoundError:  # Direct execution via `python3 scripts/...`.
    from qc_common import candidate_review_fingerprint


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
VERDICTS = {"entailed", "contradicted", "insufficient"}
BLOCKING_FLAGS = {
    "long_term_overclaim",
    "affected_by_uncertainty",
    "unsupported_aggregation",
}
HUMAN_REVIEW_FIELDS = [
    "source_video_id",
    "participant_id",
    "candidate_id",
    "candidate_type",
    "claim",
    "qc_status",
    "review_fingerprint",
    "first_pass_verdict",
    "local_verdict",
    "quality_flags",
    "support_ranges",
    "corrected_claim",
    "reason",
    "human_decision",
    "approved_claim",
    "human_notes",
]


def extract_json_text(text: str) -> str:
    text = text.strip()
    match = FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    raise ValueError("No JSON object found in assistant content")


def parse_batch_output_line(line: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    custom_id = str(line.get("custom_id") or "")
    if not custom_id:
        raise ValueError("Batch output line has no custom_id")
    if line.get("error"):
        raise ValueError(f"Batch request {custom_id} failed: {line['error']}")
    response = line.get("response") or {}
    status_code = int(response.get("status_code") or 0)
    if status_code != 200:
        raise ValueError(f"Batch request {custom_id} returned HTTP {status_code}")
    body = response.get("body") or {}
    choices = body.get("choices") or []
    if not choices:
        raise ValueError(f"Batch request {custom_id} has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content") or message.get("reasoning")
    if not content:
        raise ValueError(f"Batch request {custom_id} has empty assistant content")
    return custom_id, json.loads(extract_json_text(str(content)))


def ranges_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        left_start = float(left["start_sec"])
        left_end = float(left["end_sec"])
        right_start = float(right["start_sec"])
        right_end = float(right["end_sec"])
    except (KeyError, TypeError, ValueError):
        return False
    if left_end <= left_start or right_end <= right_start:
        return False
    return max(left_start, right_start) < min(left_end, right_end)


def has_evidence_overlap(
    evidence_ranges: list[dict[str, Any]], support_ranges: list[dict[str, Any]]
) -> bool:
    return any(
        ranges_overlap(evidence, support)
        for evidence in evidence_ranges
        for support in support_ranges
    )


def id_map(items: list[dict[str, Any]], field: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        value = str(item.get(field) or "")
        if not value:
            raise ValueError(f"{label} has no {field}")
        if value in result:
            raise ValueError(f"Duplicate {label} id: {value}")
        result[value] = item
    return result


def local_review_item(
    source_video_id: str,
    participant_id: str,
    candidate: dict[str, Any],
    manifest_candidate: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    return {
        "record_id": f"{source_video_id}:{candidate['candidate_id']}",
        "source_video_id": source_video_id,
        "participant_id": participant_id,
        "candidate_id": candidate["candidate_id"],
        "candidate_type": candidate.get("type"),
        "claim": candidate.get("claim"),
        "observed_value": candidate.get("observed_value"),
        "supporting_window_ids": list(candidate.get("supporting_window_ids") or []),
        "support_ranges": list(manifest_candidate.get("support_ranges") or []),
        "quality_flags": list(candidate.get("quality_flags") or []),
        "first_pass_verdict": verification.get("verdict"),
        "first_pass_evidence_time_ranges": list(verification.get("evidence_time_ranges") or []),
        "first_pass_reason_codes": list(verification.get("reason_codes") or []),
        "first_pass_reason": verification.get("reason"),
        "corrected_claim": verification.get("corrected_claim"),
    }


def merge_source_verdicts(
    session: dict[str, Any],
    manifest: dict[str, Any],
    response: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_video_id = str(session.get("source_video_id") or session.get("session_id") or "")
    if not source_video_id:
        raise ValueError("Session has no source_video_id")
    if str(manifest.get("source_video_id") or "") != source_video_id:
        raise ValueError("Session and manifest source_video_id mismatch")
    if str(response.get("source_video_id") or "") != source_video_id:
        raise ValueError("Verifier response source_video_id mismatch")
    session_candidates = id_map(
        list(session.get("cross_session_evidence_candidates") or []),
        "candidate_id",
        "session candidate",
    )
    manifest_candidates = id_map(
        list(manifest.get("candidates") or []),
        "candidate_id",
        "manifest candidate",
    )
    verifications = id_map(
        list(response.get("verification_results") or []),
        "candidate_id",
        "verification result",
    )
    requested_ids = set(manifest_candidates)
    session_ids = set(session_candidates)
    if not requested_ids.issubset(session_ids) or set(verifications) != requested_ids:
        raise ValueError(
            "Candidate coverage mismatch: "
            f"session={sorted(session_ids)} manifest={sorted(manifest_candidates)} "
            f"response={sorted(verifications)}"
        )
    excluded_ids = session_ids - requested_ids
    invalid_excluded = [
        candidate_id
        for candidate_id in sorted(excluded_ids)
        if session_candidates[candidate_id].get("qc_status") != "schema_failed"
    ]
    if invalid_excluded:
        raise ValueError(
            "Candidate coverage mismatch: non-failed candidates were not requested: "
            + ", ".join(invalid_excluded)
        )

    participant_id = str(session.get("participant_id") or manifest.get("participant_id") or "")
    merged_candidates: list[dict[str, Any]] = []
    local_queue: list[dict[str, Any]] = []
    for candidate_id in [
        str(item.get("candidate_id"))
        for item in session.get("cross_session_evidence_candidates") or []
    ]:
        original = copy.deepcopy(session_candidates[candidate_id])
        if candidate_id not in requested_ids:
            original["qc_status"] = "schema_failed"
            original["usable_for_reference"] = False
            merged_candidates.append(original)
            continue
        manifest_candidate = manifest_candidates[candidate_id]
        verification = copy.deepcopy(verifications[candidate_id])
        verdict = str(verification.get("verdict") or "")
        if verdict not in VERDICTS:
            raise ValueError(f"Invalid verdict for {candidate_id}: {verdict}")
        flags = {str(value) for value in original.get("quality_flags") or []}
        support_ranges = list(manifest_candidate.get("support_ranges") or [])
        evidence_ranges = list(verification.get("evidence_time_ranges") or [])
        corrected_claim = verification.get("corrected_claim")
        overlap = has_evidence_overlap(evidence_ranges, support_ranges)
        if verdict == "entailed" and overlap and not (flags & BLOCKING_FLAGS) and not corrected_claim:
            qc_status = "verification_passed"
            usable = True
        elif verdict == "contradicted":
            qc_status = "verification_disputed"
            usable = False
        elif verdict == "insufficient" or (verdict == "entailed" and not overlap):
            qc_status = "verification_uncertain"
            usable = False
        else:
            qc_status = "human_review_required"
            usable = False
        original["first_pass_verification"] = verification
        original["support_ranges"] = support_ranges
        original["qc_status"] = qc_status
        original["usable_for_reference"] = usable
        original["verifier_model"] = manifest.get("model")
        merged_candidates.append(original)
        if qc_status in {"verification_disputed", "verification_uncertain"}:
            local_queue.append(
                local_review_item(
                    source_video_id,
                    participant_id,
                    original,
                    manifest_candidate,
                    verification,
                )
            )

    return (
        {
            "source_video_id": source_video_id,
            "session_id": session.get("session_id") or source_video_id,
            "participant_id": participant_id,
            "extractor_model": "qwen35-a3b",
            "verifier_model": manifest.get("model"),
            "pipeline_version": "v0.2",
            "candidates": merged_candidates,
        },
        local_queue,
    )


def merge_local_verdict(
    first_pass_record: dict[str, Any],
    manifest: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    merged = copy.deepcopy(first_pass_record)
    source_video_id = str(merged.get("source_video_id") or "")
    candidate_id = str(manifest.get("candidate_id") or "")
    if not source_video_id or not candidate_id:
        raise ValueError("Local manifest requires source_video_id and candidate_id")
    if str(manifest.get("source_video_id") or "") != source_video_id:
        raise ValueError("Local manifest source_video_id mismatch")
    if str(response.get("source_video_id") or "") != source_video_id:
        raise ValueError("Local verifier response source_video_id mismatch")
    candidates = id_map(list(merged.get("candidates") or []), "candidate_id", "QC candidate")
    if candidate_id not in candidates:
        raise ValueError(f"Local manifest candidate is missing from first pass: {candidate_id}")
    verifications = id_map(
        list(response.get("verification_results") or []),
        "candidate_id",
        "local verification result",
    )
    if set(verifications) != {candidate_id}:
        raise ValueError(
            f"Local verification coverage mismatch: expected={candidate_id} "
            f"response={sorted(verifications)}"
        )
    verification = copy.deepcopy(verifications[candidate_id])
    verdict = str(verification.get("verdict") or "")
    if verdict not in VERDICTS:
        raise ValueError(f"Invalid local verdict for {candidate_id}: {verdict}")
    candidate = candidates[candidate_id]
    flags = {str(value) for value in candidate.get("quality_flags") or []}
    support_ranges = list(manifest.get("support_ranges") or candidate.get("support_ranges") or [])
    evidence_ranges = list(verification.get("evidence_time_ranges") or [])
    overlap = has_evidence_overlap(evidence_ranges, support_ranges)
    corrected_claim = verification.get("corrected_claim")
    if verdict == "entailed" and overlap and not (flags & BLOCKING_FLAGS) and not corrected_claim:
        qc_status = "local_verification_passed"
        usable = True
    elif verdict == "contradicted" and overlap:
        qc_status = "local_verification_rejected"
        usable = False
    else:
        qc_status = "human_review_required"
        usable = False
    candidate["local_verification"] = verification
    candidate["local_verification_clip_ids"] = list(manifest.get("clip_ids") or [])
    candidate["local_verifier_model"] = manifest.get("model")
    candidate["qc_status"] = qc_status
    candidate["usable_for_reference"] = usable
    merged["candidates"] = [
        candidates[str(item.get("candidate_id"))]
        for item in first_pass_record.get("candidates") or []
    ]
    return merged


def build_quality_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [candidate for record in records for candidate in record.get("candidates") or []]
    status_counts = Counter(str(item.get("qc_status") or "unknown") for item in candidates)
    verdict_counts = Counter(
        str((item.get("first_pass_verification") or {}).get("verdict") or "missing")
        for item in candidates
    )
    type_counts = Counter(str(item.get("type") or "unknown") for item in candidates)
    flag_counts = Counter(
        str(flag) for item in candidates for flag in item.get("quality_flags") or []
    )
    participant_counts = Counter(
        str(record.get("participant_id") or "unknown")
        for record in records
        for _ in record.get("candidates") or []
    )
    total = len(candidates)
    return {
        "sources": len(records),
        "candidates": total,
        "qc_status_counts": dict(sorted(status_counts.items())),
        "first_pass_verdict_counts": dict(sorted(verdict_counts.items())),
        "candidate_type_counts": dict(sorted(type_counts.items())),
        "quality_flag_counts": dict(sorted(flag_counts.items())),
        "participant_candidate_counts": dict(sorted(participant_counts.items())),
        "automatic_pass_rate": (
            status_counts.get("verification_passed", 0) / total if total else 0.0
        ),
        "local_review_rate": (
            (
                status_counts.get("verification_disputed", 0)
                + status_counts.get("verification_uncertain", 0)
            )
            / total
            if total
            else 0.0
        ),
        "human_review_rate": (
            status_counts.get("human_review_required", 0) / total if total else 0.0
        ),
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def prune_merged_outputs(merged_dir: Path, current_source_ids: set[str]) -> None:
    if not merged_dir.exists():
        return
    expected_names = {f"{source_id}.qc.json" for source_id in current_source_ids}
    for path in merged_dir.glob("*.qc.json"):
        if path.name not in expected_names:
            path.unlink()


def reset_merge_outputs(output_dir: Path) -> Path:
    merged_dir = output_dir / "merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    prune_merged_outputs(merged_dir, set())
    for name in (
        "local_review_queue.jsonl",
        "retry_queue.jsonl",
        "quality_report.json",
        "human_review.csv",
    ):
        path = output_dir / name
        if path.exists():
            path.unlink()
    return merged_dir


def load_session_records(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        return [
            json.loads(item.read_text(encoding="utf-8"))
            for item in sorted(path.glob("*.clean.json"))
        ]
    return read_jsonl(path)


def human_review_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records:
        for candidate in record.get("candidates") or []:
            if candidate.get("qc_status") != "human_review_required":
                continue
            first_verification = candidate.get("first_pass_verification") or {}
            local_verification = candidate.get("local_verification") or {}
            review_verification = local_verification or first_verification
            rows.append(
                {
                    "source_video_id": str(record.get("source_video_id") or ""),
                    "participant_id": str(record.get("participant_id") or ""),
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "candidate_type": str(candidate.get("type") or ""),
                    "claim": str(candidate.get("claim") or ""),
                    "qc_status": str(candidate.get("qc_status") or ""),
                    "review_fingerprint": candidate_review_fingerprint(record, candidate),
                    "first_pass_verdict": str(first_verification.get("verdict") or ""),
                    "local_verdict": str(
                        (candidate.get("local_verification") or {}).get("verdict") or ""
                    ),
                    "quality_flags": json.dumps(candidate.get("quality_flags") or [], ensure_ascii=False),
                    "support_ranges": json.dumps(candidate.get("support_ranges") or [], ensure_ascii=False),
                    "corrected_claim": str(review_verification.get("corrected_claim") or ""),
                    "reason": str(review_verification.get("reason") or ""),
                    "human_decision": "",
                    "approved_claim": "",
                    "human_notes": "",
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    source = subparsers.add_parser("source")
    source.add_argument("--session-records", required=True)
    source.add_argument("--manifest-jsonl", required=True)
    source.add_argument("--batch-output-jsonl", required=True)
    source.add_argument("--output-dir", required=True)
    local = subparsers.add_parser("local")
    local.add_argument("--first-pass-dir", required=True)
    local.add_argument("--manifest-jsonl", required=True)
    local.add_argument("--batch-output-jsonl", required=True)
    local.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    merged_dir = reset_merge_outputs(output_dir)
    outputs: dict[str, dict[str, Any]] = {}
    for line in read_jsonl(Path(args.batch_output_jsonl)):
        custom_id, payload = parse_batch_output_line(line)
        if custom_id in outputs:
            raise ValueError(f"Duplicate Batch output custom_id: {custom_id}")
        outputs[custom_id] = payload
    merged_records: list[dict[str, Any]]
    retry_records: list[dict[str, Any]] = []
    local_queue: list[dict[str, Any]] = []
    if args.command == "source":
        sessions = id_map(
            load_session_records(Path(args.session_records)), "source_video_id", "session"
        )
        manifests = id_map(read_jsonl(Path(args.manifest_jsonl)), "source_video_id", "manifest")
        merged_records = []
        for current_source, manifest in sorted(manifests.items()):
            if current_source not in sessions:
                raise ValueError(f"Manifest has no session record: {current_source}")
            if manifest.get("request_skipped"):
                merged, queue = merge_source_verdicts(
                    sessions[current_source],
                    manifest,
                    {"source_video_id": current_source, "verification_results": []},
                )
                merged_records.append(merged)
                local_queue.extend(queue)
                continue
            if current_source not in outputs:
                retry_records.append(
                    {"source_video_id": current_source, "reason": "missing_batch_output"}
                )
                continue
            merged, queue = merge_source_verdicts(
                sessions[current_source], manifest, outputs[current_source]
            )
            merged_records.append(merged)
            local_queue.extend(queue)
    else:
        first_pass_records = id_map(
            [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(Path(args.first_pass_dir).glob("*.qc.json"))
            ],
            "source_video_id",
            "first-pass QC record",
        )
        manifests = id_map(read_jsonl(Path(args.manifest_jsonl)), "record_id", "local manifest")
        for record_id, manifest in sorted(manifests.items()):
            source_video_id = str(manifest.get("source_video_id") or "")
            if source_video_id not in first_pass_records:
                raise ValueError(f"Local manifest has no first-pass source: {source_video_id}")
            if record_id not in outputs:
                retry_records.append(
                    {"record_id": record_id, "source_video_id": source_video_id, "reason": "missing_batch_output"}
                )
                continue
            first_pass_records[source_video_id] = merge_local_verdict(
                first_pass_records[source_video_id], manifest, outputs[record_id]
            )
        merged_records = [first_pass_records[key] for key in sorted(first_pass_records)]

    if not retry_records:
        for merged in merged_records:
            (merged_dir / f"{merged['source_video_id']}.qc.json").write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    published_local_queue = [] if retry_records else local_queue
    write_jsonl(output_dir / "local_review_queue.jsonl", published_local_queue)
    write_jsonl(output_dir / "retry_queue.jsonl", retry_records)
    report = build_quality_report(merged_records)
    report["missing_outputs"] = len(retry_records)
    (output_dir / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rows = [] if retry_records else human_review_rows(merged_records)
    with (output_dir / "human_review.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HUMAN_REVIEW_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"Merged sources={len(merged_records)} candidates={report['candidates']} "
        f"local_review={len(local_queue)} human_review={len(rows)} missing={len(retry_records)}",
        flush=True,
    )
    if retry_records:
        raise RuntimeError(
            f"QC merge has {len(retry_records)} missing Batch outputs; process retry_queue.jsonl"
        )


if __name__ == "__main__":
    main()
