#!/usr/bin/env python3
"""Validate and normalize hierarchical video evidence without changing originals."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


CONFIDENCES = {"high", "medium", "low"}
LONG_TERM_RE = re.compile(r"通常|习惯|经常|总是|长期|偏好|喜欢|always|usually|typically", re.IGNORECASE)
RELATIVE_TIME_RE = re.compile(r"^(\d+):([0-5]\d(?:\.\d+)?)$")

REQUIRED_FIELDS = {
    "micro": [
        "clip_id",
        "source_video_id",
        "clip_time_range",
        "clip_summary",
        "places",
        "objects",
        "atomic_events",
        "state_observations",
        "state_changes",
        "end_state",
        "uncertainties",
    ],
    "window": [
        "window_id",
        "source_video_id",
        "time_range",
        "window_summary",
        "entity_map",
        "local_event_chain",
        "state_changes",
        "window_end_state",
        "open_threads",
        "evidence_facts",
        "conflicts_or_uncertainties",
    ],
    "session": [
        "session_id",
        "source_video_id",
        "participant_id",
        "time_range",
        "session_summary",
        "session_timeline",
        "session_entities",
        "state_update_timeline",
        "session_final_state",
        "open_tasks_or_unresolved_states",
        "cross_session_evidence_candidates",
        "contradictions_or_uncertainties",
    ],
}

COUNT_LIMITS = {
    "micro": {
        "places": 3,
        "objects": 6,
        "atomic_events": 4,
        "state_observations": 6,
        "state_changes": 4,
        "end_state": 5,
        "uncertainties": 3,
    },
    "window": {
        "entity_map": 10,
        "local_event_chain": 8,
        "state_changes": 6,
        "window_end_state": 8,
        "open_threads": 4,
        "evidence_facts": 8,
        "conflicts_or_uncertainties": 5,
    },
    "session": {
        "session_timeline": 12,
        "session_entities": 20,
        "state_update_timeline": 16,
        "session_final_state": 12,
        "open_tasks_or_unresolved_states": 8,
        "cross_session_evidence_candidates": 12,
        "contradictions_or_uncertainties": 8,
    },
}

ISSUE_FIELDS = ["layer", "record_id", "candidate_id", "severity", "code", "message"]
REFERENCE_ISSUE_CODES = {
    "unknown_candidate_reference",
    "unknown_clip_reference",
    "unknown_entity_reference",
    "unknown_event_reference",
    "unknown_fact_reference",
    "unknown_object_reference",
    "unknown_place_reference",
    "unknown_window_reference",
}


def issue(
    layer: str,
    record_id: str,
    severity: str,
    code: str,
    message: str,
    candidate_id: str = "",
) -> dict[str, str]:
    return {
        "layer": layer,
        "record_id": record_id,
        "candidate_id": candidate_id,
        "severity": severity,
        "code": code,
        "message": message,
    }


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def record_id_for(layer: str, record: dict[str, Any]) -> str:
    keys = {
        "micro": ("clip_id", "record_id"),
        "window": ("window_id", "record_id"),
        "session": ("session_id", "record_id"),
    }[layer]
    for key in keys:
        if record.get(key):
            return str(record[key])
    return "unknown"


def add_common_issues(layer: str, record: dict[str, Any]) -> list[dict[str, str]]:
    record_id = record_id_for(layer, record)
    issues: list[dict[str, str]] = []
    for field in REQUIRED_FIELDS[layer]:
        if field not in record:
            issues.append(issue(layer, record_id, "blocking", "missing_required_field", field))
    for field, maximum in COUNT_LIMITS[layer].items():
        value = record.get(field)
        if value is not None and not isinstance(value, list):
            issues.append(issue(layer, record_id, "blocking", "invalid_field_type", f"{field} must be a list"))
        elif isinstance(value, list) and len(value) > maximum:
            issues.append(
                issue(
                    layer,
                    record_id,
                    "warning",
                    "count_limit_exceeded",
                    f"{field} has {len(value)} items; maximum is {maximum}",
                )
            )
    return issues


def collect_ids(
    layer: str,
    record_id: str,
    items: Iterable[dict[str, Any]],
    id_field: str,
    issues: list[dict[str, str]],
) -> set[str]:
    values: set[str] = set()
    for item_value in items:
        value = str(item_value.get(id_field) or "")
        if not value:
            issues.append(issue(layer, record_id, "blocking", "missing_local_id", id_field))
        elif value in values:
            issues.append(issue(layer, record_id, "blocking", "duplicate_local_id", value))
        else:
            values.add(value)
    return values


def check_refs(
    layer: str,
    record_id: str,
    values: Iterable[Any],
    allowed: set[str],
    code: str,
    issues: list[dict[str, str]],
    candidate_id: str = "",
    severity: str = "blocking",
) -> None:
    for value in values:
        ref = str(value or "")
        if ref and ref not in allowed:
            issues.append(issue(layer, record_id, severity, code, ref, candidate_id))


def check_confidences(layer: str, record_id: str, record: dict[str, Any], issues: list[dict[str, str]]) -> None:
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "confidence" in value and value["confidence"] not in CONFIDENCES:
                issues.append(
                    issue(layer, record_id, "blocking", "invalid_confidence", str(value["confidence"]))
                )
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(record)


def has_blocking(issues: Iterable[dict[str, str]]) -> bool:
    return any(item["severity"] == "blocking" for item in issues)


def downgrade_reference_issues(issues: list[dict[str, str]]) -> None:
    for item in issues:
        if item["code"] in REFERENCE_ISSUE_CODES and item["severity"] == "blocking":
            item["severity"] = "warning"


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def expected_micro_duration(record: dict[str, Any], metadata: dict[str, Any]) -> float | None:
    duration = as_float(metadata.get("duration_sec"))
    if duration is not None and duration > 0:
        return duration
    start = as_float(metadata.get("start_sec"))
    end = as_float(metadata.get("end_sec"))
    if start is not None and end is not None and end > start:
        return end - start
    clip_range = record.get("clip_time_range")
    if isinstance(clip_range, dict):
        start = as_float(clip_range.get("start_sec"))
        end = as_float(clip_range.get("end_sec"))
        if start is not None and end is not None and end > start:
            return end - start
    return None


def parse_relative_timestamp(value: str) -> float:
    match = RELATIVE_TIME_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError(value)
    minutes, seconds = match.groups()
    return int(minutes) * 60 + float(seconds)


def check_relative_time(
    record_id: str,
    field_path: str,
    value: Any,
    expected_duration_sec: float | None,
    issues: list[dict[str, str]],
    range_mode: str,
) -> tuple[float, float] | None:
    text = str(value or "").strip()
    parts = [part.strip() for part in text.split("-")]
    valid_part_count = {
        "point": len(parts) == 1,
        "range": len(parts) == 2,
        "either": len(parts) in {1, 2},
    }[range_mode]
    try:
        if not text or not valid_part_count:
            raise ValueError(text)
        parsed = [parse_relative_timestamp(part) for part in parts]
    except ValueError:
        issues.append(
            issue(
                "micro",
                record_id,
                "blocking",
                "invalid_relative_time",
                f"{field_path}={text!r}",
            )
        )
        return None
    start_sec = parsed[0]
    end_sec = parsed[-1]
    if start_sec > end_sec:
        issues.append(
            issue(
                "micro",
                record_id,
                "blocking",
                "invalid_relative_time",
                f"{field_path} is reversed: {text}",
            )
        )
        return None
    if expected_duration_sec is not None:
        allowed_end = math.ceil(expected_duration_sec - 1e-9)
        if end_sec > allowed_end:
            issues.append(
                issue(
                    "micro",
                    record_id,
                    "blocking",
                    "relative_time_out_of_bounds",
                    f"{field_path}={text}; clip duration={expected_duration_sec:.3f}s",
                )
            )
    return start_sec, end_sec


def check_micro_times(
    record: dict[str, Any], metadata: dict[str, Any], issues: list[dict[str, str]]
) -> None:
    record_id = record_id_for("micro", record)
    duration_sec = expected_micro_duration(record, metadata)
    clip_range = record.get("clip_time_range")
    if isinstance(clip_range, dict):
        for key in ("start_sec", "end_sec"):
            expected = as_float(metadata.get(key))
            actual = as_float(clip_range.get(key))
            if expected is not None and (actual is None or abs(actual - expected) > 0.25):
                issues.append(
                    issue(
                        "micro",
                        record_id,
                        "blocking",
                        "clip_time_range_mismatch",
                        f"{key}: expected={expected} actual={clip_range.get(key)!r}",
                    )
                )

    for place_index, place in enumerate(as_list(record.get("places"))):
        for time_index, value in enumerate(as_list(place.get("evidence_times"))):
            check_relative_time(
                record_id,
                f"places[{place_index}].evidence_times[{time_index}]",
                value,
                duration_sec,
                issues,
                "either",
            )
    for object_index, item in enumerate(as_list(record.get("objects"))):
        first = check_relative_time(
            record_id,
            f"objects[{object_index}].first_seen",
            item.get("first_seen"),
            duration_sec,
            issues,
            "point",
        )
        last = check_relative_time(
            record_id,
            f"objects[{object_index}].last_seen",
            item.get("last_seen"),
            duration_sec,
            issues,
            "point",
        )
        if first is not None and last is not None and first[0] > last[0]:
            issues.append(
                issue(
                    "micro",
                    record_id,
                    "blocking",
                    "invalid_relative_time",
                    f"objects[{object_index}] first_seen is after last_seen",
                )
            )
    time_fields = (
        ("atomic_events", "time_range", "range"),
        ("state_observations", "time", "point"),
        ("state_changes", "time_range", "range"),
        ("end_state", "evidence_time", "point"),
        ("uncertainties", "time_range", "either"),
    )
    for section, field, range_mode in time_fields:
        for item_index, item in enumerate(as_list(record.get(section))):
            check_relative_time(
                record_id,
                f"{section}[{item_index}].{field}",
                item.get(field),
                duration_sec,
                issues,
                range_mode,
            )


def validate_micro_record(
    record: dict[str, Any], metadata: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    normalized = copy.deepcopy(record)
    layer = "micro"
    record_id = record_id_for(layer, record)
    issues = add_common_issues(layer, record)
    places = collect_ids(layer, record_id, as_list(record.get("places")), "place_id", issues)
    objects = collect_ids(layer, record_id, as_list(record.get("objects")), "object_id", issues)
    events = collect_ids(layer, record_id, as_list(record.get("atomic_events")), "event_id", issues)
    entities = places | objects

    for event in as_list(record.get("atomic_events")):
        place_id = event.get("place_id")
        if place_id:
            check_refs(layer, record_id, [place_id], places, "unknown_place_reference", issues)
        check_refs(
            layer,
            record_id,
            as_list(event.get("object_ids")),
            objects,
            "unknown_object_reference",
            issues,
        )
    for field in ("state_observations", "state_changes", "end_state"):
        for item_value in as_list(record.get(field)):
            check_refs(
                layer,
                record_id,
                [item_value.get("entity_id")],
                entities,
                "unknown_entity_reference",
                issues,
            )
    for change in as_list(record.get("state_changes")):
        trigger = change.get("trigger_event_id")
        if trigger:
            check_refs(layer, record_id, [trigger], events, "unknown_event_reference", issues)

    expected_id = str(metadata.get("session_id") or metadata.get("record_id") or "")
    if expected_id and record_id != expected_id:
        issues.append(issue(layer, record_id, "blocking", "record_id_mismatch", expected_id))
    expected_source = str(metadata.get("source_video_id") or metadata.get("video_id") or "")
    if expected_source and str(record.get("source_video_id") or "") != expected_source:
        issues.append(issue(layer, record_id, "blocking", "source_video_id_mismatch", expected_source))
    check_micro_times(record, metadata, issues)
    check_confidences(layer, record_id, record, issues)
    downgrade_reference_issues(issues)
    normalized["quality_summary"] = {
        "schema_status": "failed" if has_blocking(issues) else "passed",
        "issue_codes": sorted({item["code"] for item in issues}),
    }
    return normalized, issues


def validate_window_record(
    record: dict[str, Any], parent: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    normalized = copy.deepcopy(record)
    layer = "window"
    record_id = record_id_for(layer, record)
    issues = add_common_issues(layer, record)
    allowed_clips = {str(value) for value in as_list(parent.get("micro_clip_ids"))}
    entities = collect_ids(layer, record_id, as_list(record.get("entity_map")), "entity_id", issues)
    events = collect_ids(layer, record_id, as_list(record.get("local_event_chain")), "event_id", issues)
    facts = collect_ids(layer, record_id, as_list(record.get("evidence_facts")), "fact_id", issues)

    sections_with_clip_refs = (
        "entity_map",
        "local_event_chain",
        "state_changes",
        "window_end_state",
        "open_threads",
        "evidence_facts",
        "conflicts_or_uncertainties",
    )
    for field in sections_with_clip_refs:
        for item_value in as_list(record.get(field)):
            check_refs(
                layer,
                record_id,
                as_list(item_value.get("supporting_clip_ids")),
                allowed_clips,
                "unknown_clip_reference",
                issues,
            )
    for field in ("local_event_chain", "open_threads", "evidence_facts"):
        for item_value in as_list(record.get(field)):
            check_refs(
                layer,
                record_id,
                as_list(item_value.get("entity_ids")),
                entities,
                "unknown_entity_reference",
                issues,
            )
    for field in ("state_changes", "window_end_state"):
        for item_value in as_list(record.get(field)):
            check_refs(
                layer,
                record_id,
                [item_value.get("entity_id")],
                entities,
                "unknown_entity_reference",
                issues,
            )
    for change in as_list(record.get("state_changes")):
        check_refs(
            layer,
            record_id,
            as_list(change.get("supporting_event_ids")),
            events,
            "unknown_event_reference",
            issues,
        )
    for conflict in as_list(record.get("conflicts_or_uncertainties")):
        check_refs(
            layer,
            record_id,
            as_list(conflict.get("affected_fact_ids")),
            facts,
            "unknown_fact_reference",
            issues,
        )
    expected_id = str(parent.get("record_id") or parent.get("window_id") or "")
    if expected_id and record_id != expected_id:
        issues.append(issue(layer, record_id, "blocking", "record_id_mismatch", expected_id))
    if str(record.get("source_video_id") or "") != str(parent.get("source_video_id") or ""):
        issues.append(issue(layer, record_id, "blocking", "source_video_id_mismatch", str(parent.get("source_video_id") or "")))
    check_confidences(layer, record_id, record, issues)
    downgrade_reference_issues(issues)
    normalized["quality_summary"] = {
        "schema_status": "failed" if has_blocking(issues) else "passed",
        "issue_codes": sorted({item["code"] for item in issues}),
    }
    return normalized, issues


def validate_session_record(
    record: dict[str, Any], parent: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    normalized = copy.deepcopy(record)
    layer = "session"
    record_id = record_id_for(layer, record)
    issues = add_common_issues(layer, record)
    allowed_windows = {str(value) for value in as_list(parent.get("window_ids"))}
    entities = collect_ids(layer, record_id, as_list(record.get("session_entities")), "entity_id", issues)
    candidates = collect_ids(
        layer,
        record_id,
        as_list(record.get("cross_session_evidence_candidates")),
        "candidate_id",
        issues,
    )

    sections_with_window_refs = (
        "session_timeline",
        "session_entities",
        "state_update_timeline",
        "session_final_state",
        "open_tasks_or_unresolved_states",
        "cross_session_evidence_candidates",
        "contradictions_or_uncertainties",
    )
    for field in sections_with_window_refs:
        for item_value in as_list(record.get(field)):
            candidate_id = str(item_value.get("candidate_id") or "")
            check_refs(
                layer,
                record_id,
                as_list(item_value.get("supporting_window_ids")),
                allowed_windows,
                "unknown_window_reference",
                issues,
                candidate_id,
                "candidate_blocking" if candidate_id else "warning",
            )
    for field in (
        "state_update_timeline",
        "session_final_state",
        "open_tasks_or_unresolved_states",
        "cross_session_evidence_candidates",
    ):
        for item_value in as_list(record.get(field)):
            refs = (
                as_list(item_value.get("entity_ids"))
                if "entity_ids" in item_value
                else [item_value.get("entity_id")]
            )
            check_refs(
                layer,
                record_id,
                refs,
                entities,
                "unknown_entity_reference",
                issues,
                str(item_value.get("candidate_id") or ""),
                (
                    "candidate_blocking"
                    if item_value.get("candidate_id")
                    else "warning"
                ),
            )

    affected_candidates: set[str] = set()
    for uncertainty in as_list(record.get("contradictions_or_uncertainties")):
        refs = as_list(uncertainty.get("affected_candidate_ids"))
        check_refs(
            layer,
            record_id,
            refs,
            candidates,
            "unknown_candidate_reference",
            issues,
            severity="warning",
        )
        affected_candidates.update(str(value) for value in refs if value)

    normalized_candidates = as_list(normalized.get("cross_session_evidence_candidates"))
    for candidate in normalized_candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        flags = {str(value) for value in as_list(candidate.get("quality_flags")) if value}
        claim = str(candidate.get("claim") or "")
        support_ids = [str(value) for value in as_list(candidate.get("supporting_window_ids"))]
        candidate_issue_codes = {
            item["code"]
            for item in issues
            if item.get("candidate_id") == candidate_id
            and item.get("severity") == "candidate_blocking"
        }
        flags.update(candidate_issue_codes)
        if LONG_TERM_RE.search(claim):
            flags.add("long_term_overclaim")
            issues.append(
                issue(layer, record_id, "warning", "long_term_overclaim", claim, candidate_id)
            )
        if candidate_id in affected_candidates:
            flags.add("affected_by_uncertainty")
            issues.append(
                issue(
                    layer,
                    record_id,
                    "warning",
                    "affected_by_uncertainty",
                    candidate_id,
                    candidate_id,
                )
            )
        if candidate.get("type") in {"stable_layout", "procedure_candidate"} and len(set(support_ids)) <= 1:
            flags.add("single_window_support")
            issues.append(
                issue(
                    layer,
                    record_id,
                    "warning",
                    "single_window_support",
                    candidate_id,
                    candidate_id,
                )
            )
        confidence = str(candidate.get("confidence") or "low")
        normalized_confidence = confidence
        if "affected_by_uncertainty" in flags and confidence == "high":
            normalized_confidence = "medium"
        candidate["quality_flags"] = sorted(flags)
        candidate["normalized_confidence"] = normalized_confidence
        candidate["qc_status"] = "schema_failed" if candidate_issue_codes else "schema_passed"
        candidate["usable_for_reference"] = False

    expected_id = str(parent.get("session_id") or parent.get("record_id") or "")
    if expected_id and record_id != expected_id:
        issues.append(issue(layer, record_id, "blocking", "record_id_mismatch", expected_id))
    if str(record.get("source_video_id") or "") != str(parent.get("source_video_id") or ""):
        issues.append(issue(layer, record_id, "blocking", "source_video_id_mismatch", str(parent.get("source_video_id") or "")))
    check_confidences(layer, record_id, record, issues)
    normalized["quality_summary"] = {
        "schema_status": "failed" if has_blocking(issues) else "passed",
        "issue_codes": sorted({item["code"] for item in issues}),
    }
    if has_blocking(issues):
        for candidate in normalized_candidates:
            candidate["qc_status"] = "schema_failed"
    return normalized, issues


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def write_reports(output_dir: Path, layer: str, issues: list[dict[str, str]], accepted: int, rejected: int) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "issues.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ISSUE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(issues)
    by_code = Counter(item["code"] for item in issues)
    report = {
        "layer": layer,
        "records": accepted + rejected,
        "accepted": accepted,
        "rejected": rejected,
        "issues": len(issues),
        "blocking_issues": sum(item["severity"] == "blocking" for item in issues),
        "candidate_blocking_issues": sum(
            item["severity"] == "candidate_blocking" for item in issues
        ),
        "issues_by_code": dict(sorted(by_code.items())),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_directory(
    layer: str,
    input_dir: Path,
    parent_records: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    parent_key = {"micro": "session_id", "window": "record_id", "session": "session_id"}[layer]
    parent_by_id: dict[str, dict[str, Any]] = {}
    for parent in parent_records:
        keys = [parent_key, "record_id", "window_id", "source_video_id", "video_id"]
        parent_id = next((str(parent[key]) for key in keys if parent.get(key)), "")
        if parent_id:
            parent_by_id[parent_id] = parent
    validator = {
        "micro": validate_micro_record,
        "window": validate_window_record,
        "session": validate_session_record,
    }[layer]
    accepted_dir = output_dir / "accepted"
    accepted_dir.mkdir(parents=True, exist_ok=True)
    input_paths = sorted(input_dir.glob("*.clean.json"))
    for stale_path in accepted_dir.glob("*.clean.json"):
        stale_path.unlink()
    accepted = 0
    rejected = 0
    all_issues: list[dict[str, str]] = []
    for path in input_paths:
        record = json.loads(path.read_text(encoding="utf-8"))
        record_id = record_id_for(layer, record)
        parent = parent_by_id.get(record_id)
        if parent is None:
            normalized = copy.deepcopy(record)
            current_issues = [
                issue(layer, record_id, "blocking", "missing_parent_metadata", record_id)
            ]
        else:
            normalized, current_issues = validator(record, parent)
        all_issues.extend(current_issues)
        if has_blocking(current_issues):
            rejected += 1
            accepted_path = accepted_dir / path.name
            if accepted_path.exists():
                accepted_path.unlink()
            continue
        accepted += 1
        (accepted_dir / path.name).write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    write_reports(output_dir, layer, all_issues, accepted, rejected)
    print(f"{layer}: accepted={accepted} rejected={rejected} issues={len(all_issues)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="layer", required=True)
    for layer in ("micro", "window", "session"):
        command = subparsers.add_parser(layer)
        command.add_argument("--input-dir", required=True)
        command.add_argument("--metadata", required=True)
        command.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    metadata_path = Path(args.metadata)
    parents = read_csv(metadata_path) if metadata_path.suffix.lower() == ".csv" else read_jsonl(metadata_path)
    validate_directory(args.layer, Path(args.input_dir), parents, Path(args.output_dir))


if __name__ == "__main__":
    main()
