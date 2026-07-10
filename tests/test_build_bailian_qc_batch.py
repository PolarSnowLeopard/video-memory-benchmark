import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.build_bailian_qc_batch import (  # noqa: E402
    build_local_requests,
    build_source_requests,
    source_video_id_from_proxy_row,
)


def session_record(source_id: str = "P30_01") -> dict:
    return {
        "session_id": source_id,
        "source_video_id": source_id,
        "participant_id": source_id.split("_", 1)[0],
        "cross_session_evidence_candidates": [
            {
                "candidate_id": "memcand_1",
                "type": "object_location",
                "claim": "本会话中观察到刀具位于柜内",
                "observed_value": "柜内",
                "supporting_window_ids": [f"{source_id}_w000"],
                "confidence": "high",
                "normalized_confidence": "high",
                "quality_flags": [],
                "qc_status": "schema_passed",
                "usable_for_reference": False,
            },
            {
                "candidate_id": "memcand_2",
                "type": "object_state",
                "claim": "本会话结束时刀具处于收纳状态",
                "observed_value": "已收纳",
                "supporting_window_ids": [f"{source_id}_w001"],
                "confidence": "medium",
                "normalized_confidence": "medium",
                "quality_flags": ["affected_by_uncertainty"],
                "qc_status": "schema_passed",
                "usable_for_reference": False,
            },
        ],
    }


def session_input(source_id: str = "P30_01") -> dict:
    return {
        "record_id": source_id,
        "session_id": source_id,
        "source_video_id": source_id,
        "window_ids": [f"{source_id}_w000", f"{source_id}_w001"],
        "window_ranges": [
            {
                "window_id": f"{source_id}_w000",
                "start_sec": 0.0,
                "end_sec": 120.0,
                "micro_clip_ids": [f"{source_id}_s000"],
            },
            {
                "window_id": f"{source_id}_w001",
                "start_sec": 120.0,
                "end_sec": 237.7,
                "micro_clip_ids": [f"{source_id}_s004"],
            },
        ],
    }


