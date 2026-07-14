#!/usr/bin/env python3
"""Audit Ego4D metadata and build reproducible benchmark manifests.

The public ``ego4d.json`` metadata identifies canonical videos and wearers, but
does not provide a standardized capture timestamp that can order different
canonical videos. Real capture chronology therefore remains unknown. For the
benchmark, this script separately assigns a deterministic presentation order;
that order defines the synthetic cross-session timeline seen by evaluated
agents and must not be described as the original recording chronology.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_OUTPUT_DIR = Path("data/processed/ego4d")


VIDEO_FIELDS = [
    "video_uid",
    "participant_id",
    "fb_participant_id",
    "participant_known",
    "video_source",
    "origin_video_id",
    "duration_sec",
    "duration_min",
    "scenarios",
    "scenario_count",
    "device",
    "physical_setting_name",
    "fps",
    "num_frames",
    "video_codec",
    "display_resolution_width",
    "display_resolution_height",
    "sample_resolution_width",
    "sample_resolution_height",
    "audio_start_sec",
    "audio_duration_sec",
    "has_audio",
    "audio_coverage_ratio",
    "split_em",
    "split_av",
    "split_fho",
    "is_stereo",
    "has_imu",
    "has_gaze",
    "component_count",
    "component_indices",
    "within_video_order_status",
    "has_redacted_regions",
    "redacted_duration_sec",
    "redacted_ratio",
    "redaction_measurement_status",
    "concurrent_video_set_ids",
    "concurrent_video_set_count",
    "session_unit_type",
    "s3_path",
]


PARTICIPANT_FIELDS = [
    "participant_id",
    "fb_participant_id",
    "participant_known",
    "video_count",
    "video_uids_unordered",
    "total_duration_sec",
    "total_duration_hours",
    "video_sources",
    "scenarios",
    "scenario_count",
    "physical_settings",
    "devices",
    "audio_video_count",
    "redacted_video_count",
    "redaction_extent_unknown_video_count",
    "concurrent_video_count",
    "cross_video_order_status",
    "cross_video_order_basis",
    "session_order_assigned",
    "temporal_evolution_eligible",
    "cross_video_consistency_eligible",
]


SCENARIO_FIELDS = [
    "scenario",
    "video_count",
    "known_participant_count",
    "total_duration_sec",
    "total_duration_hours",
    "audio_video_count",
    "redacted_video_count",
    "cross_video_consistency_eligible_video_count",
]


TEMPORAL_AUDIT_FIELDS = [
    "participant_id",
    "fb_participant_id",
    "participant_known",
    "canonical_video_count",
    "canonical_video_uids_unordered",
    "within_canonical_video_order_status",
    "cross_video_order_status",
    "cross_video_order_basis",
    "session_order_assigned",
    "temporal_evolution_eligible",
    "cross_video_consistency_eligible",
    "prohibited_order_inference_sources",
    "audit_note",
]


CANDIDATE_FIELDS = [
    "video_uid",
    "participant_id",
    "fb_participant_id",
    "participant_known",
    "duration_sec",
    "scenarios",
    "has_audio",
    "audio_coverage_ratio",
    "redacted_ratio",
    "redaction_measurement_status",
    "concurrent_video_set_ids",
    "canonical_session_id",
    "cross_video_session_order",
    "within_video_order_status",
    "cross_video_order_status",
    "cross_video_order_basis",
    "temporal_evolution_eligible",
    "cross_video_consistency_eligible",
    "eligible_video_count_for_participant",
    "independent_eligible_video_count_for_participant",
    "candidate_status",
    "exclusion_reasons",
    "recommended_ego4d_dataset",
]


PILOT_FIELDS = CANDIDATE_FIELDS + [
    "benchmark_session_order",
    "benchmark_order_status",
    "benchmark_order_basis",
    "benchmark_temporal_evolution_eligible",
    "pilot_selection_rank",
    "participant_selection_rank",
    "participant_video_selection_rank",
    "pilot_selection_rank_is_temporal",
]


BENCHMARK_ORDER_FIELDS = [
    "benchmark_session_order",
    "benchmark_order_status",
    "benchmark_order_basis",
    "benchmark_temporal_evolution_eligible",
]


BENCHMARK_FIELDS = CANDIDATE_FIELDS + BENCHMARK_ORDER_FIELDS


PARTICIPANT_MANIFEST_INDEX_FIELDS = [
    "participant_id",
    "manifest_file",
    "video_count",
    "total_duration_sec",
    "first_benchmark_session_order",
    "last_benchmark_session_order",
]


@dataclass(frozen=True)
class CandidateFilters:
    """Transparent filters for the production candidate pool."""

    min_duration_sec: float = 300.0
    max_duration_sec: float = 7200.0
    max_redaction_ratio: float = 0.20
    min_videos_per_participant: int = 3
    require_audio: bool = False
    scenarios: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "min_duration_sec": self.min_duration_sec,
            "max_duration_sec": self.max_duration_sec,
            "max_redaction_ratio": self.max_redaction_ratio,
            "min_videos_per_participant": self.min_videos_per_participant,
            "require_audio": self.require_audio,
            "scenarios": list(self.scenarios),
        }


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _sorted_strings(values: Iterable[object]) -> list[str]:
    clean: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            clean.add(text)
    return sorted(clean, key=str.casefold)


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return value


def write_csv(path: Path, rows: Sequence[dict[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def normalize_participant_id(raw_participant_id: object, video_uid: str) -> tuple[str, bool]:
    """Return a stable ID while keeping every unknown wearer in a separate group."""
    if raw_participant_id is None or raw_participant_id == "":
        return f"EGO4D_UNKNOWN_{video_uid}", False

    numeric = _as_int(raw_participant_id)
    if numeric is not None:
        return f"EGO4D_P{numeric:06d}", True

    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(raw_participant_id)).strip("_")
    if not slug:
        return f"EGO4D_UNKNOWN_{video_uid}", False
    return f"EGO4D_P_{slug}", True


def build_concurrent_video_index(metadata: dict[str, object]) -> dict[str, list[object]]:
    """Map video UIDs to valid concurrent capture sets."""
    index: dict[str, set[object]] = defaultdict(set)
    raw_sets = metadata.get("concurrent_video_sets") or []
    if not isinstance(raw_sets, list):
        return {}

    for concurrent_set in raw_sets:
        if not isinstance(concurrent_set, dict) or concurrent_set.get("valid") is False:
            continue
        set_id = concurrent_set.get("concurrent_video_set_id")
        if set_id is None:
            continue
        for member in concurrent_set.get("videos") or []:
            if not isinstance(member, dict):
                continue
            video_uid = str(member.get("video_uid") or "").strip()
            if video_uid:
                index[video_uid].add(set_id)

    return {
        video_uid: sorted(set_ids, key=lambda item: str(item))
        for video_uid, set_ids in index.items()
    }


def merged_interval_duration(intervals: object, duration_sec: float) -> float:
    """Measure the union of redaction intervals, clipped to the video timeline."""
    if not isinstance(intervals, list) or duration_sec <= 0:
        return 0.0

    clean: list[tuple[float, float]] = []
    for interval in intervals:
        if not isinstance(interval, dict):
            continue
        start = _as_float(interval.get("start_sec"))
        end = _as_float(interval.get("end_sec"))
        if start is None or end is None:
            continue
        start = min(max(start, 0.0), duration_sec)
        end = min(max(end, 0.0), duration_sec)
        if end > start:
            clean.append((start, end))

    total = 0.0
    merged_end: float | None = None
    merged_start = 0.0
    for start, end in sorted(clean):
        if merged_end is None:
            merged_start, merged_end = start, end
        elif start <= merged_end:
            merged_end = max(merged_end, end)
        else:
            total += merged_end - merged_start
            merged_start, merged_end = start, end
    if merged_end is not None:
        total += merged_end - merged_start
    return total


def normalize_video(
    video: dict[str, object],
    concurrent_video_index: dict[str, list[object]] | None = None,
) -> dict[str, object]:
    video_uid = str(video.get("video_uid") or "").strip()
    if not video_uid:
        raise ValueError("Ego4D video is missing video_uid")

    metadata = video.get("video_metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    duration_sec = _as_float(video.get("duration_sec"))
    if duration_sec is None:
        duration_sec = _as_float(metadata.get("video_duration_sec"))
    if duration_sec is None:
        duration_sec = _as_float(metadata.get("mp4_duration_sec"))
    duration_sec = duration_sec or 0.0

    raw_participant_id = video.get("fb_participant_id")
    participant_id, participant_known = normalize_participant_id(raw_participant_id, video_uid)
    scenarios = _sorted_strings(video.get("scenarios") or [])

    audio_duration_sec = _as_float(metadata.get("audio_duration_sec"))
    audio_start_sec = _as_float(metadata.get("audio_start_sec"))
    has_audio = audio_duration_sec is not None and audio_duration_sec > 0
    audio_coverage_ratio = None
    if has_audio and duration_sec > 0 and audio_duration_sec is not None:
        audio_coverage_ratio = min(max(audio_duration_sec / duration_sec, 0.0), 1.0)

    raw_intervals = video.get("redacted_intervals")
    declared_redactions = bool(video.get("has_redacted_regions"))
    has_interval_metadata = isinstance(raw_intervals, list) and bool(raw_intervals)
    has_redacted_regions = declared_redactions or has_interval_metadata
    if has_interval_metadata:
        redacted_duration_sec: float | None = merged_interval_duration(raw_intervals, duration_sec)
        redacted_ratio: float | None = (
            min(max(redacted_duration_sec / duration_sec, 0.0), 1.0) if duration_sec > 0 else None
        )
        redaction_measurement_status = "measured" if declared_redactions else "measured_flag_mismatch"
    elif declared_redactions:
        redacted_duration_sec = None
        redacted_ratio = None
        redaction_measurement_status = "missing_intervals"
    else:
        redacted_duration_sec = 0.0
        redacted_ratio = 0.0 if duration_sec > 0 else None
        redaction_measurement_status = "none"

    components = video.get("video_components") or []
    if not isinstance(components, list):
        components = []
    component_indices = sorted(
        index
        for index in (
            _as_int(component.get("component_idx"))
            for component in components
            if isinstance(component, dict)
        )
        if index is not None
    )
    concurrent_set_ids = (concurrent_video_index or {}).get(video_uid, [])

    return {
        "video_uid": video_uid,
        "participant_id": participant_id,
        "fb_participant_id": raw_participant_id,
        "participant_known": participant_known,
        "video_source": video.get("video_source"),
        "origin_video_id": video.get("origin_video_id"),
        "duration_sec": duration_sec,
        "duration_min": duration_sec / 60.0,
        "scenarios": scenarios,
        "scenario_count": len(scenarios),
        "device": video.get("device"),
        "physical_setting_name": video.get("physical_setting_name"),
        "fps": _as_float(metadata.get("fps")),
        "num_frames": _as_int(metadata.get("num_frames")),
        "video_codec": metadata.get("video_codec"),
        "display_resolution_width": _as_int(metadata.get("display_resolution_width")),
        "display_resolution_height": _as_int(metadata.get("display_resolution_height")),
        "sample_resolution_width": _as_int(metadata.get("sample_resolution_width")),
        "sample_resolution_height": _as_int(metadata.get("sample_resolution_height")),
        "audio_start_sec": audio_start_sec,
        "audio_duration_sec": audio_duration_sec,
        "has_audio": has_audio,
        "audio_coverage_ratio": audio_coverage_ratio,
        "split_em": video.get("split_em"),
        "split_av": video.get("split_av"),
        "split_fho": video.get("split_fho"),
        "is_stereo": bool(video.get("is_stereo")),
        "has_imu": bool(video.get("has_imu")),
        "has_gaze": bool(video.get("has_gaze")),
        "component_count": len(components),
        "component_indices": component_indices,
        "within_video_order_status": "verified_canonical_timeline",
        "has_redacted_regions": has_redacted_regions,
        "redacted_duration_sec": redacted_duration_sec,
        "redacted_ratio": redacted_ratio,
        "redaction_measurement_status": redaction_measurement_status,
        "concurrent_video_set_ids": concurrent_set_ids,
        "concurrent_video_set_count": len(concurrent_set_ids),
        "session_unit_type": "canonical_video",
        "s3_path": video.get("s3_path"),
    }


def build_participant_summaries(video_rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in video_rows:
        groups[str(row["participant_id"])].append(row)

    summaries: list[dict[str, object]] = []
    for participant_id, rows in sorted(groups.items()):
        participant_known = all(bool(row["participant_known"]) for row in rows)
        video_count = len(rows)
        cross_video_order_status = "unknown" if video_count > 1 else "not_applicable_single_video"
        raw_ids = _sorted_strings(
            row.get("fb_participant_id")
            for row in rows
            if row.get("fb_participant_id") is not None
        )
        summaries.append(
            {
                "participant_id": participant_id,
                "fb_participant_id": raw_ids[0] if len(raw_ids) == 1 else raw_ids,
                "participant_known": participant_known,
                "video_count": video_count,
                "video_uids_unordered": sorted(str(row["video_uid"]) for row in rows),
                "total_duration_sec": sum(float(row["duration_sec"]) for row in rows),
                "total_duration_hours": sum(float(row["duration_sec"]) for row in rows) / 3600.0,
                "video_sources": _sorted_strings(row.get("video_source") for row in rows if row.get("video_source")),
                "scenarios": _sorted_strings(
                    scenario
                    for row in rows
                    for scenario in row.get("scenarios", [])
                ),
                "scenario_count": len(
                    {
                        str(scenario).casefold()
                        for row in rows
                        for scenario in row.get("scenarios", [])
                    }
                ),
                "physical_settings": _sorted_strings(
                    row.get("physical_setting_name") for row in rows if row.get("physical_setting_name")
                ),
                "devices": _sorted_strings(row.get("device") for row in rows if row.get("device")),
                "audio_video_count": sum(bool(row["has_audio"]) for row in rows),
                "redacted_video_count": sum(bool(row["has_redacted_regions"]) for row in rows),
                "redaction_extent_unknown_video_count": sum(
                    row["redaction_measurement_status"] == "missing_intervals" for row in rows
                ),
                "concurrent_video_count": sum(bool(row["concurrent_video_set_count"]) for row in rows),
                "cross_video_order_status": cross_video_order_status,
                "cross_video_order_basis": "none",
                "session_order_assigned": False,
                "temporal_evolution_eligible": False,
                "cross_video_consistency_eligible": participant_known and video_count >= 2,
            }
        )
    return summaries


def build_scenario_summaries(
    video_rows: Sequence[dict[str, object]],
    participant_rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    participant_by_id = {str(row["participant_id"]): row for row in participant_rows}
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for video in video_rows:
        for scenario in video.get("scenarios", []):
            groups[str(scenario)].append(video)

    summaries: list[dict[str, object]] = []
    for scenario, rows in sorted(groups.items(), key=lambda item: item[0].casefold()):
        known_participants = {
            str(row["participant_id"])
            for row in rows
            if bool(row["participant_known"])
        }
        summaries.append(
            {
                "scenario": scenario,
                "video_count": len(rows),
                "known_participant_count": len(known_participants),
                "total_duration_sec": sum(float(row["duration_sec"]) for row in rows),
                "total_duration_hours": sum(float(row["duration_sec"]) for row in rows) / 3600.0,
                "audio_video_count": sum(bool(row["has_audio"]) for row in rows),
                "redacted_video_count": sum(bool(row["has_redacted_regions"]) for row in rows),
                "cross_video_consistency_eligible_video_count": sum(
                    bool(participant_by_id[str(row["participant_id"])]["cross_video_consistency_eligible"])
                    for row in rows
                ),
            }
        )
    return summaries


def build_temporal_order_audit(
    participant_rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    audit: list[dict[str, object]] = []
    for participant in participant_rows:
        video_count = int(participant["video_count"])
        order_status = str(participant["cross_video_order_status"])
        if order_status == "unknown":
            note = "Canonical videos share a wearer identity, but ego4d.json provides no standardized capture order."
        else:
            note = "Only one canonical video is available for this participant group."
        audit.append(
            {
                "participant_id": participant["participant_id"],
                "fb_participant_id": participant["fb_participant_id"],
                "participant_known": participant["participant_known"],
                "canonical_video_count": video_count,
                "canonical_video_uids_unordered": participant["video_uids_unordered"],
                "within_canonical_video_order_status": "verified_canonical_timeline",
                "cross_video_order_status": order_status,
                "cross_video_order_basis": participant["cross_video_order_basis"],
                "session_order_assigned": participant["session_order_assigned"],
                "temporal_evolution_eligible": participant["temporal_evolution_eligible"],
                "cross_video_consistency_eligible": participant["cross_video_consistency_eligible"],
                "prohibited_order_inference_sources": [
                    "video_uid",
                    "origin_video_id",
                    "s3_path",
                    "lexical_sort",
                    "video_component_idx_across_videos",
                ],
                "audit_note": note,
            }
        )
    return audit


def _matches_scenario(video_scenarios: Sequence[object], required: Sequence[str]) -> bool:
    if not required:
        return True
    available = {str(value).strip().casefold() for value in video_scenarios}
    return any(value.strip().casefold() in available for value in required)


def _deduplicate_concurrent_rows(
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    used_concurrent_sets: set[str] = set()
    for row in sorted(rows, key=lambda item: str(item["video_uid"])):
        concurrent_sets = {str(value) for value in row["concurrent_video_set_ids"]}
        if concurrent_sets & used_concurrent_sets:
            continue
        selected.append(row)
        used_concurrent_sets.update(concurrent_sets)
    return selected


def benchmark_manifest_slug(participant_id: str) -> str:
    """Return the filename component shared by vpn and cluster manifests."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", participant_id).strip("_.-").lower()
    if not slug:
        raise ValueError(f"Cannot build manifest filename for participant {participant_id!r}")
    return slug


