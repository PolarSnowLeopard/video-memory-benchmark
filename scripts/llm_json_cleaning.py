#!/usr/bin/env python3
"""Strictly parse LLM JSON, with an audited syntax-repair fallback."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from json_repair import repair_json


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_text(text: str) -> str:
    text = text.strip()
    match = FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    raise ValueError("No JSON object found in assistant content")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def repair_path_for(clean_path: Path) -> Path:
    suffix = ".clean.json"
    if clean_path.name.endswith(suffix):
        return clean_path.with_name(clean_path.name[: -len(suffix)] + ".repair.json")
    return clean_path.with_suffix(clean_path.suffix + ".repair.json")


def record_id_for(clean_path: Path) -> str:
    suffix = ".clean.json"
    return clean_path.name[: -len(suffix)] if clean_path.name.endswith(suffix) else clean_path.stem


def clean_chat_completion_response(raw_path: Path, clean_path: Path) -> str:
    """Write normalized JSON and return strict_json or json_repair."""

    response = json.loads(raw_path.read_text(encoding="utf-8"))
    choice = response["choices"][0]
    message = choice["message"]
    content = message.get("content") or message.get("reasoning")
    if not content:
        raise ValueError("Assistant content is empty")

    extracted = extract_json_text(content)
    repair_path = repair_path_for(clean_path)
    strict_error = ""
    try:
        payload: Any = json.loads(extracted)
        clean_method = "strict_json"
    except json.JSONDecodeError as exc:
        strict_error = repr(exc)
        finish_reason = str(choice.get("finish_reason") or "")
        if finish_reason != "stop":
            raise ValueError(
                f"Refusing JSON repair because finish_reason={finish_reason or 'missing'}; "
                f"strict parse failed: {strict_error}"
            ) from exc
        payload = repair_json(extracted, return_objects=True, skip_json_loads=True)
        clean_method = "json_repair"

    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = clean_path.with_suffix(clean_path.suffix + ".tmp")
    temporary_path.write_text(serialized, encoding="utf-8")
    temporary_path.replace(clean_path)

    if clean_method == "json_repair":
        audit = {
            "record_id": record_id_for(clean_path),
            "repair_method": "json_repair",
            "repair_library_version": importlib.metadata.version("json-repair"),
            "strict_error": strict_error,
            "finish_reason": str(choice.get("finish_reason") or ""),
            "source_raw_path": str(raw_path),
            "source_raw_sha256": sha256_bytes(raw_path.read_bytes()),
            "extracted_text_sha256": sha256_bytes(extracted.encode("utf-8")),
            "clean_output_path": str(clean_path),
            "clean_output_sha256": sha256_bytes(serialized.encode("utf-8")),
            "repaired_at": datetime.now(timezone.utc).isoformat(),
        }
        repair_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    elif repair_path.exists():
        repair_path.unlink()

    return clean_method
