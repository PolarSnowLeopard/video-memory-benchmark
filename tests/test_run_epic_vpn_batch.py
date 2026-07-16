import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from scripts.run_epic_vpn_batch import (
    DEFAULT_MAX_PROXY_DURATION_ERROR_SEC,
    parse_ffmpeg_duration,
    transcode_proxy,
    validate_proxy_duration,
)


class EpicVpnBatchTests(unittest.TestCase):
    def test_proxy_duration_validation(self) -> None:
        self.assertEqual(DEFAULT_MAX_PROXY_DURATION_ERROR_SEC, 1.0)
        self.assertAlmostEqual(
            parse_ffmpeg_duration("Duration: 00:10:05.25, start: 0.0"),
            605.25,
        )
        self.assertAlmostEqual(validate_proxy_duration(30.1, 30.0, 1.0), 0.1)
        with self.assertRaisesRegex(RuntimeError, "Proxy duration mismatch"):
            validate_proxy_duration(8.13, 30.0, 1.0)

    def test_transcode_uses_atomic_temporary_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw.mp4"
            proxy = root / "proxy.mp4"
            raw.write_bytes(b"raw")

            def fake_run(command, cwd=None, dry_run=False):
                Path(command[-1]).write_bytes(b"proxy")

            with patch("scripts.run_epic_vpn_batch.run", side_effect=fake_run):
                transcode_proxy(raw, proxy, ffmpeg_threads=2, dry_run=False)

            self.assertEqual(proxy.read_bytes(), b"proxy")
            self.assertFalse((root / "proxy.part.mp4").exists())


if __name__ == "__main__":
    unittest.main()
