import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_hierarchical_evidence_inputs import (  # noqa: E402
    build_session_records,
    build_window_records,
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class HierarchicalEvidenceInputTests(unittest.TestCase):
    def test_build_window_records_groups_micro_clips_by_source_video_and_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clean_dir = root / "micro"
            rows = [
                {
                    "session_id": "P30_01_s000",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "start_sec": "0",
                    "end_sec": "30",
                    "duration_sec": "30",
                    "benchmark_session_order": "2",
                    "benchmark_order_status": "assigned",
                    "benchmark_order_basis": "deterministic_video_uid_order",
                    "benchmark_temporal_evolution_eligible": "true",
                },
                {
                    "session_id": "P30_01_s001",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "start_sec": "30",
                    "end_sec": "60",
                    "duration_sec": "30",
                },
                {
                    "session_id": "P30_01_s002",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "start_sec": "60",
                    "end_sec": "90",
                    "duration_sec": "30",
                },
                {
                    "session_id": "P30_01_s003",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "start_sec": "90",
                    "end_sec": "120",
                    "duration_sec": "30",
                },
                {
                    "session_id": "P30_01_s004",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "start_sec": "120",
                    "end_sec": "135",
                    "duration_sec": "15",
                },
            ]
            for row in rows:
                write_json(
                    clean_dir / f"{row['session_id']}.clean.json",
                    {
                        "clip_id": row["session_id"],
                        "facts": [row["start_sec"]],
                        "confidence": "high",
                        "nested": {"trackability": "high", "evidence": "画面可见"},
                    },
                )

            records = build_window_records(rows, clean_dir, window_sec=120, allow_missing=False)

        self.assertEqual([record["record_id"] for record in records], ["P30_01_w000", "P30_01_w001"])
        self.assertEqual(records[0]["start_sec"], 0.0)
        self.assertEqual(records[0]["benchmark_session_order"], 2)
        self.assertTrue(records[0]["benchmark_temporal_evolution_eligible"])
        self.assertEqual(records[0]["end_sec"], 120.0)
        self.assertEqual(records[0]["micro_clip_ids"], ["P30_01_s000", "P30_01_s001", "P30_01_s002", "P30_01_s003"])
        self.assertEqual(records[1]["start_sec"], 120.0)
        self.assertEqual(records[1]["end_sec"], 135.0)
        self.assertEqual(records[1]["micro_clip_ids"], ["P30_01_s004"])
        self.assertEqual(records[0]["micro_evidence"][0]["facts"], ["0"])
        self.assertEqual(
            records[0]["upstream_model_self_assessment"],
            "removed_unverified_fields",
        )
        self.assertNotIn("confidence", records[0]["micro_evidence"][0])
        self.assertNotIn("trackability", records[0]["micro_evidence"][0]["nested"])
        self.assertEqual(records[0]["micro_evidence"][0]["nested"]["evidence"], "画面可见")

    def test_build_session_records_groups_clean_window_outputs_by_source_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            window_dir = root / "windows"
            window_records = [
                {
                    "record_id": "P30_01_w000",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "window_index": 0,
                    "start_sec": 0.0,
                    "end_sec": 120.0,
                    "micro_clip_ids": ["P30_01_s000"],
                    "benchmark_session_order": 2,
                    "benchmark_order_status": "assigned",
                    "benchmark_order_basis": "deterministic_video_uid_order",
                    "benchmark_temporal_evolution_eligible": True,
                },
                {
                    "record_id": "P30_01_w001",
                    "participant_id": "P30",
                    "source_video_id": "P30_01",
                    "window_index": 1,
                    "start_sec": 120.0,
                    "end_sec": 180.0,
                    "micro_clip_ids": ["P30_01_s004"],
                },
            ]
            for record in window_records:
                write_json(
                    window_dir / f"{record['record_id']}.clean.json",
                    {
                        "window_id": record["record_id"],
                        "summary": record["record_id"],
                        "confidence": "high",
                    },
                )

            sessions = build_session_records(window_records, window_dir, allow_missing=False)

        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["record_id"], "P30_01")
        self.assertEqual(session["benchmark_session_order"], 2)
        self.assertTrue(session["benchmark_temporal_evolution_eligible"])
        self.assertEqual(session["start_sec"], 0.0)
        self.assertEqual(session["end_sec"], 180.0)
        self.assertEqual(session["window_ids"], ["P30_01_w000", "P30_01_w001"])
        self.assertEqual([item["summary"] for item in session["window_evidence"]], ["P30_01_w000", "P30_01_w001"])
        self.assertEqual(
            session["upstream_model_self_assessment"],
            "removed_unverified_fields",
        )
        self.assertTrue(all("confidence" not in item for item in session["window_evidence"]))


if __name__ == "__main__":
    unittest.main()
