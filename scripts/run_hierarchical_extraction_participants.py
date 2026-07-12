#!/usr/bin/env python3
"""Run the three-layer evidence extraction pipeline participant by participant."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATTERN = re.compile(r"^(p\d{2})_all_videos_proxy_540p16_urls\.csv$", re.IGNORECASE)


def normalize_participant(value: str) -> str:
    match = re.fullmatch(r"[pP]?(\d{1,2})", value.strip())
    if not match:
        raise ValueError(f"Invalid participant id: {value}")
    return f"P{int(match.group(1)):02d}"


def discover_participant_manifests(manifest_dir: Path, participants: str) -> list[tuple[str, Path]]:
    available: dict[str, Path] = {}
    for path in manifest_dir.glob("*_all_videos_proxy_540p16_urls.csv"):
        match = MANIFEST_PATTERN.fullmatch(path.name)
        if match:
            participant = normalize_participant(match.group(1))
            available[participant] = path

    if participants.strip().lower() == "all":
        wanted = sorted(available, key=lambda item: int(item[1:]))
    else:
        wanted = [normalize_participant(item) for item in participants.split(",") if item.strip()]

    missing = [participant for participant in wanted if participant not in available]
    if missing:
        raise FileNotFoundError(f"Missing participant manifests: {', '.join(missing)}")
    if not wanted:
        raise FileNotFoundError(f"No participant manifests found in {manifest_dir}")
    return [(participant, available[participant]) for participant in wanted]


def count_csv_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def count_jsonl_rows(path: Path) -> int:
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def count_clean_outputs(output_dir: Path) -> int:
    return sum(1 for _ in output_dir.glob("*.clean.json")) if output_dir.exists() else 0


def run_until_clean(
    *,
    label: str,
    output_dir: Path,
    expected: int,
    attempts: int,
    command: list[str],
    runner: Callable[[list[str]], None],
) -> None:
    for attempt in range(1, attempts + 1):
        actual = count_clean_outputs(output_dir)
        if actual == expected:
            return
        if actual > expected:
            raise RuntimeError(f"{label}: found {actual} clean outputs, expected {expected}; remove stale outputs")
        print(f"{label}: attempt {attempt}/{attempts}, clean={actual}/{expected}", flush=True)
        try:
            runner(command)
        except subprocess.CalledProcessError as exc:
            print(f"{label}: command failed with return code {exc.returncode}", flush=True)

    actual = count_clean_outputs(output_dir)
    if actual != expected:
        raise RuntimeError(f"{label}: found {actual} clean outputs after {attempts} attempts, expected {expected}")


def require_validation_complete(report_path: Path, expected: int, label: str) -> None:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    actual = {key: int(report.get(key, -1)) for key in ("records", "accepted", "rejected")}
    print(f"{label}: validation={actual}, expected={expected}", flush=True)
    if actual != {"records": expected, "accepted": expected, "rejected": 0}:
        raise RuntimeError(f"{label}: validation incomplete {actual}; expected {expected} accepted records")


def check_url(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def command_runner(command: list[str]) -> None:
    print("$ " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def extract_participant(
    participant: str,
    manifest: Path,
    args: argparse.Namespace,
) -> None:
    lower = participant.lower()
    data_root = args.data_root
    output_root = args.output_root
    micro_csv = data_root / "cos_urls" / f"{lower}_all_videos_sessions_30s_urls.csv"
    micro_output = output_root / f"{lower}_micro_30s"
    qc_root = output_root / f"{lower}_qc"
    hierarchy = qc_root / "hierarchical"
    window_output = output_root / f"{lower}_windows_120s"
    session_output = output_root / f"{lower}_sessions_full"
    window_input = hierarchy / "window_inputs_30s_120s.jsonl"
    session_input = hierarchy / "session_inputs_30s_120s.jsonl"

    print(f"\n{'=' * 28} {participant} {'=' * 28}", flush=True)
    for path in (micro_output, hierarchy, window_output, session_output):
        path.mkdir(parents=True, exist_ok=True)

    command_runner(
        [
            args.python,
            "scripts/prepare_video_sessions_for_inference.py",
            "--video-url-csv",
            str(manifest),
            "--data-root",
            str(data_root),
            "--source-cache-root",
            str(data_root / "proxy_from_cos"),
            "--download-missing-source",
            "--session-duration-sec",
            "30",
            "--min-tail-sec",
            "10",
            "--cut-mode",
            "copy",
            "--local-url-base",
            args.local_url_base,
        ]
    )

    expected_micro = count_csv_rows(micro_csv)
    run_until_clean(
        label=f"{participant} micro",
        output_dir=micro_output,
        expected=expected_micro,
        attempts=args.attempts,
        runner=command_runner,
        command=[
            args.python,
            "scripts/qwen_video_batch.py",
            "--base-url",
            args.base_url,
            "--model",
            args.model,
            "--signed-url-csv",
            str(micro_csv),
            "--prompt-file",
            "prompts/video_micro_evidence_schema_zh.txt",
            "--output-dir",
            str(micro_output),
            "--fps",
            "1",
            "--max-tokens",
            "4096",
            "--temperature",
            "0",
            "--extra-body-json",
            '{"chat_template_kwargs":{"enable_thinking":false}}',
        ],
    )

    micro_validation = qc_root / "validation/micro"
    command_runner(
        [
            args.python,
            "scripts/validate_hierarchical_evidence.py",
            "micro",
            "--input-dir",
            str(micro_output),
            "--metadata",
            str(micro_csv),
            "--output-dir",
            str(micro_validation),
        ]
    )
    require_validation_complete(micro_validation / "report.json", expected_micro, f"{participant} micro")

    command_runner(
        [
            args.python,
            "scripts/build_hierarchical_evidence_inputs.py",
            "windows",
            "--micro-url-csv",
            str(micro_csv),
            "--micro-output-dir",
            str(micro_validation / "accepted"),
            "--window-sec",
            "120",
            "--output-jsonl",
            str(window_input),
        ]
    )

    if not args.keep_local_video:
        shutil.rmtree(data_root / "proxy_from_cos" / participant, ignore_errors=True)
        shutil.rmtree(data_root / "sessions" / participant, ignore_errors=True)
        print(f"{participant}: removed local proxy videos and 30-second clips", flush=True)

    expected_window = count_jsonl_rows(window_input)
    run_until_clean(
        label=f"{participant} window",
        output_dir=window_output,
        expected=expected_window,
        attempts=args.attempts,
        runner=command_runner,
        command=[
            args.python,
            "scripts/qwen_text_jsonl_batch.py",
            "--base-url",
            args.base_url,
            "--model",
            args.model,
            "--input-jsonl",
            str(window_input),
            "--prompt-file",
            "prompts/video_window_aggregation_schema_zh.txt",
            "--output-dir",
            str(window_output),
            "--max-tokens",
            "8192",
            "--temperature",
            "0",
            "--extra-body-json",
            '{"chat_template_kwargs":{"enable_thinking":false}}',
        ],
    )

    window_validation = qc_root / "validation/window"
    command_runner(
        [
            args.python,
            "scripts/validate_hierarchical_evidence.py",
            "window",
            "--input-dir",
            str(window_output),
            "--metadata",
            str(window_input),
            "--output-dir",
            str(window_validation),
        ]
    )
    require_validation_complete(window_validation / "report.json", expected_window, f"{participant} window")

    command_runner(
        [
            args.python,
            "scripts/build_hierarchical_evidence_inputs.py",
            "sessions",
            "--window-input-jsonl",
            str(window_input),
            "--window-output-dir",
            str(window_validation / "accepted"),
            "--output-jsonl",
            str(session_input),
        ]
    )

    expected_session = count_jsonl_rows(session_input)
    run_until_clean(
        label=f"{participant} session",
        output_dir=session_output,
        expected=expected_session,
        attempts=args.attempts,
        runner=command_runner,
        command=[
            args.python,
            "scripts/qwen_text_jsonl_batch.py",
            "--base-url",
            args.base_url,
            "--model",
            args.model,
            "--input-jsonl",
            str(session_input),
            "--prompt-file",
            "prompts/video_session_aggregation_schema_zh.txt",
            "--output-dir",
            str(session_output),
            "--max-tokens",
            "16384",
            "--temperature",
            "0",
            "--extra-body-json",
            '{"chat_template_kwargs":{"enable_thinking":false}}',
        ],
    )

    session_validation = qc_root / "validation/session"
    command_runner(
        [
            args.python,
            "scripts/validate_hierarchical_evidence.py",
            "session",
            "--input-dir",
            str(session_output),
            "--metadata",
            str(session_input),
            "--output-dir",
            str(session_validation),
        ]
    )
    require_validation_complete(session_validation / "report.json", expected_session, f"{participant} session")
    print(
        f"{participant} COMPLETE: micro={expected_micro} window={expected_window} session={expected_session}",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-dir", required=True)
    parser.add_argument("--participants", default="all", help="Comma-separated participant ids or all.")
    parser.add_argument("--expected-participants", type=int)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--output-root", default="outputs/epic_kitchens_100")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="qwen35-a3b")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--http-bind", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=18080)
    parser.add_argument("--keep-local-video", action="store_true")
    parser.add_argument("--external-http-server", action="store_true")
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be >= 1")
    args.manifest_dir = resolve_path(args.manifest_dir)
    args.data_root = resolve_path(args.data_root)
    args.output_root = resolve_path(args.output_root)
    args.local_url_base = f"http://127.0.0.1:{args.http_port}"
    return args


def main() -> None:
    args = parse_args()
    manifests = discover_participant_manifests(args.manifest_dir, args.participants)
    if args.expected_participants is not None and len(manifests) != args.expected_participants:
        raise SystemExit(f"Expected {args.expected_participants} participant manifests, found {len(manifests)}")
    if not check_url(f"{args.base_url.rstrip('/')}/models"):
        raise SystemExit(f"VLM service is not reachable: {args.base_url}")

    args.data_root.mkdir(parents=True, exist_ok=True)
    session_root = args.data_root / "sessions"
    session_root.mkdir(parents=True, exist_ok=True)
    http_process: subprocess.Popen[bytes] | None = None
    http_log = None
    if not args.external_http_server and not check_url(args.local_url_base):
        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        http_log = (log_dir / "session_http.log").open("ab")
        http_process = subprocess.Popen(
            [
                args.python,
                "-m",
                "http.server",
                str(args.http_port),
                "--bind",
                args.http_bind,
                "--directory",
                str(session_root),
            ],
            cwd=ROOT,
            stdout=http_log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(20):
            if check_url(args.local_url_base, timeout=1):
                break
            time.sleep(0.5)
        else:
            http_process.terminate()
            raise SystemExit(f"Local video HTTP server failed to start; see {http_log.name}")

    print(f"Participants: {len(manifests)}", flush=True)
    print(f"Manifest dir: {args.manifest_dir}", flush=True)
    print(f"Cleanup local video: {not args.keep_local_video}", flush=True)
    try:
        for participant, manifest in manifests:
            extract_participant(participant, manifest, args)
    finally:
        if http_process is not None:
            http_process.terminate()
            try:
                http_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                http_process.kill()
        if http_log is not None:
            http_log.close()

    print("All selected participants completed.", flush=True)


if __name__ == "__main__":
    main()
