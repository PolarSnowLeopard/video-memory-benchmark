#!/usr/bin/env python3
"""Shared helpers for hierarchical evidence quality control."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def candidate_review_fingerprint(
    record: dict[str, Any], candidate: dict[str, Any]
) -> str:
    """Bind a human decision to the exact candidate state that was reviewed."""
    first_pass = candidate.get("first_pass_verification") or {}
    local = candidate.get("local_verification") or {}
    payload = {
        "source_video_id": record.get("source_video_id"),
        "session_id": record.get("session_id"),
        "participant_id": record.get("participant_id"),
        "candidate_id": candidate.get("candidate_id"),
        "type": candidate.get("type"),
        "claim": candidate.get("claim"),
        "observed_value": candidate.get("observed_value"),
        "supporting_window_ids": candidate.get("supporting_window_ids") or [],
        "support_ranges": candidate.get("support_ranges") or [],
        "quality_flags": sorted(str(value) for value in candidate.get("quality_flags") or []),
        "qc_status": candidate.get("qc_status"),
        "usable_for_reference": candidate.get("usable_for_reference"),
        "first_pass_verification": first_pass,
        "local_verification": local,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
