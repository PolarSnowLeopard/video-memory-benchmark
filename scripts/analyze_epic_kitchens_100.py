#!/usr/bin/env python3
"""Lightweight EPIC-KITCHENS-100 metadata analysis.

This script intentionally uses only the Python standard library so it can run
without conda, pandas, or local package installs.
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median


ROOT = Path("data/external/epic-kitchens-100-annotations")
PROCESSED = Path("data/processed/epic_kitchens_100")
REPORTS = Path("reports/epic_kitchens_100")


def parse_timestamp(value: str) -> float:
    """Parse HH:MM:SS(.sss) into seconds."""
    if not value:
        return math.nan
    parts = value.strip().split(":")
    if len(parts) != 3:
        return math.nan
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def fmt_h(seconds: float) -> str:
    return f"{seconds / 3600:.2f}"


def fmt_m(seconds: float) -> str:
    return f"{seconds / 60:.2f}"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - rank) + ordered[hi] * (rank - lo)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def describe(values: list[float]) -> dict[str, float]:
    clean = [v for v in values if not math.isnan(v)]
    if not clean:
        return {
            "count": 0,
            "min": math.nan,
            "p25": math.nan,
            "median": math.nan,
            "mean": math.nan,
            "p75": math.nan,
            "max": math.nan,
        }
    return {
        "count": len(clean),
        "min": min(clean),
        "p25": percentile(clean, 0.25),
        "median": median(clean),
        "mean": mean(clean),
        "p75": percentile(clean, 0.75),
        "max": max(clean),
    }


def video_sequence_number(video_id: str) -> int:
    try:
        return int(video_id.split("_", 1)[1])
    except (IndexError, ValueError):
        return 0


def main() -> None:
    if not ROOT.exists():
        raise SystemExit(f"Missing annotation repo: {ROOT}")

    PROCESSED.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    video_rows = read_csv(ROOT / "EPIC_100_video_info.csv")
    train_rows = read_csv(ROOT / "EPIC_100_train.csv")
    val_rows = read_csv(ROOT / "EPIC_100_validation.csv")
    test_rows = read_csv(ROOT / "EPIC_100_test_timestamps.csv")

    videos: dict[str, dict[str, object]] = {}
    for row in video_rows:
        video_id = row["video_id"]
        participant_id = video_id.split("_")[0]
        videos[video_id] = {
            "video_id": video_id,
            "participant_id": participant_id,
            "duration_sec": float(row["duration"]),
            "duration_min": float(row["duration"]) / 60,
            "fps": float(row["fps"]),
            "resolution": row["resolution"],
            "train_actions": 0,
            "validation_actions": 0,
            "test_timestamps": 0,
            "labelled_actions": 0,
            "labelled_action_sec": 0.0,
            "unique_verbs": set(),
            "unique_nouns": set(),
            "unique_verb_classes": set(),
            "unique_noun_classes": set(),
        }

    labelled_rows: list[tuple[str, dict[str, str]]] = []
    for split, rows in [("train", train_rows), ("validation", val_rows)]:
        for row in rows:
            labelled_rows.append((split, row))
            video_id = row["video_id"]
            video = videos.get(video_id)
            if video is None:
                continue
            start = parse_timestamp(row["start_timestamp"])
            stop = parse_timestamp(row["stop_timestamp"])
            duration = stop - start if not math.isnan(start) and not math.isnan(stop) else math.nan
            video[f"{split}_actions"] = int(video[f"{split}_actions"]) + 1
            video["labelled_actions"] = int(video["labelled_actions"]) + 1
            if not math.isnan(duration) and duration >= 0:
                video["labelled_action_sec"] = float(video["labelled_action_sec"]) + duration
            cast_verbs = video["unique_verbs"]
            cast_nouns = video["unique_nouns"]
            cast_verb_classes = video["unique_verb_classes"]
            cast_noun_classes = video["unique_noun_classes"]
            assert isinstance(cast_verbs, set)
            assert isinstance(cast_nouns, set)
            assert isinstance(cast_verb_classes, set)
            assert isinstance(cast_noun_classes, set)
            cast_verbs.add(row["verb"])
            cast_nouns.add(row["noun"])
            cast_verb_classes.add(row["verb_class"])
            cast_noun_classes.add(row["noun_class"])

    for row in test_rows:
        video_id = row["video_id"]
        video = videos.get(video_id)
        if video is not None:
            video["test_timestamps"] = int(video["test_timestamps"]) + 1

    video_summary: list[dict[str, object]] = []
    for video_id, video in sorted(videos.items()):
        unique_verbs = video["unique_verbs"]
        unique_nouns = video["unique_nouns"]
        unique_verb_classes = video["unique_verb_classes"]
        unique_noun_classes = video["unique_noun_classes"]
        assert isinstance(unique_verbs, set)
        assert isinstance(unique_nouns, set)
        assert isinstance(unique_verb_classes, set)
        assert isinstance(unique_noun_classes, set)
        duration_min = float(video["duration_min"])
        labelled_actions = int(video["labelled_actions"])
        labelled_action_sec = float(video["labelled_action_sec"])
        video_summary.append(
            {
                "video_id": video_id,
                "participant_id": video["participant_id"],
                "duration_sec": f"{float(video['duration_sec']):.3f}",
                "duration_min": f"{duration_min:.3f}",
                "fps": f"{float(video['fps']):.3f}",
                "resolution": video["resolution"],
                "train_actions": video["train_actions"],
                "validation_actions": video["validation_actions"],
                "test_timestamps": video["test_timestamps"],
                "labelled_actions": labelled_actions,
                "unique_verb_labels": len(unique_verbs),
                "unique_noun_labels": len(unique_nouns),
                "unique_verb_classes": len(unique_verb_classes),
                "unique_noun_classes": len(unique_noun_classes),
                "actions_per_min": f"{labelled_actions / duration_min:.3f}" if duration_min else "",
                "labelled_action_coverage": f"{labelled_action_sec / float(video['duration_sec']):.3f}"
                if float(video["duration_sec"])
                else "",
            }
        )

    write_csv(
        PROCESSED / "video_summary.csv",
        video_summary,
        [
            "video_id",
            "participant_id",
            "duration_sec",
            "duration_min",
            "fps",
            "resolution",
            "train_actions",
            "validation_actions",
            "test_timestamps",
            "labelled_actions",
            "unique_verb_labels",
            "unique_noun_labels",
            "unique_verb_classes",
            "unique_noun_classes",
            "actions_per_min",
            "labelled_action_coverage",
        ],
    )

    by_participant: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "video_ids": set(),
            "duration_sec": 0.0,
            "train_actions": 0,
            "validation_actions": 0,
            "test_timestamps": 0,
            "labelled_actions": 0,
            "unique_verbs": set(),
            "unique_nouns": set(),
            "unique_verb_classes": set(),
            "unique_noun_classes": set(),
        }
    )
    for video in videos.values():
        participant_id = str(video["participant_id"])
        participant = by_participant[participant_id]
        cast_video_ids = participant["video_ids"]
        assert isinstance(cast_video_ids, set)
        cast_video_ids.add(video["video_id"])
        participant["duration_sec"] = float(participant["duration_sec"]) + float(video["duration_sec"])
        participant["train_actions"] = int(participant["train_actions"]) + int(video["train_actions"])
        participant["validation_actions"] = int(participant["validation_actions"]) + int(video["validation_actions"])
        participant["test_timestamps"] = int(participant["test_timestamps"]) + int(video["test_timestamps"])
        participant["labelled_actions"] = int(participant["labelled_actions"]) + int(video["labelled_actions"])
        cast_verbs = participant["unique_verbs"]
        cast_nouns = participant["unique_nouns"]
        cast_verb_classes = participant["unique_verb_classes"]
        cast_noun_classes = participant["unique_noun_classes"]
        video_verbs = video["unique_verbs"]
        video_nouns = video["unique_nouns"]
        video_verb_classes = video["unique_verb_classes"]
        video_noun_classes = video["unique_noun_classes"]
        assert isinstance(cast_verbs, set)
        assert isinstance(cast_nouns, set)
        assert isinstance(cast_verb_classes, set)
        assert isinstance(cast_noun_classes, set)
        assert isinstance(video_verbs, set)
        assert isinstance(video_nouns, set)
        assert isinstance(video_verb_classes, set)
        assert isinstance(video_noun_classes, set)
        cast_verbs.update(video_verbs)
        cast_nouns.update(video_nouns)
        cast_verb_classes.update(video_verb_classes)
        cast_noun_classes.update(video_noun_classes)

    participant_summary: list[dict[str, object]] = []
    for participant_id, participant in sorted(by_participant.items()):
        video_ids = participant["video_ids"]
        unique_verbs = participant["unique_verbs"]
        unique_nouns = participant["unique_nouns"]
        unique_verb_classes = participant["unique_verb_classes"]
        unique_noun_classes = participant["unique_noun_classes"]
        assert isinstance(video_ids, set)
        assert isinstance(unique_verbs, set)
        assert isinstance(unique_nouns, set)
        assert isinstance(unique_verb_classes, set)
        assert isinstance(unique_noun_classes, set)
        durations = [float(videos[video_id]["duration_sec"]) for video_id in video_ids]
        participant_summary.append(
            {
                "participant_id": participant_id,
                "video_count": len(video_ids),
                "total_duration_sec": f"{float(participant['duration_sec']):.3f}",
                "total_duration_h": fmt_h(float(participant["duration_sec"])),
                "mean_video_duration_min": fmt_m(mean(durations)),
                "median_video_duration_min": fmt_m(median(durations)),
                "train_actions": participant["train_actions"],
                "validation_actions": participant["validation_actions"],
                "test_timestamps": participant["test_timestamps"],
                "labelled_actions": participant["labelled_actions"],
                "unique_verb_labels": len(unique_verbs),
                "unique_noun_labels": len(unique_nouns),
                "unique_verb_classes": len(unique_verb_classes),
                "unique_noun_classes": len(unique_noun_classes),
            }
        )

    participant_summary.sort(
        key=lambda row: (float(row["total_duration_h"]), int(row["video_count"]), int(row["labelled_actions"])),
        reverse=True,
    )
    write_csv(
        PROCESSED / "participant_summary.csv",
        participant_summary,
        [
            "participant_id",
            "video_count",
            "total_duration_sec",
            "total_duration_h",
            "mean_video_duration_min",
            "median_video_duration_min",
            "train_actions",
            "validation_actions",
            "test_timestamps",
            "labelled_actions",
            "unique_verb_labels",
            "unique_noun_labels",
            "unique_verb_classes",
            "unique_noun_classes",
        ],
    )

    verb_counts: Counter[str] = Counter()
    noun_counts: Counter[str] = Counter()
    verb_class_counts: Counter[str] = Counter()
    noun_class_counts: Counter[str] = Counter()
    action_durations: list[float] = []
    for _, row in labelled_rows:
        verb_counts[row["verb"]] += 1
        noun_counts[row["noun"]] += 1
        verb_class_counts[row["verb_class"]] += 1
        noun_class_counts[row["noun_class"]] += 1
        start = parse_timestamp(row["start_timestamp"])
        stop = parse_timestamp(row["stop_timestamp"])
        if not math.isnan(start) and not math.isnan(stop) and stop >= start:
            action_durations.append(stop - start)

    write_csv(
        PROCESSED / "top_verbs.csv",
        [{"verb": verb, "count": count} for verb, count in verb_counts.most_common()],
        ["verb", "count"],
    )
    write_csv(
        PROCESSED / "top_nouns.csv",
        [{"noun": noun, "count": count} for noun, count in noun_counts.most_common()],
        ["noun", "count"],
    )

    candidate_rows: list[dict[str, object]] = []
    for row in participant_summary:
        video_count = int(row["video_count"])
        total_h = float(row["total_duration_h"])
        labelled_actions = int(row["labelled_actions"])
        unique_nouns = int(row["unique_noun_classes"])
        unique_verbs = int(row["unique_verb_classes"])
        score = total_h * 4 + video_count * 0.4 + unique_nouns * 0.05 + unique_verbs * 0.03
        if video_count >= 8 and total_h >= 1.0 and labelled_actions >= 500:
            candidate_rows.append(
                {
                    "participant_id": row["participant_id"],
                    "score": f"{score:.3f}",
                    "video_count": video_count,
                    "total_duration_h": row["total_duration_h"],
                    "labelled_actions": labelled_actions,
                    "unique_verb_classes": unique_verbs,
                    "unique_noun_classes": unique_nouns,
                    "suggested_use": "multi-session kitchen memory stream",
                }
            )
    candidate_rows.sort(key=lambda row: float(row["score"]), reverse=True)
    write_csv(
        PROCESSED / "candidate_participants.csv",
        candidate_rows,
        [
            "participant_id",
            "score",
            "video_count",
            "total_duration_h",
            "labelled_actions",
            "unique_verb_classes",
            "unique_noun_classes",
            "suggested_use",
        ],
    )

    top_candidate_ids = {str(row["participant_id"]) for row in candidate_rows[:15]}
    candidate_video_rows: list[dict[str, object]] = []
    for row in video_summary:
        if row["participant_id"] not in top_candidate_ids:
            continue
        duration_min = float(row["duration_min"])
        labelled_actions = int(row["labelled_actions"])
        noun_classes = int(row["unique_noun_classes"])
        verb_classes = int(row["unique_verb_classes"])
        if labelled_actions <= 0 or duration_min < 3:
            continue
        if 4 <= duration_min <= 12 and labelled_actions >= 60:
            suggested_use = "direct_session"
        elif duration_min > 12 and labelled_actions >= 120:
            suggested_use = "cut_into_sessions"
        elif labelled_actions >= 60:
            suggested_use = "short_support_session"
        else:
            suggested_use = "low_priority"
        candidate_video_rows.append(
            {
                "participant_id": row["participant_id"],
                "video_id": row["video_id"],
                "video_sequence": video_sequence_number(str(row["video_id"])),
                "duration_min": row["duration_min"],
                "labelled_actions": labelled_actions,
                "unique_verb_classes": verb_classes,
                "unique_noun_classes": noun_classes,
                "actions_per_min": row["actions_per_min"],
                "suggested_use": suggested_use,
            }
        )
    candidate_video_rows.sort(
        key=lambda row: (
            str(row["participant_id"]),
            int(row["video_sequence"]),
            -float(row["duration_min"]),
        )
    )
    write_csv(
        PROCESSED / "candidate_videos.csv",
        candidate_video_rows,
        [
            "participant_id",
            "video_id",
            "video_sequence",
            "duration_min",
            "labelled_actions",
            "unique_verb_classes",
            "unique_noun_classes",
            "actions_per_min",
            "suggested_use",
        ],
    )

    video_durations = [float(row["duration"]) for row in video_rows]
    action_stats = describe(action_durations)
    video_stats = describe(video_durations)

    report_lines = [
        "# EPIC-KITCHENS-100 元信息初筛报告",
        "",
        "## 数据范围",
        "",
        f"- 视频数：{len(video_rows)}",
        f"- 参与者数：{len(by_participant)}",
        f"- 视频总时长：{fmt_h(sum(video_durations))} 小时",
        f"- 训练动作片段：{len(train_rows)}",
        f"- 验证动作片段：{len(val_rows)}",
        f"- 测试时间戳片段：{len(test_rows)}",
        f"- 已标注动词官方类别：{len(verb_class_counts)}",
        f"- 已标注名词官方类别：{len(noun_class_counts)}",
        f"- 动词文本变体：{len(verb_counts)}",
        f"- 名词文本变体：{len(noun_counts)}",
        "",
        "## 视频时长分布",
        "",
        f"- 最短：{fmt_m(video_stats['min'])} 分钟",
        f"- 下四分位：{fmt_m(video_stats['p25'])} 分钟",
        f"- 中位数：{fmt_m(video_stats['median'])} 分钟",
        f"- 平均：{fmt_m(video_stats['mean'])} 分钟",
        f"- 上四分位：{fmt_m(video_stats['p75'])} 分钟",
        f"- 最长：{fmt_m(video_stats['max'])} 分钟",
        "",
        "## 动作片段时长分布",
        "",
        f"- 最短：{action_stats['min']:.2f} 秒",
        f"- 下四分位：{action_stats['p25']:.2f} 秒",
        f"- 中位数：{action_stats['median']:.2f} 秒",
        f"- 平均：{action_stats['mean']:.2f} 秒",
        f"- 上四分位：{action_stats['p75']:.2f} 秒",
        f"- 最长：{action_stats['max']:.2f} 秒",
        "",
        "## 最适合作为多会话记忆流的参与者（按时长优先）",
        "",
        "| 参与者 | 视频数 | 总时长/小时 | 已标注动作 | 动词类别 | 名词类别 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in participant_summary[:15]:
        report_lines.append(
            f"| {row['participant_id']} | {row['video_count']} | {row['total_duration_h']} | "
            f"{row['labelled_actions']} | {row['unique_verb_classes']} | {row['unique_noun_classes']} |"
        )

    report_lines.extend(
        [
            "",
            "## 高频动作",
            "",
            "| 动词 | 次数 |",
            "|---|---:|",
        ]
    )
    for verb, count in verb_counts.most_common(15):
        report_lines.append(f"| {verb} | {count} |")

    report_lines.extend(
        [
            "",
            "## 高频物体",
            "",
            "| 名词 | 次数 |",
            "|---|---:|",
        ]
    )
    for noun, count in noun_counts.most_common(15):
        report_lines.append(f"| {noun} | {count} |")

    report_lines.extend(
        [
            "",
            "## 首批视频下载建议",
            "",
            "| 参与者 | 建议视频 |",
            "|---|---|",
        ]
    )
    videos_by_candidate: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in candidate_video_rows:
        if row["suggested_use"] in {"direct_session", "cut_into_sessions"}:
            videos_by_candidate[str(row["participant_id"])].append(row)
    for row in candidate_rows[:8]:
        participant_id = str(row["participant_id"])
        videos_for_participant = videos_by_candidate.get(participant_id, [])[:10]
        video_ids = ", ".join(str(video["video_id"]) for video in videos_for_participant) or "暂无"
        report_lines.append(f"| {participant_id} | {video_ids} |")

    report_lines.extend(
        [
            "",
            "## 初步结论",
            "",
            "- EPIC-KITCHENS-100 的视频粒度适合切成单个会话；多数视频本身约数分钟到十几分钟。",
            "- 更适合先按参与者构造厨房长期记忆流，而不是跨参与者混合。",
            "- 首轮可以优先选择视频数多、总时长长、名词覆盖广的参与者，构造 5 到 10 个会话。",
            "- 后续下载视频时只需要下载候选参与者的少量视频，并先转成低清代理视频用于浏览和视觉语言模型标注。",
            "",
            "## 产物",
            "",
            "- `data/processed/epic_kitchens_100/video_summary.csv`",
            "- `data/processed/epic_kitchens_100/participant_summary.csv`",
            "- `data/processed/epic_kitchens_100/candidate_participants.csv`",
            "- `data/processed/epic_kitchens_100/candidate_videos.csv`",
            "- `data/processed/epic_kitchens_100/top_verbs.csv`",
            "- `data/processed/epic_kitchens_100/top_nouns.csv`",
        ]
    )

    (REPORTS / "metadata_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"Wrote {PROCESSED}")
    print(f"Wrote {REPORTS / 'metadata_report.md'}")


if __name__ == "__main__":
    main()
