import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.select_session_smoke_ids import select_midpoint_session_ids  # noqa: E402


class SelectSessionSmokeIdsTests(unittest.TestCase):
    def test_selects_one_midpoint_per_source_in_stable_order(self) -> None:
        rows = [
            {
                "source_video_id": "video_b",
                "session_id": "video_b_s001",
                "session_index": "1",
                "start_sec": "30",
            },
            {
                "source_video_id": "video_a",
                "session_id": "video_a_s002",
                "session_index": "2",
                "start_sec": "60",
            },
            {
                "source_video_id": "video_a",
                "session_id": "video_a_s000",
                "session_index": "0",
                "start_sec": "0",
            },
            {
                "source_video_id": "video_a",
                "session_id": "video_a_s001",
                "session_index": "1",
                "start_sec": "30",
            },
            {
                "source_video_id": "video_b",
                "session_id": "video_b_s000",
                "session_index": "0",
                "start_sec": "0",
            },
        ]

        self.assertEqual(
            select_midpoint_session_ids(rows),
            ["video_a_s001", "video_b_s001"],
        )

    def test_rejects_duplicate_session_ids(self) -> None:
        rows = [
            {"source_video_id": "video_a", "session_id": "same"},
            {"source_video_id": "video_b", "session_id": "same"},
        ]

        with self.assertRaisesRegex(ValueError, "Duplicate session id"):
            select_midpoint_session_ids(rows)


if __name__ == "__main__":
    unittest.main()
