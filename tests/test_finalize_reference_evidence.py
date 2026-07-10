import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.finalize_reference_evidence import (  # noqa: E402
    candidate_review_fingerprint,
    finalize_candidates,
)


def qc_candidate(candidate_id: str, status: str, usable: bool) -> dict:
    return {
        "candidate_id": candidate_id,
        "type": "object_location",
        "claim": f"原始事实 {candidate_id}",
        "observed_value": "柜内",
        "quality_flags": [],
        "qc_status": status,
        "usable_for_reference": usable,
        "support_ranges": [{"start_sec": 0.0, "end_sec": 30.0}],
    }


class FinalizeReferenceEvidenceTests(unittest.TestCase):
    def test_finalize_splits_automatic_and_human_decisions(self) -> None:
        records = [
            {
                "source_video_id": "P30_01",
                "session_id": "P30_01",
                "participant_id": "P30",
                "pipeline_version": "v0.2",
                "candidates": [
                    qc_candidate("memcand_1", "verification_passed", True),
                    qc_candidate("memcand_2", "local_verification_rejected", False),
                    qc_candidate("memcand_3", "human_review_required", False),
                    qc_candidate("memcand_4", "human_review_required", False),
                ],
            }
        ]
        human_rows = [
            {
                "source_video_id": "P30_01",
                "candidate_id": "memcand_3",
                "claim": "原始事实 memcand_3",
                "review_fingerprint": candidate_review_fingerprint(
                    records[0], records[0]["candidates"][2]
                ),
                "human_decision": "accept_corrected",
                "approved_claim": "人工确认的保守事实",
                "human_notes": "已查看局部片段",
            },
            {
                "source_video_id": "P30_01",
                "candidate_id": "memcand_4",
                "claim": "原始事实 memcand_4",
                "review_fingerprint": candidate_review_fingerprint(
                    records[0], records[0]["candidates"][3]
                ),
                "human_decision": "",
                "approved_claim": "",
                "human_notes": "",
            },
        ]

        ready, rejected, unresolved = finalize_candidates(records, human_rows)

        self.assertEqual(
            [item["candidate_id"] for item in ready],
            ["memcand_1", "memcand_3"],
        )
        self.assertEqual(ready[1]["claim"], "人工确认的保守事实")
        self.assertEqual(ready[1]["qc_status"], "human_accepted")
        self.assertEqual([item["candidate_id"] for item in rejected], ["memcand_2"])
        self.assertEqual([item["candidate_id"] for item in unresolved], ["memcand_4"])

    def test_accept_corrected_requires_approved_claim(self) -> None:
        records = [
            {
                "source_video_id": "P30_01",
                "participant_id": "P30",
                "candidates": [
                    qc_candidate("memcand_1", "human_review_required", False)
                ],
            }
        ]
        with self.assertRaisesRegex(ValueError, "approved_claim"):
            finalize_candidates(
                records,
                [
                    {
                        "source_video_id": "P30_01",
                        "candidate_id": "memcand_1",
                        "claim": "原始事实 memcand_1",
                        "review_fingerprint": candidate_review_fingerprint(
                            records[0], records[0]["candidates"][0]
                        ),
                        "human_decision": "accept_corrected",
                        "approved_claim": "",
                    }
                ],
            )

    def test_stale_human_review_fingerprint_is_rejected(self) -> None:
        records = [
            {
                "source_video_id": "P30_01",
                "participant_id": "P30",
                "candidates": [
                    qc_candidate("memcand_1", "human_review_required", False)
                ],
            }
        ]
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            finalize_candidates(
                records,
                [
                    {
                        "source_video_id": "P30_01",
                        "candidate_id": "memcand_1",
                        "claim": "旧事实",
                        "review_fingerprint": "stale",
                        "human_decision": "accept_original",
                    }
                ],
            )

    def test_human_decision_cannot_override_rejected_candidate(self) -> None:
        records = [
            {
                "source_video_id": "P30_01",
                "participant_id": "P30",
                "candidates": [
                    qc_candidate("memcand_1", "local_verification_rejected", False)
                ],
            }
        ]
        candidate_value = records[0]["candidates"][0]
        with self.assertRaisesRegex(ValueError, "human_review_required"):
            finalize_candidates(
                records,
                [
                    {
                        "source_video_id": "P30_01",
                        "candidate_id": "memcand_1",
                        "claim": candidate_value["claim"],
                        "review_fingerprint": candidate_review_fingerprint(
                            records[0], candidate_value
                        ),
                        "human_decision": "accept_original",
                    }
                ],
            )

    def test_human_decision_for_unknown_candidate_is_rejected(self) -> None:
        records = [
            {
                "source_video_id": "P30_01",
                "participant_id": "P30",
                "candidates": [qc_candidate("memcand_1", "verification_passed", True)],
            }
        ]
        with self.assertRaisesRegex(ValueError, "no matching QC candidate"):
            finalize_candidates(
                records,
                [
                    {
                        "source_video_id": "P30_99",
                        "candidate_id": "memcand_9",
                        "human_decision": "reject",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
