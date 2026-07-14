import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.analyze_ego4d_metadata import (  # noqa: E402
    CandidateFilters,
    analyze_metadata,
    build_candidate_rows,
    build_concurrent_video_index,
    build_participant_summaries,
    build_temporal_order_audit,
    normalize_video,
    select_benchmark_rows,
    select_pilot_rows,
)


def make_video(
    video_uid: str,
    participant_id: int | None,
    *,
    duration_sec: float = 600.0,
    scenarios: list[str] | None = None,
    audio_duration_sec: float | None = 590.0,
    has_redacted_regions: bool = False,
    redacted_intervals: list[dict[str, float]] | None = None,
) -> dict[str, object]:
    return {
        "video_uid": video_uid,
        "duration_sec": duration_sec,
        "scenarios": scenarios if scenarios is not None else ["Cooking"],
        "video_metadata": {
            "fps": 30.0,
            "num_frames": int(duration_sec * 30),
            "video_codec": "vp9",
            "display_resolution_width": 960,
            "display_resolution_height": 540,
            "sample_resolution_width": 960,
            "sample_resolution_height": 540,
            "mp4_duration_sec": duration_sec,
            "video_start_sec": 0.0,
            "video_duration_sec": duration_sec,
            "audio_start_sec": 0.5 if audio_duration_sec is not None else None,
            "audio_duration_sec": audio_duration_sec,
        },
        "split_em": "train",
        "split_av": None,
        "split_fho": "train",
        "s3_path": f"s3://ego4d/{video_uid}.mp4",
        "origin_video_id": f"origin-{video_uid}",
        "video_source": "cmu",
        "device": "GoPro",
        "physical_setting_name": "kitchen-1",
        "fb_participant_id": participant_id,
        "is_stereo": False,
        "has_imu": True,
        "has_gaze": False,
        "video_components": [
            {
                "video_component_uid": f"{video_uid}-component-1",
                "video_uid": video_uid,
                "component_idx": 1,
                "canonical_video_start_sec": duration_sec / 2,
                "canonical_video_end_sec": duration_sec,
            },
            {
                "video_component_uid": f"{video_uid}-component-0",
                "video_uid": video_uid,
                "component_idx": 0,
                "canonical_video_start_sec": 0.0,
                "canonical_video_end_sec": duration_sec / 2,
            },
        ],
        "has_redacted_regions": has_redacted_regions,
        "redacted_intervals": redacted_intervals or [],
    }


def make_metadata() -> dict[str, object]:
    video_a = make_video(
        "video-a",
        7,
        duration_sec=600.0,
        redacted_intervals=[
            {"start_sec": 10.0, "end_sec": 20.0},
            {"start_sec": 15.0, "end_sec": 25.0},
            {"start_sec": 599.0, "end_sec": 620.0},
        ],
        has_redacted_regions=True,
    )
    video_b = make_video("video-b", 7, duration_sec=480.0, audio_duration_sec=None)
    video_c = make_video("video-c", None, duration_sec=300.0)
    video_d = make_video("video-d", None, duration_sec=300.0)
    video_e = make_video("video-e", 8, duration_sec=240.0)
    return {
        "date": "2026-07-14",
        "version": "v2.1",
        "description": "synthetic metadata",
        "videos": [video_a, video_b, video_c, video_d, video_e],
        "concurrent_video_sets": [
            {
                "concurrent_video_set_id": 42,
                "valid": True,
                "videos": [
                    {"video_uid": "video-a", "video_start_offset_sec": 0.0},
                    {"video_uid": "video-c", "video_start_offset_sec": 1.2},
                ],
            },
            {
                "concurrent_video_set_id": 99,
                "valid": False,
                "videos": [{"video_uid": "video-b", "video_start_offset_sec": 0.0}],
            },
        ],
    }


