import tempfile
import unittest
from pathlib import Path


from scripts.run_epic_vpn_session_batch import (
    DEFAULT_CUT_MODE,
    DEFAULT_MAX_DURATION_ERROR_SEC,
    DEFAULT_REENCODE_CRF,
    completed_session_ids,
    parse_ffmpeg_duration,
    remove_csv_row,
    validate_session_duration,
)


class SessionBatchTests(unittest.TestCase):
    def test_frame_accurate_defaults(self) -> None:
        self.assertEqual(DEFAULT_CUT_MODE, "reencode")
        self.assertEqual(DEFAULT_REENCODE_CRF, 23)
        self.assertEqual(DEFAULT_MAX_DURATION_ERROR_SEC, 0.25)

    def test_parse_ffmpeg_duration(self) -> None:
        output = "Duration: 01:02:03.45, start: 0.000000, bitrate: 1000 kb/s"
        self.assertAlmostEqual(parse_ffmpeg_duration(output), 3723.45)

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