def build_candidate_rows(
    video_rows: Sequence[dict[str, object]],
    participant_rows: Sequence[dict[str, object]],
    filters: CandidateFilters,
) -> list[dict[str, object]]:
    """Build candidates for an unordered same-wearer consistency pilot."""
    participant_by_id = {str(row["participant_id"]): row for row in participant_rows}
    preliminary_reasons: dict[str, list[str]] = {}

    for video in video_rows:
        reasons: list[str] = []
        duration_sec = float(video["duration_sec"])
        participant = participant_by_id[str(video["participant_id"])]
        if not video["participant_known"]:
            reasons.append("unknown_participant")
        elif not participant["cross_video_consistency_eligible"]:
            reasons.append("not_enough_videos_for_cross_video_consistency")
        if duration_sec <= 0:
            reasons.append("invalid_duration")
        elif duration_sec < filters.min_duration_sec:
            reasons.append("duration_below_min")
        elif duration_sec > filters.max_duration_sec:
            reasons.append("duration_above_max")

        redacted_ratio = video.get("redacted_ratio")
        if video["redaction_measurement_status"] == "missing_intervals":
            reasons.append("redaction_extent_unknown")
        elif redacted_ratio is not None and float(redacted_ratio) > filters.max_redaction_ratio:
            reasons.append("redaction_ratio_above_max")
        if filters.require_audio and not video["has_audio"]:
            reasons.append("audio_required_but_missing")
        if not _matches_scenario(video.get("scenarios", []), filters.scenarios):
            reasons.append("scenario_filter_mismatch")
        preliminary_reasons[str(video["video_uid"])] = reasons

    eligible_count_by_participant: dict[str, int] = defaultdict(int)
    preliminarily_eligible_by_participant: dict[str, list[dict[str, object]]] = defaultdict(list)
    for video in video_rows:
        if not preliminary_reasons[str(video["video_uid"])]:
            participant_id = str(video["participant_id"])
            eligible_count_by_participant[participant_id] += 1
            preliminarily_eligible_by_participant[participant_id].append(video)
    independent_count_by_participant = {
        participant_id: len(_deduplicate_concurrent_rows(rows))
        for participant_id, rows in preliminarily_eligible_by_participant.items()
    }

    candidates: list[dict[str, object]] = []
    for video in sorted(video_rows, key=lambda row: str(row["video_uid"])):
        participant_id = str(video["participant_id"])
        participant = participant_by_id[participant_id]
        reasons = list(preliminary_reasons[str(video["video_uid"])])
        eligible_count = eligible_count_by_participant[participant_id]
        independent_count = independent_count_by_participant.get(participant_id, 0)
        if not reasons and independent_count < filters.min_videos_per_participant:
            reasons.append("insufficient_independent_eligible_videos_for_participant")

        candidates.append(
            {
                "video_uid": video["video_uid"],
                "participant_id": participant_id,
                "fb_participant_id": video["fb_participant_id"],
                "participant_known": video["participant_known"],
                "duration_sec": video["duration_sec"],
                "scenarios": video["scenarios"],
                "has_audio": video["has_audio"],
                "audio_coverage_ratio": video["audio_coverage_ratio"],
                "redacted_ratio": video["redacted_ratio"],
                "redaction_measurement_status": video["redaction_measurement_status"],
                "concurrent_video_set_ids": video["concurrent_video_set_ids"],
                "canonical_session_id": video["video_uid"],
                "cross_video_session_order": None,
                "within_video_order_status": video["within_video_order_status"],
                "cross_video_order_status": participant["cross_video_order_status"],
                "cross_video_order_basis": participant["cross_video_order_basis"],
                "temporal_evolution_eligible": False,
                "cross_video_consistency_eligible": participant["cross_video_consistency_eligible"],
                "eligible_video_count_for_participant": eligible_count,
                "independent_eligible_video_count_for_participant": independent_count,
                "candidate_status": "eligible" if not reasons else "excluded",
                "exclusion_reasons": reasons,
                "recommended_ego4d_dataset": "video_540ss",
            }
        )
    return candidates


