import csv
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.prepare_full_qc_bundle import build_bundle, create_archive  # noqa: E402


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class FullQcBundleTests(unittest.TestCase):
    def make_participant(
        self,
        output_root: Path,
        manifest_dir: Path,
        participant: str,
        source: str,
    ) -> None:
        slug = participant.lower()
        write_csv(
            manifest_dir / f"{slug}_all_videos_proxy_540p16_urls.csv",
            ["participant_id", "video_id", "bucket", "region", "key", "signed_url"],
            [
                {
                    "participant_id": participant,
                    "video_id": source,
                    "bucket": "bucket-1",
                    "region": "region-1",
                    "key": f"proxy/{source}.mp4",
                    "signed_url": f"https://example.test/{source}.mp4?sig=old",
                }
            ],
        )
        accepted = (
            output_root
            / f"{slug}_qc"
            / "validation"
            / "session"
            / "accepted"
        )
        accepted.mkdir(parents=True)
        (accepted / f"{source}.clean.json").write_text(
            json.dumps(
                {
                    "source_video_id": source,
                    "participant_id": participant,
                    "cross_session_evidence_candidates": [
                        {"candidate_id": "candidate_1"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        hierarchical = output_root / f"{slug}_qc" / "hierarchical"
        hierarchical.mkdir(parents=True)
        (hierarchical / "session_inputs_30s_120s.jsonl").write_text(
            json.dumps(
                {
                    "record_id": source,
                    "source_video_id": source,
                    "participant_id": participant,
                    "window_ranges": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_builds_validated_minimal_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "outputs"
            manifest_dir = root / "manifests"
            bundle_dir = root / "bundle"
            self.make_participant(output_root, manifest_dir, "P01", "P01_01")
            self.make_participant(output_root, manifest_dir, "P02", "P02_01")
            write_csv(
                output_root / "participant_pipeline_status.csv",
                ["updated_at", "participant_id", "status"],
                [
                    {
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "participant_id": "P01",
                        "status": "ok",
                    },
                    {
                        "updated_at": "2026-01-01T00:00:01+00:00",
                        "participant_id": "P02",
                        "status": "ok",
                    },
                ],
            )

            report = build_bundle(
                output_root,
                manifest_dir,
                bundle_dir,
                expected_participants=2,
            )

            self.assertEqual(report["participants"], 2)
            self.assertEqual(report["sources"], 2)
            self.assertEqual(report["candidates"], 2)
            self.assertEqual(
                sorted(path.name for path in (bundle_dir / "session_records").glob("*")),
                ["P01_01.clean.json", "P02_01.clean.json"],
            )
            self.assertEqual(
                len(
                    [
                        line
                        for line in (bundle_dir / "session_inputs_30s_120s.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                        if line
                    ]
                ),
                2,
            )

            archive_path = root / "bundle.tar.gz"
            create_archive(bundle_dir, archive_path, overwrite=False)
            with tarfile.open(archive_path, "r:gz") as archive:
                names = archive.getnames()
            self.assertIn("bundle/bundle_report.json", names)
            self.assertIn("bundle/session_records/P01_01.clean.json", names)

    def test_rejects_incomplete_participant_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "outputs"
            manifest_dir = root / "manifests"
            self.make_participant(output_root, manifest_dir, "P01", "P01_01")
            write_csv(
                output_root / "participant_pipeline_status.csv",
                ["updated_at", "participant_id", "status"],
                [
                    {
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "participant_id": "P01",
                        "status": "error",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "P01=error"):
                build_bundle(
                    output_root,
                    manifest_dir,
                    root / "bundle",
                    expected_participants=1,
                )

    def test_rejects_source_set_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_root = root / "outputs"
            manifest_dir = root / "manifests"
            self.make_participant(output_root, manifest_dir, "P01", "P01_01")
            accepted = (
                output_root
                / "p01_qc"
                / "validation"
                / "session"
                / "accepted"
            )
            (accepted / "P01_01.clean.json").unlink()
            write_csv(
                output_root / "participant_pipeline_status.csv",
                ["updated_at", "participant_id", "status"],
                [
                    {
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "participant_id": "P01",
                        "status": "ok",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "source mismatch"):
                build_bundle(
                    output_root,
                    manifest_dir,
                    root / "bundle",
                    expected_participants=1,
                )
