import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_ego4d_download_access import (  # noqa: E402
    audit_download_access,
    classify_head_error,
)


class Ego4DDownloadAccessAuditTests(unittest.TestCase):
    def test_authentication_failure_is_not_treated_as_object_forbidden(self) -> None:
        class FakeClientError(RuntimeError):
            response = {"Error": {"Code": "ExpiredToken"}}

        self.assertEqual(classify_head_error(FakeClientError("403")), "error")

    def test_audits_missing_available_and_forbidden_objects(self) -> None:
        benchmark = [
            {
                "video_uid": "available",
                "participant_id": "EGO4D_P000001",
                "benchmark_session_order": "1",
            },
            {
                "video_uid": "forbidden",
                "participant_id": "EGO4D_P000001",
                "benchmark_session_order": "2",
            },
            {
                "video_uid": "missing",
                "participant_id": "EGO4D_P000001",
                "benchmark_session_order": "3",
            },
        ]
        download = [
            {
                "video_uid": "available",
                "s3_path": "s3://bucket/available.mp4",
            },
            {
                "video_uid": "forbidden",
                "s3_path": "s3://bucket/forbidden.mp4",
            },
        ]

        def head_object(_bucket: str, key: str) -> dict[str, object]:
            if key == "forbidden.mp4":
                raise RuntimeError(
                    "An error occurred (403) when calling HeadObject: Forbidden"
                )
            return {"ContentLength": 123}

        rows = audit_download_access(
            benchmark,
            download,
            head_object,
            workers=2,
            checked_at="2026-07-20T00:00:00+00:00",
        )

        self.assertEqual(
            [row["status"] for row in rows],
            ["available", "forbidden", "not_in_download_manifest"],
        )
        self.assertEqual(rows[0]["size_bytes"], 123)
        self.assertEqual(rows[1]["bucket"], "bucket")
        self.assertEqual(rows[2]["bucket"], "")


if __name__ == "__main__":
    unittest.main()
