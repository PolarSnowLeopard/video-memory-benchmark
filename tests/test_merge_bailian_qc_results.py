import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.merge_bailian_qc_results import (  # noqa: E402
    build_quality_report,
    human_review_rows,
    merge_local_verdict,
    main as merge_main,
    merge_source_verdicts,
    parse_batch_output_line,
    prune_merged_outputs,
    repair_unquoted_minute_second_values,
    reset_merge_outputs,
)


def candidate(candidate_id: str, flags: list[str] | None = None) -> dict:
    return {
        "candidate_id": candidate_id,
        "type": "object_location",
        "claim": f"本会话观察事实 {candidate_id}",
        "observed_value": "柜内",
        "supporting_window_ids": ["P30_01_w000"],
        "confidence": "high",
        "normalized_confidence": "high",
        "quality_flags": flags or [],
        "qc_status": "schema_passed",
        "usable_for_reference": False,
    }


def session_with_candidates(items: list[dict]) -> dict:
    return {
        "session_id": "P30_01",
        "source_video_id": "P30_01",
        "participant_id": "P30",
        "cross_session_evidence_candidates": items,
    }


def source_manifest(ids: list[str]) -> dict:
    return {
        "custom_id": "P30_01",
        "source_video_id": "P30_01",
        "participant_id": "P30",
        "model": "qwen3.7-plus",
        "candidate_ids": ids,
        "candidates": [
            {
                "candidate_id": item,
                "supporting_window_ids": ["P30_01_w000"],
                "support_ranges": [{"start_sec": 0.0, "end_sec": 120.0}],
                "quality_flags": [],
            }
            for item in ids
        ],
    }


def verdict(candidate_id: str, value: str, corrected_claim=None, ranges=None) -> dict:
    if ranges is None:
        ranges = [{"start_sec": 10.0, "end_sec": 20.0}]
    return {
        "candidate_id": candidate_id,
        "verdict": value,
        "corrected_claim": corrected_claim,
        "evidence_time_ranges": ranges,
        "reason_codes": ["visible_support" if value == "entailed" else "location_unclear"],
        "reason": "画面核验结果",
        "confidence": "high",
    }


