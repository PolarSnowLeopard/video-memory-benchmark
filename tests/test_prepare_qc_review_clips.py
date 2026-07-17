import sys
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_qc_review_clips import (  # noqa: E402
    DEFAULT_CUT_MODE,
    DEFAULT_MAX_CLIPS_PER_CANDIDATE,
    DEFAULT_MIN_TAIL_SEC,
    build_clip_specs,
    delete_downloaded_sources,
    index_unique_rows,
    mapping_records,
    release_source_after_spec,
)


def review_item(candidate_id: str, start_sec: float, end_sec: float) -> dict:
    return {
        "record_id": f"P30_01:{candidate_id}",
        "source_video_id": "P30_01",
        "participant_id": "P30",
        "candidate_id": candidate_id,
        "claim": f"候选 {candidate_id}",
        "support_ranges": [{"start_sec": start_sec, "end_sec": end_sec}],
        "first_pass_verdict": "insufficient",
    }


class PrepareQcReviewClipsTests(unittest.TestCase):
    def test_default_cut_mode_is_frame_accurate_reencode(self) -> None:
        self.assertEqual(DEFAULT_CUT_MODE, "reencode")
        self.assertEqual(DEFAULT_MAX_CLIPS_PER_CANDIDATE, 16)
        self.assertEqual(DEFAULT_MIN_TAIL_SEC, 10.0)

    def test_duplicate_source_rows_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate source proxy"):
            index_unique_rows(
                [
                    {"video_id": "P30_01", "signed_url": "https://example.test/a"},
                    {"video_id": "P30_01", "signed_url": "https://example.test/b"},
                ],
                id_getter=lambda row: row["video_id"],
                label="source proxy",
            )

    def test_cleanup_deletes_only_paths_downloaded_by_this_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = root / "existing.mp4"
            downloaded = root / "downloaded.mp4"
            existing.write_bytes(b"existing")
            downloaded.write_bytes(b"downloaded")

            delete_downloaded_sources({downloaded})

            self.assertTrue(existing.exists())
            self.assertFalse(downloaded.exists())

    def test_downloaded_source_is_released_after_its_final_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "downloaded.mp4"
            source.write_bytes(b"downloaded")
            remaining = {"P30_01": 2}
            source_paths = {"P30_01": source}
            downloaded = {source}

            self.assertFalse(
                release_source_after_spec(
                    "P30_01",
                    remaining,
                    source_paths,
                    downloaded,
                    delete_source_after=True,
                    dry_run=False,
                )
            )
            self.assertTrue(source.exists())
            self.assertTrue(
                release_source_after_spec(
                    "P30_01",
                    remaining,
                    source_paths,
                    downloaded,
                    delete_source_after=True,
                    dry_run=False,
                )
            )
            self.assertFalse(source.exists())
            self.assertNotIn("P30_01", source_paths)
            self.assertEqual(downloaded, set())

    def test_script_help_runs_from_repository_root(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/prepare_qc_review_clips.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlapping_review_ranges_share_one_grid_clip(self) -> None:
        specs, mappings = build_clip_specs(
            [review_item("memcand_1", 31, 45), review_item("memcand_2", 40, 58)],
            clip_sec=30,
        )

        self.assertEqual([spec["clip_id"] for spec in specs], ["P30_01_qc_s00001"])
        self.assertEqual(specs[0]["start_sec"], 30.0)
        self.assertEqual(specs[0]["end_sec"], 60.0)
        self.assertEqual(specs[0]["candidate_ids"], ["memcand_1", "memcand_2"])
        self.assertEqual(mappings["P30_01:memcand_1"], ["P30_01_qc_s00001"])
        self.assertEqual(mappings["P30_01:memcand_2"], ["P30_01_qc_s00001"])

    def test_long_support_range_is_covered_by_consecutive_grid_clips(self) -> None:
        specs, mappings = build_clip_specs(
            [review_item("memcand_1", 0, 120)],
            clip_sec=30,
        )

        self.assertEqual(len(specs), 4)
        self.assertEqual(
            mappings["P30_01:memcand_1"],
            [
                "P30_01_qc_s00000",
                "P30_01_qc_s00001",
                "P30_01_qc_s00002",
                "P30_01_qc_s00003",
            ],
        )

    def test_short_tail_is_shifted_to_cover_source_end(self) -> None:
        specs, mappings = build_clip_specs(
            [review_item("memcand_1", 1080, 1141.76)],
            clip_sec=30,
            source_durations={"P30_01": 1141.76},
            min_tail_sec=10,
        )

        self.assertEqual(len(specs), 3)
        self.assertEqual(mappings["P30_01:memcand_1"][-1], "P30_01_qc_s00038")
        self.assertAlmostEqual(specs[-1]["start_sec"], 1111.76)
        self.assertAlmostEqual(specs[-1]["end_sec"], 1141.76)
        self.assertAlmostEqual(specs[-1]["duration_sec"], 30.0)

    def test_valid_tail_is_clamped_without_shifting(self) -> None:
        specs, _ = build_clip_specs(
            [review_item("memcand_1", 30, 74.875)],
            clip_sec=30,
            source_durations={"P30_01": 74.875},
            min_tail_sec=10,
        )

        self.assertEqual(specs[-1]["start_sec"], 60.0)
        self.assertEqual(specs[-1]["end_sec"], 74.875)
        self.assertAlmostEqual(specs[-1]["duration_sec"], 14.875)

    def test_oversized_support_range_is_uniformly_sampled(self) -> None:
        item = review_item("memcand_1", 0, 3000)

        specs, mappings = build_clip_specs(
            [item], clip_sec=30, max_clips_per_candidate=16
        )

        selected = mappings["P30_01:memcand_1"]
        self.assertEqual(len(selected), 16)
        self.assertEqual(selected[0], "P30_01_qc_s00000")
        self.assertEqual(selected[-1], "P30_01_qc_s00099")
        self.assertEqual(len(specs), 16)
        records = mapping_records([item], mappings, specs, clip_sec=30)
        self.assertEqual(
            records[0]["clip_selection"],
            {
                "strategy": "uniform_grid_cap",
                "is_exhaustive": False,
                "available_clip_count": 100,
                "selected_clip_count": 16,
            },
        )

    def test_empty_or_reversed_support_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid support range"):
            build_clip_specs([review_item("memcand_1", 60, 30)], clip_sec=30)


if __name__ == "__main__":
    unittest.main()