class BailianQcBatchBuilderTests(unittest.TestCase):
    def test_source_builder_groups_candidates_and_resolves_window_ranges(self) -> None:
        requests, manifest = build_source_requests(
            [session_record()],
            [session_input()],
            [
                {
                    "video_id": "P30_01",
                    "signed_url": "https://example.test/P30_01_540p16.mp4?signature=secret",
                }
            ],
            prompt="核验候选。",
            model="qwen3.7-plus",
            fps=0.5,
            max_tokens=4096,
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["custom_id"], "P30_01")
        body = requests[0]["body"]
        self.assertEqual(body["model"], "qwen3.7-plus")
        self.assertFalse(body["enable_thinking"])
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "video_url")
        self.assertEqual(content[0]["video_url"]["fps"], 0.5)
        input_payload = json.loads(content[1]["text"].split("输入 JSON：\n", 1)[1])
        self.assertEqual(len(input_payload["candidates"]), 2)

        self.assertEqual(manifest[0]["source_video_id"], "P30_01")
        self.assertEqual(
            manifest[0]["candidates"][0]["support_ranges"],
            [{"start_sec": 0.0, "end_sec": 120.0}],
        )
        self.assertEqual(
            manifest[0]["candidates"][1]["support_ranges"],
            [{"start_sec": 120.0, "end_sec": 237.7}],
        )

    def test_source_builder_rejects_unknown_supporting_window(self) -> None:
        session = session_record()
        session["cross_session_evidence_candidates"][0]["supporting_window_ids"] = [
            "P30_01_w999"
        ]

        with self.assertRaisesRegex(ValueError, "unknown window"):
            build_source_requests(
                [session],
                [session_input()],
                [{"video_id": "P30_01", "signed_url": "https://example.test/video.mp4"}],
                prompt="核验候选。",
                model="qwen3.7-plus",
                fps=0.5,
                max_tokens=4096,
            )

    def test_source_builder_rejects_missing_proxy_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "signed URL"):
            build_source_requests(
                [session_record()],
                [session_input()],
                [{"video_id": "P30_01", "signed_url": ""}],
                prompt="核验候选。",
                model="qwen3.7-plus",
                fps=0.5,
                max_tokens=4096,
            )

    def test_source_builder_skips_schema_failed_candidate(self) -> None:
        session = session_record()
        session["cross_session_evidence_candidates"][0]["qc_status"] = "schema_failed"

        requests, manifest = build_source_requests(
            [session],
            [session_input()],
            [{"video_id": "P30_01", "signed_url": "https://example.test/video.mp4"}],
            prompt="核验候选。",
            model="qwen3.7-plus",
            fps=0.5,
            max_tokens=4096,
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(manifest[0]["candidate_ids"], ["memcand_2"])

    def test_source_builder_preserves_manifest_when_all_candidates_fail_schema(self) -> None:
        session = session_record()
        for item in session["cross_session_evidence_candidates"]:
            item["qc_status"] = "schema_failed"

        requests, manifest = build_source_requests(
            [session],
            [session_input()],
            [{"video_id": "P30_01", "signed_url": "https://example.test/video.mp4"}],
            prompt="核验候选。",
            model="qwen3.7-plus",
            fps=0.5,
            max_tokens=4096,
        )

        self.assertEqual(requests, [])
        self.assertEqual(len(manifest), 1)
        self.assertTrue(manifest[0]["request_skipped"])
        self.assertEqual(
            manifest[0]["excluded_candidate_ids"], ["memcand_1", "memcand_2"]
        )

    def test_proxy_video_id_can_be_derived_from_key(self) -> None:
        self.assertEqual(
            source_video_id_from_proxy_row(
                {"key": "video-benchmark/EPIC/P30/videos/P30_01_540p16.mp4"}
            ),
            "P30_01",
        )

    def test_local_builder_creates_one_request_per_candidate(self) -> None:
        review_items = [
            {
                "record_id": "P30_01:memcand_1",
                "source_video_id": "P30_01",
                "participant_id": "P30",
                "candidate_id": "memcand_1",
                "claim": "刀具被放入柜内",
                "support_ranges": [{"start_sec": 30.0, "end_sec": 90.0}],
                "quality_flags": [],
                "clip_ids": ["P30_01_qc_s00001", "P30_01_qc_s00002"],
            }
        ]
        clip_rows = [
            {
                "clip_id": "P30_01_qc_s00002",
                "source_video_id": "P30_01",
                "start_sec": "60",
                "end_sec": "90",
                "signed_url": "https://example.test/c2.mp4",
            },
            {
                "clip_id": "P30_01_qc_s00001",
                "source_video_id": "P30_01",
                "start_sec": "30",
                "end_sec": "60",
                "signed_url": "https://example.test/c1.mp4",
            },
        ]

        requests, manifest = build_local_requests(
            review_items,
            clip_rows,
            prompt="局部核验。",
            model="qwen3.7-plus",
            fps=1.0,
            max_tokens=4096,
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["custom_id"], "P30_01:memcand_1")
        content = requests[0]["body"]["messages"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["video_url", "video_url", "text"])
        self.assertEqual(content[0]["video_url"]["url"], "https://example.test/c1.mp4")
        self.assertEqual(manifest[0]["clip_ids"], ["P30_01_qc_s00001", "P30_01_qc_s00002"])

    def test_local_builder_rejects_clip_without_signed_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "signed URL"):
            build_local_requests(
                [
                    {
                        "record_id": "P30_01:memcand_1",
                        "source_video_id": "P30_01",
                        "candidate_id": "memcand_1",
                        "claim": "刀具被收纳",
                        "support_ranges": [{"start_sec": 0.0, "end_sec": 30.0}],
                        "clip_ids": ["P30_01_qc_s00000"],
                    }
                ],
                [
                    {
                        "clip_id": "P30_01_qc_s00000",
                        "source_video_id": "P30_01",
                        "start_sec": "0",
                        "end_sec": "30",
                        "signed_url": "",
                    }
                ],
                prompt="局部核验。",
                model="qwen3.7-plus",
                fps=1.0,
                max_tokens=4096,
            )


if __name__ == "__main__":
    unittest.main()