class Ego4DMetadataAnalysisTests(unittest.TestCase):
    def normalized_rows(self) -> list[dict[str, object]]:
        metadata = make_metadata()
        concurrent_index = build_concurrent_video_index(metadata)
        return [normalize_video(video, concurrent_index) for video in metadata["videos"]]

    def test_normalizes_audio_redactions_components_and_concurrency(self) -> None:
        rows = self.normalized_rows()
        video = next(row for row in rows if row["video_uid"] == "video-a")

        self.assertEqual(video["participant_id"], "EGO4D_P000007")
        self.assertTrue(video["participant_known"])
        self.assertEqual(video["component_indices"], [0, 1])
        self.assertEqual(video["within_video_order_status"], "verified_canonical_timeline")
        self.assertAlmostEqual(video["audio_coverage_ratio"], 590.0 / 600.0)
        self.assertAlmostEqual(video["redacted_duration_sec"], 16.0)
        self.assertAlmostEqual(video["redacted_ratio"], 16.0 / 600.0)
        self.assertEqual(video["redaction_measurement_status"], "measured")
        self.assertEqual(video["concurrent_video_set_ids"], [42])

    def test_unknown_participants_are_never_grouped_together(self) -> None:
        rows = self.normalized_rows()
        unknown = [row for row in rows if not row["participant_known"]]
        summaries = build_participant_summaries(rows)

        self.assertEqual(len({row["participant_id"] for row in unknown}), 2)
        for row in unknown:
            summary = next(item for item in summaries if item["participant_id"] == row["participant_id"])
            self.assertEqual(summary["video_count"], 1)
            self.assertFalse(summary["cross_video_consistency_eligible"])
            self.assertFalse(summary["temporal_evolution_eligible"])

    def test_cross_video_order_is_not_inferred_from_ids_or_components(self) -> None:
        rows = self.normalized_rows()
        summaries = build_participant_summaries(rows)
        audit = build_temporal_order_audit(summaries)
        p7 = next(row for row in audit if row["participant_id"] == "EGO4D_P000007")
        p8 = next(row for row in audit if row["participant_id"] == "EGO4D_P000008")

        self.assertEqual(p7["canonical_video_count"], 2)
        self.assertEqual(p7["cross_video_order_status"], "unknown")
        self.assertEqual(p7["cross_video_order_basis"], "none")
        self.assertFalse(p7["session_order_assigned"])
        self.assertFalse(p7["temporal_evolution_eligible"])
        self.assertTrue(p7["cross_video_consistency_eligible"])
        self.assertEqual(p8["cross_video_order_status"], "not_applicable_single_video")

    def test_unknown_redaction_extent_is_excluded_from_candidates(self) -> None:
        video = make_video(
            "video-redacted-unknown",
            9,
            has_redacted_regions=True,
            redacted_intervals=[],
        )
        companion = make_video("video-redacted-companion", 9)
        rows = [normalize_video(item, {}) for item in (video, companion)]
        summaries = build_participant_summaries(rows)
        candidates = build_candidate_rows(
            rows,
            summaries,
            CandidateFilters(min_videos_per_participant=1),
        )
        candidate = next(
            row for row in candidates if row["video_uid"] == "video-redacted-unknown"
        )

        self.assertEqual(candidate["candidate_status"], "excluded")
        self.assertIn("redaction_extent_unknown", candidate["exclusion_reasons"])

    def test_pilot_does_not_select_two_concurrent_views_for_one_participant(self) -> None:
        base = {
            "participant_id": "EGO4D_P000007",
            "candidate_status": "eligible",
            "duration_sec": 600.0,
        }
        candidates = [
            {**base, "video_uid": "video-a", "concurrent_video_set_ids": [42]},
            {**base, "video_uid": "video-b", "concurrent_video_set_ids": [42]},
            {**base, "video_uid": "video-c", "concurrent_video_set_ids": []},
        ]

        pilot = select_pilot_rows(candidates, participant_limit=1, videos_per_participant=3)

        self.assertEqual([row["video_uid"] for row in pilot], ["video-a", "video-c"])

    def test_benchmark_rows_receive_reproducible_presentation_order(self) -> None:
        rows = self.normalized_rows()
        summaries = build_participant_summaries(rows)
        candidates = build_candidate_rows(
            rows,
            summaries,
            CandidateFilters(
                min_duration_sec=300.0,
                max_duration_sec=900.0,
                max_redaction_ratio=0.20,
                min_videos_per_participant=2,
                require_audio=False,
                scenarios=("Cooking",),
            ),
        )
        benchmark = select_benchmark_rows(candidates)
        pilot = select_pilot_rows(benchmark, participant_limit=1, videos_per_participant=2)

        eligible = [row for row in candidates if row["candidate_status"] == "eligible"]
        self.assertEqual([row["video_uid"] for row in eligible], ["video-a", "video-b"])
        self.assertEqual(
            {row["independent_eligible_video_count_for_participant"] for row in eligible},
            {2},
        )
        self.assertEqual([row["video_uid"] for row in benchmark], ["video-a", "video-b"])
        self.assertEqual([row["benchmark_session_order"] for row in benchmark], [1, 2])
        self.assertTrue(all(row["cross_video_session_order"] is None for row in benchmark))
        self.assertTrue(all(row["cross_video_order_status"] == "unknown" for row in benchmark))
        self.assertTrue(all(row["benchmark_order_status"] == "assigned" for row in benchmark))
        self.assertTrue(all(row["benchmark_temporal_evolution_eligible"] for row in benchmark))
        self.assertEqual([row["video_uid"] for row in pilot], ["video-a", "video-b"])
        self.assertTrue(all(not row["pilot_selection_rank_is_temporal"] for row in pilot))

    def test_analysis_writes_reproducible_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "processed"
            report = analyze_metadata(
                make_metadata(),
                output_dir,
                CandidateFilters(
                    min_duration_sec=300.0,
                    max_duration_sec=900.0,
                    max_redaction_ratio=0.20,
                    min_videos_per_participant=2,
                ),
                pilot_participants=1,
                pilot_videos_per_participant=2,
            )

            expected_files = {
                "video_summary.csv",
                "participant_summary.csv",
                "scenario_summary.csv",
                "temporal_order_audit.csv",
                "candidate_videos.csv",
                "benchmark_manifest.csv",
                "benchmark_video_uids.txt",
                "participant_manifest_index.csv",
                "participant_manifests",
                "pilot_manifest.csv",
                "pilot_video_uids.txt",
                "metadata_report.json",
            }
            self.assertEqual({path.name for path in output_dir.iterdir()}, expected_files)
            self.assertEqual(report["video_count"], 5)
            self.assertEqual(report["known_participant_count"], 2)
            self.assertEqual(report["multi_video_known_participant_count"], 1)
            self.assertEqual(report["temporal_evolution_eligible_participant_count"], 0)
            self.assertEqual(report["benchmark_video_count"], 2)
            self.assertEqual(report["benchmark_participant_count"], 1)
            self.assertEqual(report["scenario_count"], 1)
            self.assertEqual(report["video_duration_sec_distribution"]["median"], 300.0)
            self.assertEqual(report["pilot_video_count"], 2)

            with (output_dir / "pilot_manifest.csv").open(newline="", encoding="utf-8") as f:
                pilot_rows = list(csv.DictReader(f))
            self.assertEqual([row["video_uid"] for row in pilot_rows], ["video-a", "video-b"])
            self.assertEqual({row["cross_video_session_order"] for row in pilot_rows}, {""})
            self.assertEqual([row["benchmark_session_order"] for row in pilot_rows], ["1", "2"])
            self.assertEqual({row["benchmark_order_status"] for row in pilot_rows}, {"assigned"})
            self.assertEqual({row["pilot_selection_rank_is_temporal"] for row in pilot_rows}, {"false"})
            self.assertEqual(
                (output_dir / "pilot_video_uids.txt").read_text(encoding="utf-8"),
                "video-a\nvideo-b\n",
            )
            persisted = json.loads((output_dir / "metadata_report.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, report)

            participant_manifests = list((output_dir / "participant_manifests").glob("*.csv"))
            self.assertEqual([path.name for path in participant_manifests], ["ego4d_p000007_all_videos.csv"])


if __name__ == "__main__":
    unittest.main()
