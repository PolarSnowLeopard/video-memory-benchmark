import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.bailian_batch_job import (  # noqa: E402
    ensure_can_submit,
    require_api_key,
    sha256_file,
    write_remote_file,
    write_job_record,
)


class BailianBatchJobTests(unittest.TestCase):
    def test_require_api_key_reads_only_the_named_environment_variable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
                require_api_key("DASHSCOPE_API_KEY")
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "secret-value"}, clear=True):
            self.assertEqual(require_api_key("DASHSCOPE_API_KEY"), "secret-value")

    def test_sha256_file_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.jsonl"
            path.write_bytes(b"one\ntwo\n")
            self.assertEqual(
                sha256_file(path),
                "c3f9c8c283a2b1f2f1896f27a01cbe3cddc0c9d93f752e4639035a0f5b36f6e8",
            )

    def test_active_job_record_blocks_duplicate_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(
                json.dumps({"batch_id": "batch-1", "status": "in_progress"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "force-new"):
                ensure_can_submit(path, force_new=False)
            ensure_can_submit(path, force_new=True)

    def test_job_record_never_serializes_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            write_job_record(
                path,
                {
                    "batch_id": "batch-1",
                    "input_file_id": "file-1",
                    "status": "validating",
                    "api_key": "must-not-be-written",
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", payload)
            self.assertEqual(payload["batch_id"], "batch-1")

    def test_completed_job_record_also_requires_force_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(
                json.dumps({"batch_id": "batch-1", "status": "completed"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "force-new"):
                ensure_can_submit(path, force_new=False)

    def test_interrupted_download_leaves_no_final_or_partial_file(self) -> None:
        class FailingResponse:
            def write_to_file(self, path):
                Path(path).write_bytes(b"partial")
                raise OSError("connection lost")

        class Files:
            def content(self, file_id):
                self.file_id = file_id
                return FailingResponse()

        class Client:
            files = Files()

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "results.jsonl"
            with self.assertRaisesRegex(OSError, "connection lost"):
                write_remote_file(Client(), "file-1", output, overwrite=False)
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".jsonl.part").exists())


if __name__ == "__main__":
    unittest.main()
