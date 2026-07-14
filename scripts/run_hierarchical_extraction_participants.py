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
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SUFFIX = "_all_videos_proxy_540p16_urls.csv"
EPIC_MANIFEST_PATTERN = re.compile(r"^(p\d{2})_all_videos_proxy_540p16_urls\.csv$", re.IGNORECASE)
SAFE_PARTICIPANT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

PIPELINE_STATUS_FIELDS = [
    "updated_at",
    "participant_id",
    "status",
    "started_at",
    "finished_at",
    "manifest",
    "error",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fieldnames} for row in rows)
    tmp_path.replace(path)


def upsert_pipeline_status(path: Path, row: dict[str, str]) -> None:
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    else:
        rows = []
    rows = [item for item in rows if item.get("participant_id") != row.get("participant_id")]
    rows.append({field: row.get(field, "") for field in PIPELINE_STATUS_FIELDS})
    rows.sort(key=lambda item: item["participant_id"].casefold())
    write_csv(path, rows, PIPELINE_STATUS_FIELDS)


def normalize_participant(value: str) -> str:
    clean = value.strip()
    epic = re.fullmatch(r"[pP]?(\d{1,2})", clean)
    if epic:
        return f"P{int(epic.group(1)):02d}"
    ego4d = re.fullmatch(r"ego4d_p(\d{1,6})", clean, re.IGNORECASE)
    if ego4d:
        return f"EGO4D_P{int(ego4d.group(1)):06d}"
    if not SAFE_PARTICIPANT_PATTERN.fullmatch(clean):
        raise ValueError(f"Invalid participant id: {value}")
    return clean


