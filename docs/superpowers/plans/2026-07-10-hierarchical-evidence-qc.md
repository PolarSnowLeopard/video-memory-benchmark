# Hierarchical Evidence QC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic validation, Bailian batch verification, dispute fallback, human review, and reference-ready export to the existing 30s -> 120s -> session evidence pipeline.

**Architecture:** Existing model outputs remain immutable. Each layer is validated into a separate accepted directory; session candidates are grouped by source video into Bailian Batch requests, merged by stable IDs, and routed either to automatic acceptance or a local evidence review queue. A second pass uses temporary 30-second evidence clips only for disputed or insufficient candidates, after which unresolved items require human decisions.

**Tech Stack:** Python 3.10+, standard library, OpenAI-compatible Python SDK, Tencent COS SDK, ffmpeg/imageio-ffmpeg, unittest.

## Global Constraints

- Never overwrite original `.json` or `.clean.json` model outputs.
- Never commit API keys, COS credentials, signed URLs, video files, or generated QC results.
- Read the Bailian key only from `DASHSCOPE_API_KEY`.
- Use stable `source_video_id`, `candidate_id`, window IDs, and clip IDs as joins.
- Treat `contradicted` from full-video verification as disputed until local evidence verification completes.
- Never auto-accept verifier-generated corrected claims.
- Current QC verifies visual evidence only; audio claims remain outside automatic acceptance.

---

### Task 1: Deterministic hierarchical validator

**Files:**
- Create: `scripts/validate_hierarchical_evidence.py`
- Create: `tests/test_validate_hierarchical_evidence.py`

**Interfaces:**
- Produces `validate_micro_record(record, metadata)`, `validate_window_record(record, parent)`, and `validate_session_record(record, parent)` returning `(normalized_record, issues)`.
- CLI subcommands `micro`, `window`, and `session` write accepted clean JSON files, `issues.csv`, and `report.json` under a caller-provided output directory.
- Session candidates receive `quality_flags`, `normalized_confidence`, `qc_status`, and `usable_for_reference` without changing their claim text.

- [ ] **Step 1: Write failing tests for structural references**

```python
def test_window_validator_rejects_unknown_clip_reference():
    parent = {"micro_clip_ids": ["P30_01_s000"]}
    record = valid_window_record()
    record["evidence_facts"][0]["supporting_clip_ids"] = ["missing"]
    _, issues = validate_window_record(record, parent)
    assert "unknown_clip_reference" in {issue["code"] for issue in issues}
```

- [ ] **Step 2: Run the validator tests and confirm they fail because the module does not exist**

Run: `python3 -m unittest tests.test_validate_hierarchical_evidence -v`

Expected: import failure for `scripts.validate_hierarchical_evidence`.

- [ ] **Step 3: Implement required-field, enum, ID, time, count, and parent-reference checks**

Use a common issue shape:

```python
{
    "layer": "session",
    "record_id": "P30_01",
    "candidate_id": "memcand_1",
    "severity": "blocking|warning",
    "code": "unknown_window_reference",
    "message": "supporting_window_ids references P30_01_w999"
}
```

- [ ] **Step 4: Write failing tests for long-term wording and uncertainty propagation**

```python
def test_session_validator_blocks_long_term_claim_and_downgrades_uncertain_high_confidence():
    record = valid_session_record()
    record["cross_session_evidence_candidates"][0]["claim"] = "用户通常把刀放入冰箱"
    record["contradictions_or_uncertainties"] = [{
        "item": "位置",
        "description": "柜体类型不清楚",
        "affected_candidate_ids": ["memcand_1"],
        "supporting_window_ids": ["P30_01_w000"],
    }]
    normalized, _ = validate_session_record(record, valid_session_parent())
    candidate = normalized["cross_session_evidence_candidates"][0]
    assert "long_term_overclaim" in candidate["quality_flags"]
    assert "affected_by_uncertainty" in candidate["quality_flags"]
    assert candidate["normalized_confidence"] == "medium"
    assert candidate["usable_for_reference"] is False
```

- [ ] **Step 5: Implement candidate normalization and the three CLI subcommands**

The session CLI must preserve original files, write only structurally accepted records to `accepted/`, and write all issues and aggregate counts to deterministic CSV/JSON reports.

