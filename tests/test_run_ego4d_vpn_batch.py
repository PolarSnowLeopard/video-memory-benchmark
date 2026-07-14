import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_ego4d_vpn_batch import (  # noqa: E402
    MediaProbe,
    URL_FIELDS,
    completed_video_uids,
    default_run_name,
    load_manifest,
    response_content_length,
    scale_filter,
    validate_proxy,
    write_csv,
)


class Ego4DVpnBatchTests(unittest.TestCase):
    def test_load_manifest_requires_unique_safe_video_uids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.csv"
            path.write_text(
                "video_uid,participant_id\nuid-1,EGO4D_P000001\nuid-2,EGO4D_P000001\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [row["video_uid"] for row in load_manifest(path)],
                ["uid-1", "uid-2"],
            )

            path.write_text(
                "video_uid,participant_id\nuid-1,EGO4D_P000001\nuid-1,EGO4D_P000001\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Duplicate video_uid"):
                load_manifest(path)

            path.write_text(
                "video_uid,participant_id\n../uid,EGO4D_P000001\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Unsafe video_uid"):
                load_manifest(path)

    def test_scale_filter_keeps_short_side_and_sets_fps(self) -> None:
        self.assertEqual(
            scale_filter(540, 16.0),
            "scale=w='if(gte(iw,ih),-2,540)':h='if(gte(iw,ih),540,-2)',fps=16",
        )

    def test_validate_proxy_accepts_expected_media_contract(self) -> None:
        source = MediaProbe(600.0, "vp9", 720, 540, 30.0, "aac")
        proxy = MediaProbe(600.03, "h264", 720, 540, 16.0, "aac")

        validate_proxy(source, proxy, short_side=540, fps=16.0)

    def test_validate_proxy_reports_contract_violations(self) -> None:
        source = MediaProbe(600.0, "vp9", 720, 540, 30.0, "aac")
        proxy = MediaProbe(590.0, "hevc", 640, 360, 15.0, None)

        with self.assertRaisesRegex(ValueError, "video codec.*short side.*average fps"):
            validate_proxy(source, proxy, short_side=540, fps=16.0)

    def test_completed_videos_require_status_and_uploaded_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "status.csv"
            url_path = root / "urls.csv"
            with status_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["video_uid", "status"])
                writer.writeheader()
                writer.writerows(
                    [
                        {"video_uid": "uid-1", "status": "ok"},
                        {"video_uid": "uid-2", "status": "ok"},
                        {"video_uid": "uid-3", "status": "error"},
                    ]
                )
            write_csv(
                url_path,
                [
                    {"video_uid": "uid-1", "signed_url": "https://example.test/1"},
                    {"video_uid": "uid-3", "signed_url": "https://example.test/3"},
                ],
                URL_FIELDS,
            )

            self.assertEqual(
                completed_video_uids(status_path, url_path, skip_upload=False),
                {"uid-1"},
            )
            self.assertEqual(
                completed_video_uids(status_path, url_path, skip_upload=True),
                {"uid-1", "uid-2"},
            )

    def test_cos_content_length_is_case_and_dash_insensitive(self) -> None:
        self.assertEqual(response_content_length({"Content-Length": "123"}), 123)
        self.assertEqual(response_content_length({"contentlength": 456}), 456)
        self.assertIsNone(response_content_length({"ETag": "abc"}))

    def test_default_run_name_includes_manifest_parent(self) -> None:
        self.assertEqual(
            default_run_name(Path("data/processed/ego4d cooking/pilot manifest.csv")),
            "ego4d_cooking_pilot_manifest",
        )


if __name__ == "__main__":
    unittest.main()