class MergeBailianQcResultsTests(unittest.TestCase):
    def test_parse_batch_output_line_extracts_assistant_json(self) -> None:
        payload = {
            "source_video_id": "P30_01",
            "verification_results": [verdict("memcand_1", "entailed")],
        }
        line = {
            "custom_id": "P30_01",
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [
                        {
                            "message": {
                                "content": "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
                            }
                        }
                    ]
                },
            },
            "error": None,
        }

        custom_id, parsed = parse_batch_output_line(line)

        self.assertEqual(custom_id, "P30_01")
        self.assertEqual(parsed, payload)

    def test_repairs_unquoted_minute_second_values_in_time_fields(self) -> None:
        text = """
        {
          "start_sec": 4:55.0,
          "end_sec": 5:00,
          "reason": "4:55.0 remains text"
        }
        """

        repaired = repair_unquoted_minute_second_values(text)
        parsed = json.loads(repaired)

        self.assertEqual(parsed["start_sec"], 295.0)
        self.assertEqual(parsed["end_sec"], 300)
        self.assertEqual(parsed["reason"], "4:55.0 remains text")

    def test_parse_batch_output_line_repairs_unquoted_minute_second_values(self) -> None:
        line = {
            "custom_id": "P30_01",
            "response": {
                "status_code": 200,
                "body": {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"source_video_id":"P30_01",'
                                    '"verification_results":[{'
                                    '"candidate_id":"memcand_1",'
                                    '"evidence_time_ranges":[{'
                                    '"start_sec":4:55.0,"end_sec":5:00}]}]}'
                                )
                            }
                        }
                    ]
                },
            },
            "error": None,
        }

        custom_id, parsed = parse_batch_output_line(line)

        self.assertEqual(custom_id, "P30_01")
        time_range = parsed["verification_results"][0]["evidence_time_ranges"][0]
        self.assertEqual(time_range, {"start_sec": 295.0, "end_sec": 300})

    def test_merge_routes_contradicted_and_insufficient_to_local_review(self) -> None:
        session = session_with_candidates([candidate("memcand_1"), candidate("memcand_2")])
        manifest = source_manifest(["memcand_1", "memcand_2"])
        response = {
            "source_video_id": "P30_01",
            "verification_results": [
                verdict("memcand_1", "contradicted"),
                verdict("memcand_2", "insufficient", ranges=[]),
            ],
        }

        merged, queue = merge_source_verdicts(session, manifest, response)

        self.assertEqual(merged["candidates"][0]["qc_status"], "verification_disputed")
        self.assertEqual(merged["candidates"][1]["qc_status"], "verification_uncertain")
        self.assertEqual(
            {item["candidate_id"] for item in queue},
            {"memcand_1", "memcand_2"},
        )

    def test_entailed_candidate_with_overlapping_evidence_is_usable(self) -> None:
        session = session_with_candidates([candidate("memcand_1")])
        manifest = source_manifest(["memcand_1"])
        response = {
            "source_video_id": "P30_01",
            "verification_results": [verdict("memcand_1", "entailed")],
        }

        merged, queue = merge_source_verdicts(session, manifest, response)

        self.assertEqual(queue, [])
        result = merged["candidates"][0]
        self.assertEqual(result["qc_status"], "verification_passed")
        self.assertTrue(result["usable_for_reference"])

    def test_entailed_candidate_with_blocking_flag_requires_human_review(self) -> None:
        session = session_with_candidates(
            [candidate("memcand_1", flags=["long_term_overclaim"])]
        )
        manifest = source_manifest(["memcand_1"])
        response = {
            "source_video_id": "P30_01",
            "verification_results": [verdict("memcand_1", "entailed")],
        }

        merged, queue = merge_source_verdicts(session, manifest, response)

        self.assertEqual(queue, [])
        result = merged["candidates"][0]
        self.assertEqual(result["qc_status"], "human_review_required")
        self.assertFalse(result["usable_for_reference"])

    def test_zero_duration_evidence_cannot_auto_pass(self) -> None:
        merged, queue = merge_source_verdicts(
            session_with_candidates([candidate("memcand_1")]),
            source_manifest(["memcand_1"]),
            {
                "source_video_id": "P30_01",
                "verification_results": [
                    verdict(
                        "memcand_1",
                        "entailed",
                        ranges=[{"start_sec": 10.0, "end_sec": 10.0}],
                    )
                ],
            },
        )

        self.assertEqual(merged["candidates"][0]["qc_status"], "verification_uncertain")
        self.assertEqual([item["candidate_id"] for item in queue], ["memcand_1"])

    def test_corrected_claim_is_never_auto_applied(self) -> None:
        session = session_with_candidates([candidate("memcand_1")])
        manifest = source_manifest(["memcand_1"])
        response = {
            "source_video_id": "P30_01",
            "verification_results": [
                verdict("memcand_1", "entailed", corrected_claim="刀具被放入抽屉")
            ],
        }

        merged, _ = merge_source_verdicts(session, manifest, response)

        result = merged["candidates"][0]
        self.assertEqual(result["claim"], "本会话观察事实 memcand_1")
        self.assertEqual(result["qc_status"], "human_review_required")
        self.assertFalse(result["usable_for_reference"])

    def test_merge_rejects_missing_candidate_verdict(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            merge_source_verdicts(
                session_with_candidates([candidate("memcand_1"), candidate("memcand_2")]),
                source_manifest(["memcand_1", "memcand_2"]),
                {
                    "source_video_id": "P30_01",
                    "verification_results": [verdict("memcand_1", "entailed")],
                },
            )

    def test_merge_preserves_schema_failed_candidate_without_requesting_verdict(self) -> None:
        failed = candidate("memcand_1", flags=["unknown_entity_reference"])
        failed["qc_status"] = "schema_failed"
        session = session_with_candidates([failed, candidate("memcand_2")])
        manifest = source_manifest(["memcand_2"])
        response = {
            "source_video_id": "P30_01",
            "verification_results": [verdict("memcand_2", "entailed")],
        }

        merged, queue = merge_source_verdicts(session, manifest, response)

        self.assertEqual(queue, [])
        self.assertEqual(merged["candidates"][0]["qc_status"], "schema_failed")
        self.assertFalse(merged["candidates"][0]["usable_for_reference"])
        self.assertEqual(merged["candidates"][1]["qc_status"], "verification_passed")

    def test_quality_report_counts_statuses_and_candidate_types(self) -> None:
        merged, _ = merge_source_verdicts(
            session_with_candidates([candidate("memcand_1")]),
            source_manifest(["memcand_1"]),
            {
                "source_video_id": "P30_01",
                "verification_results": [verdict("memcand_1", "entailed")],
            },
        )

        report = build_quality_report([merged])

        self.assertEqual(report["sources"], 1)
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(report["qc_status_counts"], {"verification_passed": 1})
        self.assertEqual(report["candidate_type_counts"], {"object_location": 1})

    def test_prune_merged_outputs_removes_stale_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            merged_dir = Path(tmp)
            current = merged_dir / "P30_01.qc.json"
            stale = merged_dir / "P30_02.qc.json"
            current.write_text("{}", encoding="utf-8")
            stale.write_text("{}", encoding="utf-8")

            prune_merged_outputs(merged_dir, {"P30_01"})

            self.assertTrue(current.exists())
            self.assertFalse(stale.exists())

    def test_reset_merge_outputs_removes_all_published_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            merged_dir = output_dir / "merged"
            merged_dir.mkdir()
            (merged_dir / "P30_01.qc.json").write_text("{}", encoding="utf-8")
            for name in (
                "local_review_queue.jsonl",
                "retry_queue.jsonl",
                "quality_report.json",
                "human_review.csv",
            ):
                (output_dir / name).write_text("stale", encoding="utf-8")

            reset_merge_outputs(output_dir)

            self.assertEqual(list(merged_dir.glob("*.qc.json")), [])
            self.assertFalse((output_dir / "local_review_queue.jsonl").exists())
            self.assertFalse((output_dir / "retry_queue.jsonl").exists())
            self.assertFalse((output_dir / "quality_report.json").exists())
            self.assertFalse((output_dir / "human_review.csv").exists())

    def test_missing_batch_output_publishes_no_partial_merged_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "sessions"
            session_dir.mkdir()
            (session_dir / "P30_01.clean.json").write_text(
                json.dumps(
                    session_with_candidates([candidate("memcand_1")]),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest_path = root / "manifest.jsonl"
            manifest_path.write_text(
                json.dumps(source_manifest(["memcand_1"]), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            results_path = root / "results.jsonl"
            results_path.write_text("", encoding="utf-8")
            output_dir = root / "qc"
            stale_dir = output_dir / "merged"
            stale_dir.mkdir(parents=True)
            (stale_dir / "P30_OLD.qc.json").write_text("{}", encoding="utf-8")

            argv = [
                "merge_bailian_qc_results.py",
                "source",
                "--session-records",
                str(session_dir),
                "--manifest-jsonl",
                str(manifest_path),
                "--batch-output-jsonl",
                str(results_path),
                "--output-dir",
                str(output_dir),
            ]
            with patch.object(sys, "argv", argv), self.assertRaisesRegex(
                RuntimeError, "missing Batch outputs"
            ):
                merge_main()

            self.assertEqual(list(stale_dir.glob("*.qc.json")), [])
            retry_rows = [
                json.loads(line)
                for line in (output_dir / "retry_queue.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line
            ]
            self.assertEqual(retry_rows[0]["source_video_id"], "P30_01")

    def test_local_entailed_verdict_passes_disputed_candidate(self) -> None:
        first_pass, _ = merge_source_verdicts(
            session_with_candidates([candidate("memcand_1")]),
            source_manifest(["memcand_1"]),
            {
                "source_video_id": "P30_01",
                "verification_results": [verdict("memcand_1", "contradicted")],
            },
        )
        local_manifest = {
            "record_id": "P30_01:memcand_1",
            "source_video_id": "P30_01",
            "candidate_id": "memcand_1",
            "model": "qwen3.7-plus",
            "support_ranges": [{"start_sec": 0.0, "end_sec": 120.0}],
            "clip_ids": ["P30_01_qc_s00000"],
        }
        response = {
            "source_video_id": "P30_01",
            "verification_results": [verdict("memcand_1", "entailed")],
        }

        merged = merge_local_verdict(first_pass, local_manifest, response)

        result = merged["candidates"][0]
        self.assertEqual(result["qc_status"], "local_verification_passed")
        self.assertTrue(result["usable_for_reference"])

    def test_local_contradicted_verdict_rejects_candidate(self) -> None:
        first_pass, _ = merge_source_verdicts(
            session_with_candidates([candidate("memcand_1")]),
            source_manifest(["memcand_1"]),
            {
                "source_video_id": "P30_01",
                "verification_results": [verdict("memcand_1", "insufficient", ranges=[])],
            },
        )
        merged = merge_local_verdict(
            first_pass,
            {
                "record_id": "P30_01:memcand_1",
                "source_video_id": "P30_01",
                "candidate_id": "memcand_1",
                "model": "qwen3.7-plus",
                "support_ranges": [{"start_sec": 0.0, "end_sec": 120.0}],
                "clip_ids": ["P30_01_qc_s00000"],
            },
            {
                "source_video_id": "P30_01",
                "verification_results": [verdict("memcand_1", "contradicted")],
            },
        )

        result = merged["candidates"][0]
        self.assertEqual(result["qc_status"], "local_verification_rejected")
        self.assertFalse(result["usable_for_reference"])

    def test_local_contradicted_without_overlapping_evidence_requires_human_review(self) -> None:
        first_pass, _ = merge_source_verdicts(
            session_with_candidates([candidate("memcand_1")]),
            source_manifest(["memcand_1"]),
            {
                "source_video_id": "P30_01",
                "verification_results": [verdict("memcand_1", "insufficient", ranges=[])],
            },
        )
        merged = merge_local_verdict(
            first_pass,
            {
                "record_id": "P30_01:memcand_1",
                "source_video_id": "P30_01",
                "candidate_id": "memcand_1",
                "model": "qwen3.7-plus",
                "support_ranges": [{"start_sec": 0.0, "end_sec": 120.0}],
                "clip_ids": ["P30_01_qc_s00000"],
            },
            {
                "source_video_id": "P30_01",
                "verification_results": [
                    verdict("memcand_1", "contradicted", ranges=[])
                ],
            },
        )

        result = merged["candidates"][0]
        self.assertEqual(result["qc_status"], "human_review_required")
        self.assertFalse(result["usable_for_reference"])

    def test_local_corrected_claim_requires_human_review(self) -> None:
        first_pass, _ = merge_source_verdicts(
            session_with_candidates([candidate("memcand_1")]),
            source_manifest(["memcand_1"]),
            {
                "source_video_id": "P30_01",
                "verification_results": [verdict("memcand_1", "insufficient", ranges=[])],
            },
        )
        merged = merge_local_verdict(
            first_pass,
            {
                "record_id": "P30_01:memcand_1",
                "source_video_id": "P30_01",
                "candidate_id": "memcand_1",
                "model": "qwen3.7-plus",
                "support_ranges": [{"start_sec": 0.0, "end_sec": 120.0}],
                "clip_ids": ["P30_01_qc_s00000"],
            },
            {
                "source_video_id": "P30_01",
                "verification_results": [
                    verdict("memcand_1", "entailed", corrected_claim="刀具位于抽屉")
                ],
            },
        )

        result = merged["candidates"][0]
        self.assertEqual(result["qc_status"], "human_review_required")
        self.assertFalse(result["usable_for_reference"])
        rows = human_review_rows([merged])
        self.assertEqual(rows[0]["corrected_claim"], "刀具位于抽屉")
        self.assertEqual(rows[0]["reason"], "画面核验结果")
        self.assertEqual(len(rows[0]["review_fingerprint"]), 64)


if __name__ == "__main__":
    unittest.main()
