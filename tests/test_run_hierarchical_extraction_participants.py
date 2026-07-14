import json
import threading
import sys
import tempfile
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_hierarchical_extraction_participants import (  # noqa: E402
    build_session_prepare_command,
    discover_participant_manifests,
    http_server_serves_directory,
    require_validation_complete,
    run_until_clean,
)


class QuietHttpHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        pass


class HierarchicalExtractionParticipantTests(unittest.TestCase):
    def test_http_server_root_check_rejects_another_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as served_tmp, tempfile.TemporaryDirectory() as other_tmp:
            handler = partial(QuietHttpHandler, directory=served_tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                self.assertTrue(http_server_serves_directory(base_url, Path(served_tmp)))
                self.assertFalse(http_server_serves_directory(base_url, Path(other_tmp)))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_session_preparation_uses_frame_accurate_reencode(self) -> None:
        command = build_session_prepare_command(
            "python3",
            Path("manifest.csv"),
            Path("data"),
            "http://127.0.0.1:18080",
        )

        self.assertEqual(command[command.index("--cut-mode") + 1], "reencode")
        self.assertEqual(command[command.index("--reencode-crf") + 1], "23")
        self.assertIn("--fail-fast", command)

    def test_discovers_and_orders_participant_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p30_all_videos_proxy_540p16_urls.csv").write_text("video_id\nP30_01\n")
            (root / "p02_all_videos_proxy_540p16_urls.csv").write_text("video_id\nP02_01\n")

            found = discover_participant_manifests(root, "all")

        self.assertEqual([participant for participant, _ in found], ["P02", "P30"])

    def test_retries_batch_until_expected_clean_outputs_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            calls: list[list[str]] = []

            def runner(command: list[str]) -> None:
                calls.append(command)
                record_id = f"record_{len(calls)}"
                (output_dir / f"{record_id}.clean.json").write_text("{}")

            run_until_clean(
                label="micro",
                output_dir=output_dir,
                expected=2,
                attempts=3,
                command=["extract"],
                runner=runner,
            )

        self.assertEqual(len(calls), 2)

    def test_retry_targets_only_missing_records_and_raises_token_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "record_1.clean.json").write_text("{}")
            calls: list[list[str]] = []

            def runner(command: list[str]) -> None:
                calls.append(command)
                if len(calls) == 2:
                    (output_dir / "record_2.clean.json").write_text("{}")

            run_until_clean(
                label="micro",
                output_dir=output_dir,
                expected=2,
                expected_ids={"record_1", "record_2"},
                attempts=2,
                command=["extract", "--max-tokens", "4096"],
                final_max_tokens=8192,
                runner=runner,
            )

        self.assertEqual(calls[0][-2:], ["--record-ids", "record_2"])
        self.assertEqual(calls[1], ["extract", "--max-tokens", "8192", "--record-ids", "record_2"])

    def test_fails_when_validation_report_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            report.write_text(
                json.dumps({"records": 9, "accepted": 9, "rejected": 0}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "expected 10"):
                require_validation_complete(report, expected=10, label="P01 micro")


if __name__ == "__main__":
    unittest.main()