def participant_slug(participant: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", participant).strip("_.-").lower()


def participant_sort_key(participant: str) -> tuple[int, int, str]:
    epic = re.fullmatch(r"P(\d{2})", participant)
    if epic:
        return (0, int(epic.group(1)), participant)
    ego4d = re.fullmatch(r"EGO4D_P(\d{6})", participant)
    if ego4d:
        return (1, int(ego4d.group(1)), participant)
    return (2, 0, participant.casefold())


def manifest_participant(path: Path) -> str | None:
    with path.open(newline="", encoding="utf-8") as handle:
        participants = {
            row.get("participant_id", "").strip()
            for row in csv.DictReader(handle)
            if row.get("participant_id", "").strip()
        }
    if len(participants) > 1:
        raise ValueError(f"Manifest contains multiple participants: {path}")
    if participants:
        return normalize_participant(participants.pop())
    match = EPIC_MANIFEST_PATTERN.fullmatch(path.name)
    return normalize_participant(match.group(1)) if match else None


def discover_participant_manifests(manifest_dir: Path, participants: str) -> list[tuple[str, Path]]:
    available: dict[str, tuple[str, Path]] = {}
    for path in manifest_dir.glob("*_all_videos_proxy_540p16_urls.csv"):
        participant = manifest_participant(path)
        if participant is None:
            continue
        key = participant.casefold()
        if key in available:
            raise ValueError(f"Duplicate manifests for participant {participant}")
        available[key] = (participant, path)

    if participants.strip().lower() == "all":
        wanted = sorted(
            (item[0] for item in available.values()),
            key=participant_sort_key,
        )
    else:
        wanted = [normalize_participant(item) for item in participants.split(",") if item.strip()]

    missing = [participant for participant in wanted if participant.casefold() not in available]
    if missing:
        raise FileNotFoundError(f"Missing participant manifests: {', '.join(missing)}")
    if not wanted:
        raise FileNotFoundError(f"No participant manifests found in {manifest_dir}")
    return [(available[participant.casefold()][0], available[participant.casefold()][1]) for participant in wanted]


def select_manifest_shard(
    manifests: list[tuple[str, Path]], num_shards: int, shard_index: int
) -> list[tuple[str, Path]]:
    if num_shards < 1:
        raise ValueError("num_shards must be >= 1")
    if not 0 <= shard_index < num_shards:
        raise ValueError("shard_index must be in [0, num_shards)")
    return [item for index, item in enumerate(manifests) if index % num_shards == shard_index]


def clean_output_ids(output_dir: Path) -> set[str]:
    suffix = ".clean.json"
    if not output_dir.exists():
        return set()
    return {path.name[: -len(suffix)] for path in output_dir.glob(f"*{suffix}")}


def csv_record_ids(path: Path, field: str) -> set[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row[field] for row in csv.DictReader(handle) if row.get(field)}


def jsonl_record_ids(path: Path, field: str = "record_id") -> set[str]:
    with path.open(encoding="utf-8") as handle:
        return {
            str(record[field])
            for line in handle
            if line.strip()
            for record in [json.loads(line)]
            if record.get(field)
        }


def replace_command_option(command: list[str], option: str, value: str) -> list[str]:
    updated = list(command)
    if option in updated:
        index = updated.index(option)
        updated[index + 1] = value
    else:
        updated.extend([option, value])
    return updated


def run_until_clean(
    *,
    label: str,
    output_dir: Path,
    expected: int,
    expected_ids: set[str] | None = None,
    attempts: int,
    command: list[str],
    final_max_tokens: int | None = None,
    runner: Callable[[list[str]], None],
) -> None:
    if expected_ids is not None and len(expected_ids) != expected:
        raise ValueError(f"{label}: expected count {expected} does not match {len(expected_ids)} record ids")

    for attempt in range(1, attempts + 1):
        clean_ids = clean_output_ids(output_dir)
        if expected_ids is not None:
            extra_ids = sorted(clean_ids - expected_ids)
            if extra_ids:
                raise RuntimeError(f"{label}: stale clean outputs: {', '.join(extra_ids[:20])}")
            missing_ids = sorted(expected_ids - clean_ids)
            actual = expected - len(missing_ids)
        else:
            missing_ids = []
            actual = len(clean_ids)
        if actual == expected:
            return
        if actual > expected:
            raise RuntimeError(f"{label}: found {actual} clean outputs, expected {expected}; remove stale outputs")
        missing_text = f", missing={','.join(missing_ids[:20])}" if missing_ids else ""
        print(f"{label}: attempt {attempt}/{attempts}, clean={actual}/{expected}{missing_text}", flush=True)
        retry_command = list(command)
        if final_max_tokens is not None and attempt == attempts:
            retry_command = replace_command_option(retry_command, "--max-tokens", str(final_max_tokens))
        if missing_ids and len(missing_ids) < expected:
            retry_command.extend(["--record-ids", ",".join(missing_ids)])
        try:
            runner(retry_command)
        except subprocess.CalledProcessError as exc:
            print(f"{label}: command failed with return code {exc.returncode}", flush=True)

    clean_ids = clean_output_ids(output_dir)
    missing_ids = sorted(expected_ids - clean_ids) if expected_ids is not None else []
    actual = expected - len(missing_ids) if expected_ids is not None else len(clean_ids)
    if actual != expected:
        missing_text = f"; missing record ids: {', '.join(missing_ids[:50])}" if missing_ids else ""
        raise RuntimeError(
            f"{label}: found {actual} clean outputs after {attempts} attempts, expected {expected}{missing_text}"
        )


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


def http_server_serves_directory(base_url: str, directory: Path, timeout: float = 2.0) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    sentinel = directory / f".video_memory_benchmark_root_{token}"
    sentinel.write_text(token, encoding="utf-8")
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/{sentinel.name}", timeout=timeout
        ) as response:
            return response.read().decode("utf-8") == token
    except Exception:
        return False
    finally:
        sentinel.unlink(missing_ok=True)