- [ ] **Step 6: Run validator tests and the existing test suite**

Run: `python3 -m unittest tests.test_validate_hierarchical_evidence -v`

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

### Task 2: Bailian full-video batch request builder

**Files:**
- Create: `prompts/video_candidate_verification_schema_zh.txt`
- Create: `scripts/build_bailian_qc_batch.py`
- Create: `tests/test_build_bailian_qc_batch.py`

**Interfaces:**
- Produces `build_source_requests(session_records, session_inputs, proxy_rows, prompt, model, fps)`.
- CLI subcommand `source` writes OpenAI Batch JSONL plus a provenance manifest.
- One request represents one source video and includes all of its session candidates, each with resolved supporting window time ranges.

- [ ] **Step 1: Write a failing test for one-request-per-source grouping**

```python
def test_source_builder_groups_candidates_and_resolves_window_ranges():
    sessions = [{
        "session_id": "P30_01",
        "source_video_id": "P30_01",
        "cross_session_evidence_candidates": [{
            "candidate_id": "memcand_1",
            "claim": "刀具被放入柜内",
            "supporting_window_ids": ["P30_01_w000"],
            "quality_flags": [],
        }],
    }]
    session_inputs = [{
        "session_id": "P30_01",
        "source_video_id": "P30_01",
        "window_ranges": [{
            "window_id": "P30_01_w000",
            "start_sec": 0.0,
            "end_sec": 120.0,
        }],
    }]
    proxy_rows = [{"video_id": "P30_01", "signed_url": "https://example.test/P30_01.mp4"}]
    requests, manifest = build_source_requests(
        sessions,
        session_inputs,
        proxy_rows,
        prompt="核验候选。",
        model="qwen3.7-plus",
        fps=0.5,
        max_tokens=4096,
    )
    assert len(requests) == 1
    assert requests[0]["custom_id"] == "P30_01"
    content = requests[0]["body"]["messages"][0]["content"]
    assert content[0]["type"] == "video_url"
    assert content[0]["video_url"]["fps"] == 0.5
    assert manifest[0]["candidates"][0]["support_ranges"] == [
        {"start_sec": 0.0, "end_sec": 120.0}
    ]
```

- [ ] **Step 2: Run the test and confirm the missing-module failure**

Run: `python3 -m unittest tests.test_build_bailian_qc_batch -v`

- [ ] **Step 3: Implement URL-column normalization and request construction**

Batch request bodies must use:

```python
{
    "custom_id": source_video_id,
    "method": "POST",
    "url": "/v1/chat/completions",
    "body": {
        "model": model,
        "enable_thinking": False,
        "messages": [{"role": "user", "content": [
            {"type": "video_url", "video_url": {"url": signed_url, "fps": fps}},
            {"type": "text", "text": prompt_and_candidate_json},
        ]}],
        "temperature": 0,
        "max_tokens": max_tokens,
    },
}
```

- [ ] **Step 4: Implement strict verification prompt and provenance manifest**

The prompt must require exactly one verdict per input candidate, ban external knowledge, distinguish `entailed`, `contradicted`, and `insufficient`, and forbid silently rewriting claims.

- [ ] **Step 5: Test duplicate IDs, missing proxy URLs, unknown windows, and signed-URL handling**

Signed URLs may appear in generated runtime files but must not be copied into reports intended for Git.

- [ ] **Step 6: Run new and existing tests**

Run: `python3 -m unittest tests.test_build_bailian_qc_batch -v`

Run: `python3 -m unittest discover -s tests -v`

### Task 3: Bailian Batch lifecycle client

**Files:**
- Create: `scripts/bailian_batch_job.py`
- Create: `tests/test_bailian_batch_job.py`
- Modify: `requirements/cluster.txt`

**Interfaces:**
- CLI subcommands: `submit`, `status`, and `download`.
- `submit` uploads the JSONL with purpose `batch`, creates `/v1/chat/completions` batch work, and writes a job record containing hashes and remote IDs but no secret.
- `status` refreshes the job record; `download` writes output and error JSONL files without overwriting unless `--overwrite` is present.

