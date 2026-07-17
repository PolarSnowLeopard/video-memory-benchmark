import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.refresh_cos_signed_urls import refresh_rows  # noqa: E402


class FakeCosClient:
    def get_presigned_url(self, **kwargs):
        return (
            f"https://example.test/{kwargs['Key']}?"
            f"bucket={kwargs['Bucket']}&expires={kwargs['Expired']}"
        )


class RefreshCosSignedUrlsTests(unittest.TestCase):
    def test_refreshes_get_urls_without_changing_object_identity(self) -> None:
        rows = [
            {
                "participant_id": "P01",
                "video_id": "P01_01",
                "bucket": "bucket-1",
                "region": "region-1",
                "key": "proxy/P01_01.mp4",
                "signed_url": "https://expired.test",
            }
        ]

        refreshed = refresh_rows(
            rows,
            FakeCosClient(),
            default_bucket="bucket-1",
            default_region="region-1",
            expire_seconds=21 * 24 * 3600,
        )

        self.assertEqual(refreshed[0]["key"], rows[0]["key"])
        self.assertIn("expires=1814400", refreshed[0]["signed_url"])
        self.assertEqual(rows[0]["signed_url"], "https://expired.test")

    def test_rejects_duplicate_objects(self) -> None:
        rows = [
            {"video_id": "a", "key": "same.mp4"},
            {"video_id": "b", "key": "same.mp4"},
        ]
        with self.assertRaisesRegex(ValueError, "Duplicate COS object"):
            refresh_rows(
                rows,
                FakeCosClient(),
                default_bucket="bucket-1",
                default_region="region-1",
                expire_seconds=3600,
            )


if __name__ == "__main__":
    unittest.main()
