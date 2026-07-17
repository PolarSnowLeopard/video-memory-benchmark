import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from scripts.run_epic_vpn_session_batch import (
    DEFAULT_CUT_MODE,
    DEFAULT_DOWNLOAD_ATTEMPTS,
    DEFAULT_MAX_DURATION_ERROR_SEC,
    DEFAULT_MAX_SOURCE_DURATION_ERROR_SEC,
    DEFAULT_REENCODE_CRF,
    DEFAULT_REENCODE_PRESET,
    benchmark_order_metadata,
    completed_session_ids,
    expected_source_duration,
    ffmpeg_can_decode_frame_at,
    is_source_integrity_error,
    parse_ffmpeg_duration,
    remove_csv_row,
    source_duration,
    validate_session_duration,
)


class SessionBatchTests(unittest.TestCase):
    def test_frame_accurate_defaults(self) -> None:
        self.assertEqual(DEFAULT_CUT_MODE, "reencode")
        self.assertEqual(DEFAULT_REENCODE_CRF, 23)
        self.assertEqual(DEFAULT_REENCODE_PRESET, "veryfast")
        self.assertEqual(DEFAULT_MAX_DURATION_ERROR_SEC, 0.25)
        self.assertEqual(DEFAULT_MAX_SOURCE_DURATION_ERROR_SEC, 1.0)
        self.assertEqual(DEFAULT_DOWNLOAD_ATTEMPTS, 3)

    def test_expected_source_duration_prefers_manifest(self) -> None:
        self.assertEqual(
            expected_source_duration(
                {"video_id": "P01_01", "duration_sec": "12.5"},
                {"P01_01": 99.0},
            ),
            12.5,
        )

    def test_preserves_benchmark_order_metadata(self) -> None:
        self.assertEqual(
            benchmark_order_metadata(
                {
                    "benchmark_session_order": "3",
                    "benchmark_order_status": "assigned",
                    "benchmark_order_basis": "deterministic_video_uid_order",
                    "benchmark_temporal_evolution_eligible": "true",
                }
            ),
            {
                "benchmark_session_order": "3",
                "benchmark_order_status": "assigned",
                "benchmark_order_basis": "deterministic_video_uid_order",
                "benchmark_temporal_evolution_eligible": "true",
            },
        )

    def test_parse_ffmpeg_duration(self) -> None:
        output = "Duration: 01:02:03.45, start: 0.000000, bitrate: 1000 kb/s"
        self.assertAlmostEqual(parse_ffmpeg_duration(output), 3723.45)

    @patch("scripts.run_epic_vpn_session_batch.subprocess.run")
    def test_tail_probe_requires_a_decoded_frame(self, run_mock) -> None:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = "frame=0\nprogress=end\n"
        self.assertFalse(ffmpeg_can_decode_frame_at(Path("proxy.mp4"), 95.0, "ffmpeg"))

        run_mock.return_value.stdout = "frame=1\nprogress=end\n"
        self.assertTrue(ffmpeg_can_decode_frame_at(Path("proxy.mp4"), 95.0, "ffmpeg"))

    def test_source_duration_rejects_missing_media_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxy.mp4"
            path.touch()
            with (
                patch(
                    "scripts.run_epic_vpn_session_batch.ffmpeg_container_duration",
                    return_value=100.0,
                ),
                patch(
                    "scripts.run_epic_vpn_session_batch.ffmpeg_can_decode_frame_at",
                    return_value=False,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "media tail missing"):
                    source_duration(
                        {"video_id": "P01_01", "duration_sec": "100"},
                        {},
                        path,
                        False,
                    )

    def test_media_tail_failure_is_retryable_integrity_error(self) -> None:
        self.assertTrue(
            is_source_integrity_error(
                RuntimeError("Source proxy media tail missing: video=P01_01")
            )
        )

    def test_duration_validation_rejects_gop_overrun(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duration mismatch"):
            validate_session_duration(44.64, 30.0, 0.25)

    def test_completed_sessions_must_have_duration_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "status.csv"
            urls = root / "urls.csv"
            status.write_text(
                "session_id,status,duration_validated\n"
                "clip_valid,ok,True\n"
                "clip_legacy,ok,\n",
                encoding="utf-8",
            )
            urls.write_text(
                "session_id,signed_url,duration_validated\n"
                "clip_valid,http://localhost/valid.mp4,True\n"
                "clip_legacy,http://localhost/legacy.mp4,\n",
                encoding="utf-8",
            )

            self.assertEqual(
                completed_session_ids(status, urls, require_url=True),
                {"clip_valid"},
            )

    def test_remove_csv_row_drops_stale_inference_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "urls.csv"
            path.write_text(
                "session_id,signed_url,duration_validated\n"
                "clip_bad,http://localhost/bad.mp4,\n"
                "clip_good,http://localhost/good.mp4,True\n",
                encoding="utf-8",
            )

            remove_csv_row(
                path,
                ["session_id", "signed_url", "duration_validated"],
                "session_id",
                "clip_bad",
            )

            self.assertNotIn("clip_bad", path.read_text(encoding="utf-8"))
            self.assertIn("clip_good", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
