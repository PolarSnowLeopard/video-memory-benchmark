#!/usr/bin/env python3
"""Small dependency-free helpers for terminal pipeline progress."""

from __future__ import annotations

import math


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "--"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def progress_line(
    completed: int,
    total: int,
    elapsed_seconds: float,
    *,
    prefix: str = "Progress",
    width: int = 24,
) -> str:
    if total < 0 or completed < 0 or completed > total:
        raise ValueError("Progress counts must satisfy 0 <= completed <= total")
    fraction = completed / total if total else 1.0
    filled = min(width, int(fraction * width))
    bar = "#" * filled + "-" * (width - filled)
    if completed == 0:
        eta_seconds = None
    elif completed >= total:
        eta_seconds = 0.0
    else:
        eta_seconds = elapsed_seconds * (total - completed) / completed
    return (
        f"{prefix} [{bar}] {completed}/{total} ({fraction * 100:5.1f}%) "
        f"elapsed={format_duration(elapsed_seconds)} eta={format_duration(eta_seconds)}"
    )