def select_benchmark_rows(
    candidate_rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Select all eligible independent videos and assign benchmark time order.

    The ordering is intentionally synthetic. UUID lexical order is used only
    because it is stable and reproducible; it is not evidence of capture time.
    """
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in candidate_rows:
        if row["candidate_status"] == "eligible":
            groups[str(row["participant_id"])].append(row)

    benchmark: list[dict[str, object]] = []
    for participant_id in sorted(groups, key=str.casefold):
        selected = _deduplicate_concurrent_rows(groups[participant_id])
        temporal_eligible = len(selected) >= 2
        for order, candidate in enumerate(selected, start=1):
            row = dict(candidate)
            row.update(
                {
                    "benchmark_session_order": order,
                    "benchmark_order_status": "assigned",
                    "benchmark_order_basis": "deterministic_video_uid_order",
                    "benchmark_temporal_evolution_eligible": temporal_eligible,
                }
            )
            benchmark.append(row)
    return benchmark


def select_pilot_rows(
    candidate_rows: Sequence[dict[str, object]],
    participant_limit: int,
    videos_per_participant: int,
) -> list[dict[str, object]]:
    """Select a deterministic subset while preserving benchmark session order."""
    if participant_limit <= 0 or videos_per_participant <= 0:
        return []

    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    benchmark_rows = (
        list(candidate_rows)
        if all("benchmark_session_order" in row for row in candidate_rows)
        else select_benchmark_rows(candidate_rows)
    )
    for row in benchmark_rows:
        groups[str(row["participant_id"])].append(row)

    selectable_by_participant = groups
    ranked_participants = sorted(
        selectable_by_participant,
        key=lambda participant_id: (
            -len(selectable_by_participant[participant_id]),
            -sum(
                float(row["duration_sec"])
                for row in selectable_by_participant[participant_id]
            ),
            participant_id,
        ),
    )[:participant_limit]

    pilot: list[dict[str, object]] = []
    for participant_rank, participant_id in enumerate(ranked_participants, start=1):
        selected = sorted(
            selectable_by_participant[participant_id],
            key=lambda row: int(row["benchmark_session_order"]),
        )[:videos_per_participant]
        for video_rank, candidate in enumerate(selected, start=1):
            row = dict(candidate)
            row.update(
                {
                    "pilot_selection_rank": len(pilot) + 1,
                    "participant_selection_rank": participant_rank,
                    "participant_video_selection_rank": video_rank,
                    "pilot_selection_rank_is_temporal": False,
                }
            )
            pilot.append(row)
    return pilot


def _validate_filters(filters: CandidateFilters) -> None:
    if filters.min_duration_sec < 0:
        raise ValueError("min_duration_sec must be non-negative")
    if filters.max_duration_sec < filters.min_duration_sec:
        raise ValueError("max_duration_sec must be >= min_duration_sec")
    if not 0 <= filters.max_redaction_ratio <= 1:
        raise ValueError("max_redaction_ratio must be between 0 and 1")
    if filters.min_videos_per_participant < 1:
        raise ValueError("min_videos_per_participant must be at least 1")


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - rank) + ordered[upper] * (rank - lower)


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": min(values),
        "p25": _percentile(values, 0.25),
        "median": _percentile(values, 0.50),
        "p75": _percentile(values, 0.75),
        "p90": _percentile(values, 0.90),
        "max": max(values),
    }


def analyze_metadata(
    metadata: dict[str, object],
    output_dir: Path,
    filters: CandidateFilters | None = None,
    *,
    pilot_participants: int = 5,
    pilot_videos_per_participant: int = 3,
) -> dict[str, object]:
    filters = filters or CandidateFilters()
    _validate_filters(filters)
    if pilot_participants < 0 or pilot_videos_per_participant < 0:
        raise ValueError("pilot limits must be non-negative")

    raw_videos = metadata.get("videos")
    if not isinstance(raw_videos, list):
        raise ValueError("ego4d.json must contain a videos array")

    concurrent_index = build_concurrent_video_index(metadata)
    video_rows = [
        normalize_video(video, concurrent_index)
        for video in raw_videos
        if isinstance(video, dict)
    ]
    video_rows.sort(key=lambda row: str(row["video_uid"]))
    video_uids = [str(row["video_uid"]) for row in video_rows]
    if len(video_uids) != len(set(video_uids)):
        raise ValueError("ego4d.json contains duplicate video_uid values")

    participant_rows = build_participant_summaries(video_rows)
    scenario_rows = build_scenario_summaries(video_rows, participant_rows)
    temporal_audit = build_temporal_order_audit(participant_rows)
    candidate_rows = build_candidate_rows(video_rows, participant_rows, filters)
    benchmark_rows = select_benchmark_rows(candidate_rows)
    pilot_rows = select_pilot_rows(
        benchmark_rows,
        participant_limit=pilot_participants,
        videos_per_participant=pilot_videos_per_participant,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "video_summary.csv", video_rows, VIDEO_FIELDS)
    write_csv(output_dir / "participant_summary.csv", participant_rows, PARTICIPANT_FIELDS)
    write_csv(output_dir / "scenario_summary.csv", scenario_rows, SCENARIO_FIELDS)
    write_csv(output_dir / "temporal_order_audit.csv", temporal_audit, TEMPORAL_AUDIT_FIELDS)
    write_csv(output_dir / "candidate_videos.csv", candidate_rows, CANDIDATE_FIELDS)
    write_csv(output_dir / "benchmark_manifest.csv", benchmark_rows, BENCHMARK_FIELDS)
    (output_dir / "benchmark_video_uids.txt").write_text(
        "".join(f"{row['video_uid']}\n" for row in benchmark_rows),
        encoding="utf-8",
    )

    participant_manifest_dir = output_dir / "participant_manifests"
    participant_manifest_dir.mkdir(parents=True, exist_ok=True)
    for stale in participant_manifest_dir.glob("*_all_videos.csv"):
        stale.unlink()
    benchmark_by_participant: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in benchmark_rows:
        benchmark_by_participant[str(row["participant_id"])].append(row)
    participant_manifest_index: list[dict[str, object]] = []
    for participant_id in sorted(benchmark_by_participant, key=str.casefold):
        rows = sorted(
            benchmark_by_participant[participant_id],
            key=lambda row: int(row["benchmark_session_order"]),
        )
        manifest_name = f"{benchmark_manifest_slug(participant_id)}_all_videos.csv"
        write_csv(participant_manifest_dir / manifest_name, rows, BENCHMARK_FIELDS)
        participant_manifest_index.append(
            {
                "participant_id": participant_id,
                "manifest_file": manifest_name,
                "video_count": len(rows),
                "total_duration_sec": sum(float(row["duration_sec"]) for row in rows),
                "first_benchmark_session_order": rows[0]["benchmark_session_order"],
                "last_benchmark_session_order": rows[-1]["benchmark_session_order"],
            }
        )
    write_csv(
        output_dir / "participant_manifest_index.csv",
        participant_manifest_index,
        PARTICIPANT_MANIFEST_INDEX_FIELDS,
    )
    write_csv(output_dir / "pilot_manifest.csv", pilot_rows, PILOT_FIELDS)
    (output_dir / "pilot_video_uids.txt").write_text(
        "".join(f"{row['video_uid']}\n" for row in pilot_rows),
        encoding="utf-8",
    )

    known_participants = [row for row in participant_rows if row["participant_known"]]
    eligible_candidates = [row for row in candidate_rows if row["candidate_status"] == "eligible"]
    report: dict[str, object] = {
        "dataset": "Ego4D",
        "metadata_version": metadata.get("version"),
        "metadata_date": metadata.get("date"),
        "video_count": len(video_rows),
        "total_duration_sec": sum(float(row["duration_sec"]) for row in video_rows),
        "total_duration_hours": sum(float(row["duration_sec"]) for row in video_rows) / 3600.0,
        "video_duration_sec_distribution": _distribution(
            [float(row["duration_sec"]) for row in video_rows]
        ),
        "known_participant_video_count": sum(bool(row["participant_known"]) for row in video_rows),
        "unknown_participant_video_count": sum(not bool(row["participant_known"]) for row in video_rows),
        "known_participant_count": len(known_participants),
        "known_participant_video_count_distribution": _distribution(
            [float(row["video_count"]) for row in known_participants]
        ),
        "multi_video_known_participant_count": sum(int(row["video_count"]) >= 2 for row in known_participants),
        "cross_video_consistency_eligible_participant_count": sum(
            bool(row["cross_video_consistency_eligible"]) for row in participant_rows
        ),
        "temporal_evolution_eligible_participant_count": sum(
            bool(row["temporal_evolution_eligible"]) for row in participant_rows
        ),
        "audio_video_count": sum(bool(row["has_audio"]) for row in video_rows),
        "redacted_video_count": sum(bool(row["has_redacted_regions"]) for row in video_rows),
        "redaction_extent_unknown_video_count": sum(
            row["redaction_measurement_status"] == "missing_intervals" for row in video_rows
        ),
        "concurrent_video_count": sum(bool(row["concurrent_video_set_count"]) for row in video_rows),
        "scenario_count": len(scenario_rows),
        "candidate_filter": filters.as_dict(),
        "eligible_candidate_video_count": len(eligible_candidates),
        "eligible_candidate_participant_count": len(
            {str(row["participant_id"]) for row in eligible_candidates}
        ),
        "benchmark_video_count": len(benchmark_rows),
        "benchmark_participant_count": len(benchmark_by_participant),
        "benchmark_total_duration_sec": sum(float(row["duration_sec"]) for row in benchmark_rows),
        "benchmark_total_duration_hours": sum(float(row["duration_sec"]) for row in benchmark_rows) / 3600.0,
        "pilot_participant_limit": pilot_participants,
        "pilot_videos_per_participant": pilot_videos_per_participant,
        "pilot_participant_count": len({str(row["participant_id"]) for row in pilot_rows}),
        "pilot_video_count": len(pilot_rows),
        "recommended_download_dataset": "video_540ss",
        "temporal_order_policy": {
            "canonical_video_timeline": "verified",
            "cross_canonical_video_order": "unknown",
            "infer_order_from_video_uid": False,
            "infer_order_from_origin_video_id": False,
            "infer_order_from_s3_path": False,
            "benchmark_presentation_order": "deterministic_video_uid_order",
            "benchmark_order_is_capture_chronology": False,
            "benchmark_order_defines_evaluation_timeline": True,
            "pilot_selection_rank_is_temporal": False,
        },
        "outputs": [
            "video_summary.csv",
            "participant_summary.csv",
            "scenario_summary.csv",
            "temporal_order_audit.csv",
            "candidate_videos.csv",
            "benchmark_manifest.csv",
            "benchmark_video_uids.txt",
            "participant_manifest_index.csv",
            "participant_manifests/",
            "pilot_manifest.csv",
            "pilot_video_uids.txt",
            "metadata_report.json",
        ],
    }
    (output_dir / "metadata_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Ego4D metadata and build full benchmark plus pilot manifests."
    )
    parser.add_argument("--metadata-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-duration-sec", type=float, default=300.0)
    parser.add_argument("--max-duration-sec", type=float, default=7200.0)
    parser.add_argument("--max-redaction-ratio", type=float, default=0.20)
    parser.add_argument("--min-videos-per-participant", type=int, default=3)
    parser.add_argument("--require-audio", action="store_true")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Require at least one exact scenario match; repeat for multiple values.",
    )
    parser.add_argument("--pilot-participants", type=int, default=5)
    parser.add_argument("--pilot-videos-per-participant", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    with args.metadata_json.open(encoding="utf-8") as file:
        metadata = json.load(file)
    if not isinstance(metadata, dict):
        raise SystemExit("ego4d.json root must be an object")

    filters = CandidateFilters(
        min_duration_sec=args.min_duration_sec,
        max_duration_sec=args.max_duration_sec,
        max_redaction_ratio=args.max_redaction_ratio,
        min_videos_per_participant=args.min_videos_per_participant,
        require_audio=args.require_audio,
        scenarios=tuple(args.scenario),
    )
    report = analyze_metadata(
        metadata,
        args.output_dir,
        filters,
        pilot_participants=args.pilot_participants,
        pilot_videos_per_participant=args.pilot_videos_per_participant,
    )
    print(f"Ego4D metadata version: {report['metadata_version']}")
    print(f"Videos: {report['video_count']}")
    print(f"Known participants: {report['known_participant_count']}")
    print(f"Eligible candidate videos: {report['eligible_candidate_video_count']}")
    print(
        "Benchmark after concurrent-view deduplication: "
        f"{report['benchmark_video_count']} videos / {report['benchmark_participant_count']} participants"
    )
    print(f"Pilot videos: {report['pilot_video_count']}")
    print("Real capture chronology remains unknown; benchmark presentation order is assigned.")
    print(f"Wrote metadata audit -> {args.output_dir}")


if __name__ == "__main__":
    main()
