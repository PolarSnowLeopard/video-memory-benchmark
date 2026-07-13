import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.llm_json_cleaning import clean_chat_completion_response  # noqa: E402


def write_response(path: Path, content: str, finish_reason: str = "stop") -> None:
    path.write_text(
        json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": content, "reasoning": None},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class LlmJsonCleaningTests(unittest.TestCase):
    def test_uses_strict_json_without_repair_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "record.json"
            clean = root / "record.clean.json"
            write_response(raw, '```json\n{"value":"ok"}\n```')

            method = clean_chat_completion_response(raw, clean)

            self.assertEqual(method, "strict_json")
            self.assertEqual(json.loads(clean.read_text()), {"value": "ok"})
            self.assertFalse((root / "record.repair.json").exists())

    def test_repairs_malformed_json_and_writes_audit_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "record.json"
            clean = root / "record.clean.json"
            write_response(raw, '{"post_state": 面饼表面覆盖黄油"}')

            method = clean_chat_completion_response(raw, clean)

            self.assertEqual(method, "json_repair")
            self.assertEqual(json.loads(clean.read_text()), {"post_state": "面饼表面覆盖黄油"})
            audit = json.loads((root / "record.repair.json").read_text())
            self.assertEqual(audit["record_id"], "record")
            self.assertEqual(audit["repair_method"], "json_repair")
            self.assertIn("JSONDecodeError", audit["strict_error"])
            self.assertEqual(len(audit["source_raw_sha256"]), 64)

    def test_does_not_repair_length_truncated_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "record.json"
            clean = root / "record.clean.json"
            write_response(raw, '{"events":[{"id":1}', finish_reason="length")

            with self.assertRaisesRegex(ValueError, "finish_reason=length"):
                clean_chat_completion_response(raw, clean)

            self.assertFalse(clean.exists())
            self.assertFalse((root / "record.repair.json").exists())


if __name__ == "__main__":
    unittest.main()
