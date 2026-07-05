import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.qwen_text_jsonl_batch import extract_json_text, make_prompt_text, record_id_from_record  # noqa: E402


class QwenTextJsonlBatchTests(unittest.TestCase):
    def test_record_id_prefers_record_id_then_session_id(self) -> None:
        self.assertEqual(record_id_from_record({"record_id": "P30_01_w000"}), "P30_01_w000")
        self.assertEqual(record_id_from_record({"session_id": "P30_01"}), "P30_01")

    def test_make_prompt_text_includes_prompt_and_compact_json_payload(self) -> None:
        text = make_prompt_text("请聚合。", {"record_id": "P30_01_w000", "value": "水槽"})
        self.assertIn("请聚合。", text)
        self.assertIn("输入 JSON：", text)
        self.assertIn('"record_id":"P30_01_w000"', text)
        self.assertIn('"value":"水槽"', text)

    def test_extract_json_text_accepts_fenced_json(self) -> None:
        text = "```json\n{\"ok\": true, \"value\": \"水槽\"}\n```"
        payload = json.loads(extract_json_text(text))
        self.assertEqual(payload, {"ok": True, "value": "水槽"})


if __name__ == "__main__":
    unittest.main()