- [ ] **Step 1: Write failing tests for environment-key enforcement and job record serialization**

```python
def test_require_api_key_reads_named_environment_variable(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        require_api_key("DASHSCOPE_API_KEY")
```

Use `unittest.mock.patch.dict` instead of pytest because the repository uses unittest.

- [ ] **Step 2: Run the test and confirm the missing-module failure**

Run: `python3 -m unittest tests.test_bailian_batch_job -v`

- [ ] **Step 3: Implement pure helpers first, then the OpenAI-compatible calls**

Use the default base URL `https://dashscope.aliyuncs.com/compatible-mode/v1`. Store `input_sha256`, `input_file_id`, `batch_id`, timestamps, status, `output_file_id`, and `error_file_id`.

- [ ] **Step 4: Add idempotency guards**

Submitting with an existing active job record must fail unless `--force-new` is supplied. Downloading over an existing file must fail unless `--overwrite` is supplied.

- [ ] **Step 5: Run lifecycle helper tests and the full suite**

Run: `python3 -m unittest tests.test_bailian_batch_job -v`

Run: `python3 -m unittest discover -s tests -v`

### Task 4: First-pass result merge and quality report

**Files:**
- Create: `scripts/merge_bailian_qc_results.py`
- Create: `tests/test_merge_bailian_qc_results.py`

**Interfaces:**
- Produces `parse_batch_output_line`, `merge_source_verdicts`, and `build_quality_report`.
- CLI subcommand `source` consumes normalized session outputs, request manifest, Batch output JSONL, and optional Batch error JSONL.
- Writes per-source QC JSON, `local_review_queue.jsonl`, `human_review.csv`, and `quality_report.json`.

- [ ] **Step 1: Write failing tests for Batch response parsing and candidate coverage**

```python
def test_merge_routes_contradicted_and_insufficient_to_local_review():
    session = session_with_candidates(["memcand_1", "memcand_2"])
    manifest = source_manifest("P30_01", ["memcand_1", "memcand_2"])
    response = verifier_response({
        "memcand_1": "contradicted",
        "memcand_2": "insufficient",
    })
    merged, queue = merge_source_verdicts(session, manifest, response)
    assert merged["candidates"][0]["qc_status"] == "verification_disputed"
    assert merged["candidates"][1]["qc_status"] == "verification_uncertain"
    assert {item["candidate_id"] for item in queue} == {"memcand_1", "memcand_2"}
```

- [ ] **Step 2: Run the test and verify the missing-module failure**

Run: `python3 -m unittest tests.test_merge_bailian_qc_results -v`

- [ ] **Step 3: Implement robust extraction of JSON from Batch response bodies**

Support standard Batch lines with `custom_id`, `response.status_code`, and `response.body.choices[0].message.content`. Missing candidates, duplicate verdicts, malformed JSON, and non-200 responses must become explicit errors rather than silent omissions.

- [ ] **Step 4: Implement first-pass routing**

- `entailed` plus no blocking flags and overlapping evidence -> `verification_passed`, `usable_for_reference=true`.
- `entailed` with blocking flags -> `human_review_required`.
- `contradicted` -> `verification_disputed`, local review required.
- `insufficient` -> `verification_uncertain`, local review required.
- Corrected claims always require human review and are never auto-applied.

- [ ] **Step 5: Implement report counters and inspectable review artifacts**

Reports include counts and rates by verdict, candidate type, quality flag, participant, and QC status. Human review CSV contains blank `human_decision` and `human_notes` columns.

- [ ] **Step 6: Run merge tests and full suite**

Run: `python3 -m unittest tests.test_merge_bailian_qc_results -v`

Run: `python3 -m unittest discover -s tests -v`

### Task 5: Local evidence fallback and finalization

**Files:**
- Create: `scripts/prepare_qc_review_clips.py`
- Create: `tests/test_prepare_qc_review_clips.py`
- Extend: `scripts/build_bailian_qc_batch.py`
- Extend: `scripts/merge_bailian_qc_results.py`
- Create: `scripts/finalize_reference_evidence.py`
- Create: `tests/test_finalize_reference_evidence.py`

