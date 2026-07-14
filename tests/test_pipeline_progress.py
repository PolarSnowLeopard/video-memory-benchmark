import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.pipeline_progress import format_duration, progress_line  # noqa: E402


class PipelineProgressTests(unittest.TestCase):
    def test_formats_duration(self) -> None:
        self.assertEqual(format_duration(None), "--")
        self.assertEqual(format_duration(65), "00:01:05")
        self.assertEqual(format_duration(90061), "1d 01:01:01")

    def test_progress_includes_elapsed_and_eta(self) -> None:
        line = progress_line(2, 4, 10, prefix="Batch", width=10)
        self.assertEqual(
            line,
            "Batch [#####-----] 2/4 ( 50.0%) elapsed=00:00:10 eta=00:00:10",
        )

    def test_zero_progress_has_unknown_eta(self) -> None:
        self.assertIn("eta=--", progress_line(0, 3, 0))


if __name__ == "__main__":
    unittest.main()
