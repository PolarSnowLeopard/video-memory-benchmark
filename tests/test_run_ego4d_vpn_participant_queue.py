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
    detect_source_auth_error,
    discover_manifests,
    output_url_csv,
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


if __name__ == "__main__":
    unittest.main()