**Interfaces:**
- `prepare_qc_review_clips.py` consumes the local review queue and source proxy URL CSV, maps support ranges to a reusable 30-second grid, cuts only needed clips, optionally uploads them to a temporary COS prefix, and writes candidate-to-clip mappings.
- `build_bailian_qc_batch.py local` creates one request per candidate with its temporary evidence video URLs.
- `merge_bailian_qc_results.py local` merges second-pass verdicts into first-pass QC files.
- `finalize_reference_evidence.py` applies optional human CSV decisions and exports `reference_ready.jsonl`, `rejected.jsonl`, and `unresolved.jsonl`.

- [ ] **Step 1: Write failing tests for range-to-clip deduplication**

```python
def test_review_ranges_share_the_same_thirty_second_clip():
    specs = build_clip_specs([
        review_item("memcand_1", 31, 45),
        review_item("memcand_2", 40, 58),
    ], clip_sec=30)
    assert [spec["clip_id"] for spec in specs] == ["P30_01_qc_s001"]
```

- [ ] **Step 2: Implement deterministic clip planning before ffmpeg/COS side effects**

Clip IDs use the source ID and 30-second grid index. The mapping records all candidate IDs using each clip. Existing valid local clips and successful upload rows are skipped unless overwrite is requested.

- [ ] **Step 3: Implement cutting, temporary upload, and cleanup manifest**

Reuse binary resolution and COS configuration helpers already used by `run_epic_vpn_session_batch.py`. Do not delete source proxies. Delete temporary local clips only after successful upload when explicitly requested. Generate a cleanup CSV containing bucket, key, and candidate references; do not automatically delete remote objects in the same run.

- [ ] **Step 4: Write failing tests for local Batch requests and second-pass transitions**

Test `local_verification_passed`, `local_verification_rejected`, and `human_review_required`, including the rule that corrected claims remain unresolved.

- [ ] **Step 5: Implement local Batch build and merge subcommands**

The local prompt receives one candidate and one or more short video clips. A second `entailed` result can pass only if no blocking quality flags remain. A second `contradicted` result becomes `local_verification_rejected`. A second `insufficient` result becomes `human_review_required`.

- [ ] **Step 6: Write failing tests for final export**

```python
def test_finalize_exports_only_usable_or_human_accepted_candidates():
    ready, rejected, unresolved = finalize_candidates(qc_records, human_rows)
    assert [item["candidate_id"] for item in ready] == ["memcand_1", "memcand_3"]
```

- [ ] **Step 7: Implement finalization and run all tests**

Run: `python3 -m unittest tests.test_prepare_qc_review_clips tests.test_finalize_reference_evidence -v`

Run: `python3 -m unittest discover -s tests -v`

### Task 6: Operational documentation and P30 dry-run verification

**Files:**
- Modify: `docs/hierarchical_evidence_pipeline.md`
- Create: `docs/hierarchical_evidence_qc_pipeline.md`
- Modify: `docs/repository_architecture.md`

**Interfaces:**
- Documents exact P30 commands from validation through Batch submission, result download, local fallback, human review, and final export.
- Includes a no-network dry-run command that builds P30 requests and reports counts without submitting to Bailian.

- [ ] **Step 1: Document environment variables, directories, and exact command sequence**

Document `DASHSCOPE_API_KEY`, the default Bailian base URL, COS config location, model IDs, fps, token settings, and generated artifact paths. Explain that signed URLs must outlive the Batch task.

- [ ] **Step 2: Add the QC stage to repository architecture and the existing extraction guide**

Keep the distinction between annotation units and true agent sessions explicit.

- [ ] **Step 3: Run a P30 local dry-run using the downloaded hierarchical outputs**

Use the local P30 session outputs under `data/tmp/cluster_outputs/p30_hierarchical/extracted` and a sanitized synthetic proxy URL CSV if the real signed URL manifest is unavailable. Verify request count, candidate count, reference integrity, and that the known P30_113 knife/fridge claim receives a blocking or review flag.

- [ ] **Step 4: Run final verification**

Run the repository's bundled Python:

```bash
/Users/zhaofanyu/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m unittest discover -s tests -v
```

Run:

```bash
git diff --check
git status --short
```

Expected: all tests pass, no whitespace errors, and only intended QC files plus pre-existing user changes appear.
