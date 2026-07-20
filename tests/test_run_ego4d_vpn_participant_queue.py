import csv
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_ego4d_vpn_participant_queue import (  # noqa: E402
    build_batch_command,
    cos_write_preflight,
    detect_cos_fatal_error,
    detect_global_fatal_error,
    detect_source_auth_error,
    discover_manifests,
    output_url_csv,
    prune_queue_status,
    source_manifest_participant,
)


FIELDS = [
    "video_uid",
    "participant_id",
    "benchmark_session_order",
]


def write_manifest(path: Path, participant: str, video_count: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for index in range(1, video_count + 1):
            writer.writerow(
                {
                    "video_uid": f"uid-{index}",
                    "participant_id": participant,
                    "benchmark_session_order": index,
                }
            )


class Ego4DVpnParticipantQueueTests(unittest.TestCase):
    def test_discovers_participant_manifests_and_validates_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "ego4d_p000002_all_videos.csv"
            second = root / "ego4d_p000001_all_videos.csv"
            write_manifest(first, "EGO4D_P000002", 2)
            write_manifest(second, "EGO4D_P000001", 3)

            discovered = discover_manifests(root, "all")

            self.assertEqual(
                [(participant, count) for participant, _, count in discovered],
                [("EGO4D_P000001", 3), ("EGO4D_P000002", 2)],
            )
            self.assertEqual(source_manifest_participant(first), ("EGO4D_P000002", 2))

            with first.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            rows[1]["benchmark_session_order"] = "3"
            with first.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaisesRegex(ValueError, "Non-contiguous"):
                source_manifest_participant(first)

    def test_batch_command_enables_cleanup_and_stable_output_name(self) -> None:
        args = Namespace(
            python="python3",
            batch_script=Path("scripts/run_ego4d_vpn_batch.py"),
            ego4d_root=Path("/data/ego4d"),
            ego4d_video_dir=Path("/data/ego4d/v2/video_540ss"),
            data_root=Path("/data"),
            aws_profile="ego4d",
            cos_config=Path("/home/user/.cos.conf"),
            cos_prefix="video-benchmark/ego4d",
            url_expire_days=90,
            ffmpeg_threads=1,
            min_free_gb=20,
            keep_raw=False,
            keep_proxy=False,
        )

        command = build_batch_command(
            Path("manifest.csv"), "EGO4D_P000007", args
        )

        self.assertEqual(
            command[command.index("--run-name") + 1],
            "ego4d_p000007_all_videos",
        )
        self.assertIn("--delete-raw-after-upload", command)
        self.assertIn("--delete-proxy-after-upload", command)
        self.assertIn("--fail-fast", command)
        self.assertEqual(
            output_url_csv(Path("/data"), "EGO4D_P000007"),
            Path("/data/cos_urls/ego4d_p000007_all_videos_proxy_540p16_urls.csv"),
        )

    def test_detects_ego4d_source_authorization_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "participant.log"
            log.write_text(
                "File \"/venv/site-packages/ego4d/cli/cli.py\"\n"
                "botocore.exceptions.ClientError: An error occurred (403) when "
                "calling the HeadObject operation: Forbidden\n",
                encoding="utf-8",
            )
            self.assertEqual(
                detect_source_auth_error(log),
                "when calling the headobject operation: forbidden",
            )

            log.write_text(
                "boto3 download failed: ExpiredToken\n",
                encoding="utf-8",
            )
            self.assertEqual(detect_source_auth_error(log), "expiredtoken")

    def test_does_not_treat_normal_video_failure_as_global_auth_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "participant.log"
            log.write_text(
                "ERROR uid-1: Invalid proxy duration\n",
                encoding="utf-8",
            )
            self.assertIsNone(detect_source_auth_error(log))

            log.write_text(
                "COS upload returned Forbidden without boto or Ego4D context\n",
                encoding="utf-8",
            )
            self.assertIsNone(detect_source_auth_error(log))

    def test_ignores_authorization_errors_from_an_earlier_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "participant.log"
            old_run = (
                "botocore.exceptions.ClientError: An error occurred (403) when "
                "calling the HeadObject operation: Forbidden\n"
            )
            current_run = "ERROR uid-2: Invalid proxy duration\n"
            log.write_text(old_run + current_run, encoding="utf-8")

            self.assertIsNone(
                detect_source_auth_error(
                    log,
                    start_offset=len(old_run.encode("utf-8")),
                )
            )

    def test_detects_cos_account_level_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "participant.log"
            log.write_text(
                "CosServiceError: <Code>UnavailableForLegalReasons</Code>"
                "<Message>Due to your account is arrears, it is unavailable "
                "until you recharge.</Message>\n",
                encoding="utf-8",
            )

            self.assertEqual(
                detect_cos_fatal_error(log),
                "<code>unavailableforlegalreasons</code>",
            )
            self.assertEqual(
                detect_global_fatal_error(log),
                (
                    "cos_account_error",
                    "<code>unavailableforlegalreasons</code>",
                ),
            )

    def test_does_not_treat_transient_cos_error_as_global_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "participant.log"
            log.write_text(
                "CosServiceError: RequestTimeout\n",
                encoding="utf-8",
            )
            self.assertIsNone(detect_cos_fatal_error(log))
            self.assertIsNone(detect_global_fatal_error(log))

    def test_cos_write_preflight_creates_checks_and_deletes_probe(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def put_object(self, **kwargs) -> None:
                self.calls.append(("put", kwargs))

            def head_object(self, **kwargs) -> None:
                self.calls.append(("head", kwargs))

            def delete_object(self, **kwargs) -> None:
                self.calls.append(("delete", kwargs))

        client = FakeClient()
        cos_write_preflight(client, "bucket-123", "prefix/probe.txt")

        self.assertEqual(
            [name for name, _ in client.calls],
            ["put", "head", "delete"],
        )
        self.assertEqual(client.calls[0][1]["Bucket"], "bucket-123")
        self.assertEqual(client.calls[0][1]["Key"], "prefix/probe.txt")

    def test_prunes_status_rows_for_removed_participants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.csv"
            fields = [
                "updated_at",
                "participant_id",
                "status",
                "started_at",
                "finished_at",
                "returncode",
                "selected_videos",
                "uploaded_videos",
                "manifest",
                "url_csv",
                "log_path",
            ]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(
                    {"participant_id": "EGO4D_P000001", "status": "ok"}
                )
                writer.writerow(
                    {"participant_id": "EGO4D_P000002", "status": "error"}
                )

            removed = prune_queue_status(path, {"EGO4D_P000001"})

            self.assertEqual(removed, 1)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [row["participant_id"] for row in rows],
                ["EGO4D_P000001"],
            )


if __name__ == "__main__":
    unittest.main()
