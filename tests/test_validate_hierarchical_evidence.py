import sys
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_hierarchical_evidence import (  # noqa: E402
    validate_micro_record,
    validate_session_record,
    validate_window_record,
    validate_directory,
)


def valid_micro_record() -> dict:
    return {
        "clip_id": "P30_01_s000",
        "source_video_id": "P30_01",
        "clip_time_range": {"start_sec": 0.0, "end_sec": 30.0},
        "clip_summary": "用户拿起刀具。",
        "places": [
            {
                "place_id": "place_1",
                "name": "操作台",
                "visual_anchors": ["木质台面"],
                "evidence_times": ["00:01"],
                "confidence": "high",
            }
        ],
        "objects": [
            {
                "object_id": "obj_1",
                "name": "刀具",
                "category": "tool",
                "distinctive_attributes": ["黑色手柄"],
                "first_seen": "00:01",
                "last_seen": "00:20",
                "initial_location": "操作台",
                "final_location": "手中",
                "initial_state": "静置",
                "final_state": "被拿起",
                "trackability": "high",
                "confidence": "high",
            }
        ],
        "atomic_events": [
            {
                "event_id": "event_1",
                "time_range": "00:01-00:03",
                "action_type": "pick_up",
                "action": "拿起刀具",
                "place_id": "place_1",
                "object_ids": ["obj_1"],
                "pre_state": "刀具在操作台",
                "post_state": "刀具在手中",
                "evidence": "手抓住刀柄",
                "confidence": "high",
            }
        ],
        "state_observations": [],
        "state_changes": [
            {
                "change_id": "change_1",
                "time_range": "00:01-00:03",
                "entity_id": "obj_1",
                "attribute": "location",
                "before": "操作台",
                "after": "手中",
                "trigger_event_id": "event_1",
                "evidence": "刀具被拿起",
                "confidence": "high",
            }
        ],
        "end_state": [],
        "uncertainties": [],
    }


def valid_window_record() -> dict:
    return {
        "window_id": "P30_01_w000",
        "source_video_id": "P30_01",
        "time_range": {"start_sec": 0.0, "end_sec": 120.0},
        "window_summary": "用户在操作台处理刀具。",
        "entity_map": [
            {
                "entity_id": "ent_1",
                "name": "刀具",
                "entity_type": "tool",
                "source_ids": ["P30_01_s000:obj_1"],
                "distinctive_attributes": ["黑色手柄"],
                "supporting_clip_ids": ["P30_01_s000"],
                "confidence": "high",
            }
        ],
        "local_event_chain": [
            {
                "event_id": "wevent_1",
                "time_range": "1-3",
                "action_type": "pick_up",
                "action": "拿起刀具",
                "entity_ids": ["ent_1"],
                "pre_state": "操作台",
                "post_state": "手中",
                "supporting_clip_ids": ["P30_01_s000"],
                "confidence": "high",
            }
        ],
        "state_changes": [],
        "window_end_state": [],
        "open_threads": [],
        "evidence_facts": [
            {
                "fact_id": "fact_1",
                "claim": "刀具被拿起",
                "entity_ids": ["ent_1"],
                "fact_type": "state_change",
                "supporting_clip_ids": ["P30_01_s000"],
                "why_useful_for_memory": "后续可以确认刀具位置",
                "confidence": "high",
            }
        ],
        "conflicts_or_uncertainties": [],
    }


def valid_session_record() -> dict:
    return {
        "session_id": "P30_01",
        "source_video_id": "P30_01",
        "participant_id": "P30",
        "time_range": {"start_sec": 0.0, "end_sec": 120.0},
        "session_summary": "用户在厨房处理刀具。",
        "session_timeline": [
            {
                "segment_id": "seg_1",
                "time_range": "0-120",
                "summary": "处理刀具",
                "supporting_window_ids": ["P30_01_w000"],
            }
        ],
        "session_entities": [
            {
                "entity_id": "sent_1",
                "name": "刀具",
                "entity_type": "tool",
                "distinctive_attributes": ["黑色手柄"],
                "supporting_window_ids": ["P30_01_w000"],
                "confidence": "high",
            }
        ],
        "state_update_timeline": [],
        "session_final_state": [],
        "open_tasks_or_unresolved_states": [],
        "cross_session_evidence_candidates": [
            {
                "candidate_id": "memcand_1",
                "type": "object_location",
                "claim": "本会话中观察到刀具位于柜内",
                "entity_ids": ["sent_1"],
                "observed_value": "柜内",
                "supporting_window_ids": ["P30_01_w000"],
                "why_useful": "可用于后续确认刀具位置",
                "validation_needed": "后续会话重新观察刀具位置",
                "confidence": "high",
            }
        ],
        "contradictions_or_uncertainties": [],
    }