def command_runner(command: list[str]) -> None:
    print("$ " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def build_session_prepare_command(
    python: str, manifest: Path, data_root: Path, local_url_base: str
) -> list[str]:
    return [
        python,
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
        "reencode",
        "--reencode-crf",
        "23",
        "--local-url-base",
        local_url_base,
        "--fail-fast",
    ]


def extract_participant(
    participant: str,
    manifest: Path,
    args: argparse.Namespace,
) -> None:
    lower = participant_slug(participant)
    data_root = args.data_root
    output_root = args.output_root
    manifest_stem = manifest.stem
    if not manifest_stem.endswith("_proxy_540p16_urls"):
        raise ValueError(f"Unexpected proxy manifest name: {manifest.name}")
    source_stem = manifest_stem[: -len("_proxy_540p16_urls")]
    micro_csv = data_root / "cos_urls" / f"{source_stem}_sessions_30s_urls.csv"
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
        build_session_prepare_command(
            args.python,
            manifest,
            data_root,
            args.local_url_base,
        )
    )

    expected_micro_ids = csv_record_ids(micro_csv, "session_id")
    expected_micro = len(expected_micro_ids)
    run_until_clean(
        label=f"{participant} micro",
        output_dir=micro_output,
        expected=expected_micro,
        expected_ids=expected_micro_ids,
        attempts=args.attempts,
        final_max_tokens=8192,
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

    expected_window_ids = jsonl_record_ids(window_input)
    expected_window = len(expected_window_ids)
    run_until_clean(
        label=f"{participant} window",
        output_dir=window_output,
        expected=expected_window,
        expected_ids=expected_window_ids,
        attempts=args.attempts,
        final_max_tokens=12288,
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

    expected_session_ids = jsonl_record_ids(session_input)
    expected_session = len(expected_session_ids)
    run_until_clean(
        label=f"{participant} session",
        output_dir=session_output,
        expected=expected_session,
        expected_ids=expected_session_ids,
        attempts=args.attempts,
        final_max_tokens=24576,
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
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
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
    parser.add_argument(
        "--continue-on-participant-error",
        action="store_true",
        help="Record a failed participant and continue with the remaining queue.",
    )
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error("--attempts must be >= 1")
    if args.num_shards < 1:
        parser.error("--num-shards must be >= 1")
    if not 0 <= args.shard_index < args.num_shards:
        parser.error("--shard-index must be in [0, --num-shards)")
    args.manifest_dir = resolve_path(args.manifest_dir)
    args.data_root = resolve_path(args.data_root)
    args.output_root = resolve_path(args.output_root)
    args.local_url_base = f"http://127.0.0.1:{args.http_port}"
    return args


def main() -> None:
    args = parse_args()
    all_manifests = discover_participant_manifests(args.manifest_dir, args.participants)
    if args.expected_participants is not None and len(all_manifests) != args.expected_participants:
        raise SystemExit(
            f"Expected {args.expected_participants} participant manifests, found {len(all_manifests)}"
        )
    manifests = select_manifest_shard(all_manifests, args.num_shards, args.shard_index)
    if not manifests:
        raise SystemExit(
            f"Shard {args.shard_index}/{args.num_shards} has no participant manifests"
        )
    if not check_url(f"{args.base_url.rstrip('/')}/models"):
        raise SystemExit(f"VLM service is not reachable: {args.base_url}")

    args.data_root.mkdir(parents=True, exist_ok=True)
    session_root = args.data_root / "sessions"
    session_root.mkdir(parents=True, exist_ok=True)
    http_process: subprocess.Popen[bytes] | None = None
    http_log = None
    http_reachable = check_url(args.local_url_base)
    if http_reachable and not http_server_serves_directory(args.local_url_base, session_root):
        raise SystemExit(
            f"HTTP server at {args.local_url_base} serves a different directory. "
            "Stop the old server or choose another --http-port."
        )
    if args.external_http_server and not http_reachable:
        raise SystemExit(f"External video HTTP server is not reachable: {args.local_url_base}")
    if not args.external_http_server and not http_reachable:
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
            if http_server_serves_directory(args.local_url_base, session_root, timeout=1):
                break
            time.sleep(0.5)
        else:
            http_process.terminate()
            raise SystemExit(f"Local video HTTP server failed to start; see {http_log.name}")

    print(f"Participants: {len(manifests)} selected from {len(all_manifests)}", flush=True)
    print(f"Shard: {args.shard_index}/{args.num_shards}", flush=True)
    print(f"Manifest dir: {args.manifest_dir}", flush=True)
    print(f"Cleanup local video: {not args.keep_local_video}", flush=True)
    pipeline_status = args.output_root / "participant_pipeline_status.csv"
    failures: list[str] = []
    try:
        for participant, manifest in manifests:
            started_at = utc_now()
            upsert_pipeline_status(
                pipeline_status,
                {
                    "updated_at": started_at,
                    "participant_id": participant,
                    "status": "running",
                    "started_at": started_at,
                    "finished_at": "",
                    "manifest": str(manifest),
                    "error": "",
                },
            )
            try:
                extract_participant(participant, manifest, args)
            except Exception as exc:
                upsert_pipeline_status(
                    pipeline_status,
                    {
                        "updated_at": utc_now(),
                        "participant_id": participant,
                        "status": "error",
                        "started_at": started_at,
                        "finished_at": utc_now(),
                        "manifest": str(manifest),
                        "error": repr(exc),
                    },
                )
                if not args.continue_on_participant_error:
                    raise
                failures.append(participant)
                print(f"{participant} FAILED; continuing: {exc!r}", flush=True)
                continue
            upsert_pipeline_status(
                pipeline_status,
                {
                    "updated_at": utc_now(),
                    "participant_id": participant,
                    "status": "ok",
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "manifest": str(manifest),
                    "error": "",
                },
            )
    finally:
        if http_process is not None:
            http_process.terminate()
            try:
                http_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                http_process.kill()
        if http_log is not None:
            http_log.close()

    if failures:
        raise SystemExit(
            "Participant extraction failures after queue completion: "
            + ", ".join(failures)
        )
    print("All selected participants completed.", flush=True)


if __name__ == "__main__":
    main()
