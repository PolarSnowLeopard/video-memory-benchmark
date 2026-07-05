import sys
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.qwen_video_batch import build_extra_body, parse_json_object_arg  # noqa: E402


class QwenVideoBatchTests(unittest.TestCase):
    def test_build_extra_body_merges_video_fps_with_user_extra_body(self) -> None:
        args = Namespace(
            fps=0.5,
            extra_body_json='{"chat_template_kwargs":{"enable_thinking":false}}',
        )

        self.assertEqual(
            build_extra_body(args),
            {
                "chat_template_kwargs": {"enable_thinking": False},
                "mm_processor_kwargs": {"fps": 0.5, "do_sample_frames": True},
            },
        )

    def test_parse_json_object_arg_rejects_non_objects(self) -> None:
        self.assertEqual(parse_json_object_arg(None), {})
        with self.assertRaises(ValueError):
            parse_json_object_arg('"not object"')


if __name__ == "__main__":
    unittest.main()