def valid_session_parent() -> dict:
    return {
        "record_id": "P30_01",
        "session_id": "P30_01",
        "source_video_id": "P30_01",
        "start_sec": 0.0,
        "end_sec": 120.0,
        "window_ids": ["P30_01_w000"],
        "window_ranges": [
            {"window_id": "P30_01_w000", "start_sec": 0.0, "end_sec": 120.0}
        ],
    }


class HierarchicalEvidenceValidatorTests(unittest.TestCase):
    def test_micro_validator_reports_unknown_object_reference(self) -> None:
        record = valid_micro_record()
        record["atomic_events"][0]["object_ids"] = ["obj_missing"]

        normalized, issues = validate_micro_record(
            record,
            {
                "session_id": "P30_01_s000",
                "source_video_id": "P30_01",
                "start_sec": "0",
                "end_sec": "30",
            },
        )

        self.assertIn("unknown_object_reference", {issue["code"] for issue in issues})
        reference_issue = next(
            issue for issue in issues if issue["code"] == "unknown_object_reference"
        )
        self.assertEqual(reference_issue["severity"], "warning")
        self.assertEqual(normalized["quality_summary"]["schema_status"], "passed")

    def test_micro_validator_rejects_relative_time_past_clip_end(self) -> None:
        record = valid_micro_record()
        record["atomic_events"][0]["time_range"] = "00:29-00:44"

        normalized, issues = validate_micro_record(
            record,
            {
                "session_id": "P30_01_s000",
                "source_video_id": "P30_01",
                "start_sec": "0",
                "end_sec": "30",
                "duration_sec": "30",
            },
        )

        self.assertIn("relative_time_out_of_bounds", {item["code"] for item in issues})
        self.assertEqual(normalized["quality_summary"]["schema_status"], "failed")

    def test_micro_validator_allows_tail_timestamp_rounded_up_to_next_second(self) -> None:
        record = valid_micro_record()
        record["clip_time_range"]["end_sec"] = 27.721
        record["objects"][0]["last_seen"] = "00:28"

        normalized, issues = validate_micro_record(
            record,
            {
                "session_id": "P30_01_s000",
                "source_video_id": "P30_01",
                "start_sec": "0",
                "end_sec": "27.721",
                "duration_sec": "27.721",
            },
        )

        self.assertNotIn("relative_time_out_of_bounds", {item["code"] for item in issues})
        self.assertEqual(normalized["quality_summary"]["schema_status"], "passed")

    def test_micro_validator_warns_about_uncalibrated_confidence(self) -> None:
        record = valid_micro_record()
        record["state_observations"] = [
            {
                "obs_id": f"obs_{index}",
                "time": f"00:0{index}",
                "entity_id": "obj_1",
                "attribute": "location",
                "value": "操作台",
                "evidence": "画面可见",
                "confidence": "high",
            }
            for index in range(6)
        ]

        normalized, issues = validate_micro_record(
            record,
            {
                "session_id": "P30_01_s000",
                "source_video_id": "P30_01",
                "start_sec": "0",
                "end_sec": "30",
            },
        )

        self.assertIn("uncalibrated_confidence", {item["code"] for item in issues})
        self.assertEqual(normalized["quality_summary"]["schema_status"], "passed")

    def test_micro_validator_warns_about_irrelevant_clothing_and_category(self) -> None:
        record = valid_micro_record()
        record["objects"][0]["name"] = "木质砧板"
        record["objects"][0]["category"] = "furniture"
        record["objects"].append(
            {
                "object_id": "obj_2",
                "name": "蓝色手套",
                "category": "clothing",
                "distinctive_attributes": ["蓝色"],
                "first_seen": "00:01",
                "last_seen": "00:20",
                "initial_location": "手上",
                "final_location": "手上",
                "initial_state": "佩戴中",
                "final_state": "佩戴中",
                "trackability": "high",
                "confidence": "high",
            }
        )

        normalized, issues = validate_micro_record(
            record,
            {
                "session_id": "P30_01_s000",
                "source_video_id": "P30_01",
                "start_sec": "0",
                "end_sec": "30",
            },
        )

        codes = {item["code"] for item in issues}
        self.assertIn("unreferenced_clothing_object", codes)
        self.assertIn("inconsistent_object_category", codes)
        self.assertEqual(normalized["quality_summary"]["schema_status"], "passed")

    def test_micro_validator_normalizes_order_state_tokens_and_cookware_category(self) -> None:
        record = valid_micro_record()
        record["objects"][0].update(
            {
                "name": "不锈钢锅",
                "category": "container",
                "initial_state": "closed",
                "final_state": "open",
            }
        )
        first_event = record["atomic_events"][0]
        later_event = dict(first_event)
        later_event.update({"event_id": "event_2", "time_range": "00:10-00:12"})
        record["atomic_events"] = [later_event, first_event]
        record["state_changes"][0].update({"before": "closed", "after": "open"})

        normalized, issues = validate_micro_record(
            record,
            {
                "session_id": "P30_01_s000",
                "source_video_id": "P30_01",
                "start_sec": "0",
                "end_sec": "30",
            },
        )

        codes = {item["code"] for item in issues}
        self.assertIn("inconsistent_object_category", codes)
        self.assertIn("non_chinese_state_token", codes)
        self.assertIn("non_monotonic_time_order", codes)
        self.assertEqual(normalized["objects"][0]["category"], "tool")
        self.assertEqual(normalized["objects"][0]["initial_state"], "关闭")
        self.assertEqual(normalized["objects"][0]["final_state"], "打开")
        self.assertEqual(normalized["state_changes"][0]["before"], "关闭")
        self.assertEqual(normalized["state_changes"][0]["after"], "打开")
        self.assertEqual(
            [item["event_id"] for item in normalized["atomic_events"]],
            ["event_1", "event_2"],
        )
        self.assertEqual(
            normalized["quality_summary"]["model_confidence_policy"],
            "self_reported_unverified",
        )
        self.assertEqual(record["objects"][0]["category"], "container")
        self.assertEqual(record["objects"][0]["initial_state"], "closed")

    def test_window_validator_reports_unknown_clip_reference(self) -> None:
        record = valid_window_record()
        record["evidence_facts"][0]["supporting_clip_ids"] = ["missing"]

        normalized, issues = validate_window_record(
            record,
            {
                "record_id": "P30_01_w000",
                "source_video_id": "P30_01",
                "start_sec": 0.0,
                "end_sec": 120.0,
                "micro_clip_ids": ["P30_01_s000"],
            },
        )

        self.assertIn("unknown_clip_reference", {issue["code"] for issue in issues})
        reference_issue = next(
            issue for issue in issues if issue["code"] == "unknown_clip_reference"
        )
        self.assertEqual(reference_issue["severity"], "warning")
        self.assertEqual(normalized["quality_summary"]["schema_status"], "passed")

    def test_session_validator_flags_long_term_wording_and_uncertainty(self) -> None:
        record = valid_session_record()
        record["cross_session_evidence_candidates"][0]["claim"] = "用户通常把刀具放入柜内"
        record["contradictions_or_uncertainties"] = [
            {
                "item": "柜体类型",
                "description": "无法确认是抽屉还是冰箱",
                "affected_candidate_ids": ["memcand_1"],
                "supporting_window_ids": ["P30_01_w000"],
            }
        ]

        normalized, issues = validate_session_record(record, valid_session_parent())
        candidate = normalized["cross_session_evidence_candidates"][0]

        self.assertIn("long_term_overclaim", candidate["quality_flags"])
        self.assertIn("affected_by_uncertainty", candidate["quality_flags"])
        self.assertEqual(candidate["normalized_confidence"], "medium")
        self.assertEqual(candidate["qc_status"], "schema_passed")
        self.assertFalse(candidate["usable_for_reference"])
        self.assertIn("long_term_overclaim", {issue["code"] for issue in issues})

    def test_session_validator_reports_unknown_window_reference(self) -> None:
        record = valid_session_record()
        record["cross_session_evidence_candidates"][0]["supporting_window_ids"] = [
            "P30_01_w999"
        ]

        normalized, issues = validate_session_record(record, valid_session_parent())

        self.assertIn("unknown_window_reference", {issue["code"] for issue in issues})
        candidate = normalized["cross_session_evidence_candidates"][0]
        self.assertEqual(candidate["qc_status"], "schema_failed")
        self.assertIn("unknown_window_reference", candidate["quality_flags"])

    def test_count_limit_is_warning_not_record_rejection(self) -> None:
        record = valid_session_record()
        record["session_timeline"] = [
            {
                "segment_id": f"seg_{index}",
                "time_range": "0-120",
                "summary": "处理食材",
                "supporting_window_ids": ["P30_01_w000"],
            }
            for index in range(13)
        ]

        normalized, issues = validate_session_record(record, valid_session_parent())

        count_issues = [item for item in issues if item["code"] == "count_limit_exceeded"]
        self.assertEqual([item["severity"] for item in count_issues], ["warning"])
        self.assertEqual(normalized["quality_summary"]["schema_status"], "passed")

    def test_single_window_procedure_candidate_is_marked_for_review(self) -> None:
        record = valid_session_record()
        candidate = record["cross_session_evidence_candidates"][0]
        candidate["type"] = "procedure_candidate"

        normalized, _ = validate_session_record(record, valid_session_parent())

        flags = normalized["cross_session_evidence_candidates"][0]["quality_flags"]
        self.assertIn("single_window_support", flags)

    def test_validate_directory_removes_stale_accepted_record_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            input_path = input_dir / "P30_01.clean.json"
            input_path.write_text(
                json.dumps(valid_session_record(), ensure_ascii=False), encoding="utf-8"
            )
            validate_directory(
                "session", input_dir, [valid_session_parent()], output_dir
            )
            accepted = output_dir / "accepted" / input_path.name
            self.assertTrue(accepted.exists())

            invalid = valid_session_record()
            del invalid["session_summary"]
            input_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
            validate_directory(
                "session", input_dir, [valid_session_parent()], output_dir
            )

            self.assertFalse(accepted.exists())

    def test_validate_directory_normalizes_model_copied_identity_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            input_path = input_dir / "P30_01_w000.clean.json"
            record = valid_window_record()
            record["window_id"] = "P30_XX_w999"
            record["source_video_id"] = "P30_XX"
            input_path.write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
            parent = {
                "record_id": "P30_01_w000",
                "window_id": "P30_01_w000",
                "participant_id": "P30",
                "source_video_id": "P30_01",
                "micro_clip_ids": ["P30_01_s000"],
            }

            validate_directory("window", input_dir, [parent], output_dir)

            report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {key: report[key] for key in ("records", "accepted", "rejected")},
                {"records": 1, "accepted": 1, "rejected": 0},
            )
            accepted = json.loads(
                (output_dir / "accepted" / input_path.name).read_text(encoding="utf-8")
            )
            self.assertEqual(accepted["window_id"], "P30_01_w000")
            self.assertEqual(accepted["source_video_id"], "P30_01")
            self.assertIn(
                "identity_field_normalized",
                accepted["quality_summary"]["issue_codes"],
            )
            issues = (output_dir / "issues.csv").read_text(encoding="utf-8")
            self.assertIn("identity_field_normalized", issues)
            self.assertNotIn("missing_parent_metadata", issues)

    def test_validate_directory_reports_missing_expected_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()

            validate_directory(
                "session", input_dir, [valid_session_parent()], output_dir
            )

            report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {key: report[key] for key in ("records", "accepted", "rejected")},
                {"records": 1, "accepted": 0, "rejected": 1},
            )
            issues = (output_dir / "issues.csv").read_text(encoding="utf-8")
            self.assertIn("missing_model_output", issues)

    def test_validate_directory_removes_stale_accepted_record_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            output_dir = root / "output"
            input_dir.mkdir()
            input_path = input_dir / "P30_01.clean.json"
            input_path.write_text(
                json.dumps(valid_session_record(), ensure_ascii=False), encoding="utf-8"
            )
            validate_directory("session", input_dir, [valid_session_parent()], output_dir)
            accepted = output_dir / "accepted" / input_path.name
            self.assertTrue(accepted.exists())

            input_path.write_text("{", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                validate_directory(
                    "session", input_dir, [valid_session_parent()], output_dir
                )

            self.assertFalse(accepted.exists())


if __name__ == "__main__":
    unittest.main()
