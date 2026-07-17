import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.bailian_online_jsonl import (  # noqa: E402
    batch_success_line,
    existing_success_ids,
    online_endpoint,
    read_jsonl,
)
from scripts.merge_bailian_qc_results import parse_batch_output_line  # noqa: E402


class BailianOnlineJsonlTests(unittest.TestCase):
    def test_online_result_is_batch_merge_compatible(self) -> None:
        verification = {
            "source_video_id": "P01_01",
            "verification_results": [],
        }
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(verification, ensure_ascii=False),
                    },
                }
            ]
        }

        custom_id, parsed = parse_batch_output_line(
            batch_success_line("P01_01", body, "request-1")
        )

        self.assertEqual(custom_id, "P01_01")
        self.assertEqual(parsed, verification)

    def test_reads_unique_post_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "custom_id": "P01_01",
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": {"model": "qwen3.7-plus"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            records = read_jsonl(path)

        self.assertEqual([row["custom_id"] for row in records], ["P01_01"])

    def test_existing_success_ids_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            line = json.dumps(
                batch_success_line(
                    "P01_01",
                    {"choices": [{"message": {"content": "{}"}}]},
                )
            )
            path.write_text(line + "\n" + line + "\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Duplicate"):
                existing_success_ids(path)

    def test_online_endpoint_appends_chat_completions(self) -> None:
        self.assertEqual(
            online_endpoint("https://example.test/compatible-mode/v1/"),
            "https://example.test/compatible-mode/v1/chat/completions",
        )


if __name__ == "__main__":
    unittest.main()
